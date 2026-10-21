"""Protocol-level checks against a real server process over MCP, not REST.

Starts server.py with CATALOG_PATH pointing at a tiny fixture catalog so the suite
stays fast, then exercises initialization, discovery, valid and invalid tools/call
requests, and the HTTP method contract.
"""
import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jsonschema import Draft202012Validator  # noqa: E402

from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

from search import INPUT_SCHEMA, OUTPUT_SCHEMA  # noqa: E402

PROTOCOL_VERSION = '2025-11-25'
OUTPUT_VALIDATOR = Draft202012Validator(OUTPUT_SCHEMA)

_server: subprocess.Popen | None = None
_workdir: tempfile.TemporaryDirectory | None = None
BASE_URL = ''
MCP_URL = ''


def fixture_products(count=60):
    colors = ('Black', 'White', 'Dark Blue')
    types = ('Dress', 'Vest top', 'Bag')
    products = []
    for i in range(count):
        average = None if i % 5 == 0 else 1.0 + (i % 9) * 0.5
        products.append({
            'id': f'{i:010d}',
            'name': f'{colors[i % 3]} {types[i % 3]} number {i}',
            'description': f'Catalog item {i}',
            'category': 'Garment Full body' if i % 3 == 0 else 'Accessories',
            'product_type': types[i % 3],
            'audience': 'Ladieswear',
            'section': 'Basics',
            'color': colors[i % 3],
            'pattern': 'Solid',
            'image_url': f'https://example.invalid/{i}.jpg',
            'price': {'amount': 199.0 + 100 * (i % 20), 'currency': 'INR',
                      'synthetic': True},
            'rating': {'average': average, 'count': 0 if average is None else (i % 8) + 1,
                       'synthetic': True},
            'reviews': [],
        })
    return products


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def setUpModule():
    global _server, _workdir, BASE_URL, MCP_URL
    _workdir = tempfile.TemporaryDirectory()
    catalog_path = Path(_workdir.name) / 'products.json'
    catalog_path.write_text(json.dumps(fixture_products()), encoding='utf-8')

    port = free_port()
    BASE_URL = f'http://127.0.0.1:{port}'
    MCP_URL = f'{BASE_URL}/mcp'
    env = {**os.environ, 'HOST': '127.0.0.1', 'PORT': str(port),
           'CATALOG_PATH': str(catalog_path), 'PYTHONUNBUFFERED': '1'}
    _server = subprocess.Popen([sys.executable, str(ROOT / 'server.py')], cwd=str(ROOT),
                               env=env, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if _server.poll() is not None:
            raise RuntimeError(f'server exited with {_server.returncode}')
        try:
            with urllib.request.urlopen(f'{BASE_URL}/healthz', timeout=2) as response:
                if json.loads(response.read())['status'] == 'ok':
                    return
        except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError):
            time.sleep(0.1)
    tearDownModule()
    raise TimeoutError('server did not become ready')


def tearDownModule():
    global _server, _workdir
    if _server is not None:
        _server.send_signal(signal.SIGINT)
        try:
            _server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            _server.kill()
        _server = None
    if _workdir is not None:
        _workdir.cleanup()
        _workdir = None


async def _call(arguments_list):
    """Initialize once, then call the tool for each argument set."""
    async with streamable_http_client(MCP_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            listed = await session.list_tools()
            results = []
            for arguments in arguments_list:
                results.append(await session.call_tool('search_products', arguments))
            return init, listed, results


def call(arguments_list):
    return asyncio.run(asyncio.wait_for(_call(arguments_list), timeout=60))


class ProtocolTests(unittest.TestCase):
    def test_initialization_pins_the_protocol_revision(self):
        init, _, _ = call([])
        self.assertEqual(init.protocolVersion, PROTOCOL_VERSION)

    def test_discovery_advertises_exactly_one_tool_with_both_schemas(self):
        _, listed, _ = call([])
        self.assertEqual([t.name for t in listed.tools], ['search_products'])
        tool = listed.tools[0]
        self.assertTrue(tool.description)
        self.assertEqual(tool.inputSchema, INPUT_SCHEMA)
        self.assertEqual(tool.outputSchema, OUTPUT_SCHEMA)

    def test_success_returns_structured_content_matching_the_text_block(self):
        _, _, results = call([{}, {'query': 'dress'},
                              {'filters': {'color': 'Black'}, 'sort_by': 'price',
                               'sort_order': 'asc', 'limit': 5}])
        for result in results:
            self.assertFalse(result.isError, result)
            OUTPUT_VALIDATOR.validate(result.structuredContent)
            self.assertEqual(json.loads(result.content[0].text), result.structuredContent)

    def test_no_matches_is_a_successful_empty_response(self):
        _, _, results = call([{'query': 'spaceship engine'},
                              {'filters': {'color': 'no such colour'}}])
        for result in results:
            self.assertFalse(result.isError, result)
            self.assertEqual(result.structuredContent,
                             {'results': [], 'recommendations': []})

    def test_invalid_calls_are_errors_and_never_empty_successes(self):
        invalid = [
            {'limit': 0}, {'limit': 101}, {'limit': True}, {'offset': -1},
            {'offset': 10001}, {'sort_by': 'unknown'}, {'sort_order': 'sideways'},
            {'unexpected': 1}, {'query': None}, {'query': 'x' * 1001},
            {'filters': {'color': ' '}}, {'filters': {'brand': 'x'}},
            {'filters': {'min_price': 5, 'max_price': 1}},
            {'filters': {'min_rating': 0}}, {'filters': {'min_rating': 6}},
            {'filters': {'min_review_count': -1}},
            {'include_recommendations': 'yes'},
        ]
        _, _, results = call(invalid)
        for arguments, result in zip(invalid, results):
            with self.subTest(arguments=arguments):
                self.assertTrue(result.isError, (arguments, result))
                self.assertIsNone(result.structuredContent)

    def test_unknown_tool_name_is_an_error(self):
        async def run():
            async with streamable_http_client(MCP_URL) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await session.call_tool('search_everything', {})
        result = asyncio.run(asyncio.wait_for(run(), timeout=30))
        self.assertTrue(result.isError, result)

    def test_pagination_is_stable_and_prefix_consistent_over_the_wire(self):
        arguments = {'filters': {'color': 'Black'}, 'sort_by': 'price',
                     'sort_order': 'asc', 'offset': 0, 'limit': 5}
        wider = {**arguments, 'limit': 20}
        _, _, results = call([arguments, arguments, wider])
        first = [r['id'] for r in results[0].structuredContent['results']]
        repeat = [r['id'] for r in results[1].structuredContent['results']]
        prefix = [r['id'] for r in results[2].structuredContent['results']][:5]
        self.assertEqual(first, repeat)
        self.assertEqual(first, prefix)

    def test_recommendations_are_empty_unless_enabled(self):
        _, _, results = call([{'query': 'dress'},
                              {'query': 'dress', 'include_recommendations': True}])
        self.assertEqual(results[0].structuredContent['recommendations'], [])
        self.assertLessEqual(len(results[1].structuredContent['recommendations']), 5)


class HttpContractTests(unittest.TestCase):
    def request(self, method, path='/mcp', headers=None, data=None):
        request = urllib.request.Request(f'{BASE_URL}{path}', method=method, data=data,
                                         headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def test_standalone_get_stream_is_rejected_with_405(self):
        status, _ = self.request('GET', headers={
            'Accept': 'text/event-stream',
            'MCP-Protocol-Version': PROTOCOL_VERSION})
        self.assertEqual(status, 405)

    def test_plain_rest_post_is_not_accepted_as_a_search(self):
        status, body = self.request(
            'POST', headers={'Content-Type': 'application/json'},
            data=json.dumps({'query': 'dress'}).encode())
        self.assertNotEqual(status, 200)
        self.assertNotIn(b'"results"', body)

    def test_health_endpoint_reports_the_loaded_catalog(self):
        status, body = self.request('GET', '/healthz')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {'status': 'ok', 'products': 60})


if __name__ == '__main__':
    unittest.main()
