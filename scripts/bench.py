"""Latency bench for the search_products MCP tool, over the real MCP protocol.

Mirrors the brief's performance pool: empty-query filter and field-sort calls with
limit=10 and offset=0, a fixed ordered pool, warm-up calls, then timed workloads at
concurrency 1 and concurrency 5. Timing covers request dispatch to fully parsed tool
response; connection and initialization happen before the clock starts.

    # bench an already running server
    .venv/bin/python scripts/bench.py --url http://127.0.0.1:8000/mcp

    # start the server, measure startup and RSS, bench, smoke test, stop it
    .venv/bin/python scripts/bench.py --manage --smoke
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jsonschema import Draft202012Validator  # noqa: E402

from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

from search import OUTPUT_SCHEMA  # noqa: E402

OUTPUT_VALIDATOR = Draft202012Validator(OUTPUT_SCHEMA)
TIMEOUT_PENALTY_MS = 10_000.0
PERCENTILES: tuple[float, ...] = (50, 90, 95, 99)

# Fixed, deterministic pool: 10 filter sets x 5 sort configurations = 50 calls.
FILTER_SETS: tuple[dict[str, Any], ...] = (
    {},
    {'category': 'Garment Upper body'},
    {'product_type': 'Dress'},
    {'color': 'Black'},
    {'audience': 'Ladieswear'},
    {'section': 'Womens Everyday Collection'},
    {'pattern': 'Solid'},
    {'max_price': 1000},
    {'min_price': 2000, 'max_price': 3000},
    {'min_rating': 4, 'min_review_count': 3},
)
SORT_SETS: tuple[tuple[str, str], ...] = (
    ('relevance', 'desc'),
    ('id', 'asc'),
    ('price', 'asc'),
    ('rating', 'desc'),
    ('review_count', 'desc'),
)


def build_pool() -> list[dict[str, Any]]:
    return [
        {'query': '', 'filters': dict(filters), 'sort_by': sort_by,
         'sort_order': sort_order, 'offset': 0, 'limit': 10,
         'include_recommendations': False}
        for filters in FILTER_SETS
        for sort_by, sort_order in SORT_SETS
    ]


def percentile(sorted_values: Sequence[float], pct: float) -> float:
    """Nearest-rank percentile, as the brief specifies for p95."""
    if not sorted_values:
        return math.nan
    rank = max(1, math.ceil(pct / 100 * len(sorted_values)))
    return float(sorted_values[rank - 1])


@dataclass
class Call:
    key: str
    elapsed_ms: float
    ok: bool
    note: str = ''


@dataclass
class Workload:
    name: str
    concurrency: int
    calls: list[Call] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        latencies = sorted(c.elapsed_ms if c.ok else TIMEOUT_PENALTY_MS for c in self.calls)
        failures = [c for c in self.calls if not c.ok]
        total = len(self.calls)
        wall = sum(c.elapsed_ms for c in self.calls) / 1000 or math.nan
        return {
            'concurrency': self.concurrency,
            'calls': total,
            'errors': len(failures),
            'correct_response_rate': round((total - len(failures)) / total, 6) if total else 0.0,
            'min_ms': round(latencies[0], 2) if latencies else None,
            'max_ms': round(latencies[-1], 2) if latencies else None,
            'mean_ms': round(sum(latencies) / len(latencies), 2) if latencies else None,
            **{f'p{int(p)}_ms': round(percentile(latencies, p), 2) for p in PERCENTILES},
            'sum_service_time_s': round(wall, 3),
            'failure_notes': sorted({c.note for c in failures})[:5],
        }


def pool_key(arguments: dict[str, Any]) -> str:
    return json.dumps(arguments, sort_keys=True)


def check_response(arguments: dict[str, Any], result: Any) -> str:
    """Return an empty string when the response is well formed, else a reason."""
    if result.isError:
        return 'tool returned isError'
    structured = result.structuredContent
    if structured is None:
        return 'missing structuredContent'
    errors = sorted(OUTPUT_VALIDATOR.iter_errors(structured), key=lambda e: e.path)
    if errors:
        return f'schema: {errors[0].message[:80]}'
    if not result.content or json.loads(result.content[0].text) != structured:
        return 'text content does not match structuredContent'
    ids = [r['id'] for r in structured['results']]
    if len(ids) > arguments['limit']:
        return 'more results than limit'
    if len(set(ids)) != len(ids):
        return 'duplicate result ids'
    if structured['recommendations']:
        return 'recommendations returned while disabled'
    return ''


async def timed_call(session: ClientSession, arguments: dict[str, Any],
                     pages: dict[str, list[str]], deadline_s: float) -> Call:
    key = pool_key(arguments)
    started = time.perf_counter()
    try:
        result = await asyncio.wait_for(
            session.call_tool('search_products', arguments), timeout=deadline_s)
    except asyncio.TimeoutError:
        return Call(key, (time.perf_counter() - started) * 1000, False, 'deadline exceeded')
    except Exception as exc:  # transport / protocol failure
        return Call(key, (time.perf_counter() - started) * 1000, False,
                    f'{type(exc).__name__}: {exc}'[:120])
    elapsed_ms = (time.perf_counter() - started) * 1000

    note = check_response(arguments, result)
    if not note:
        ids = [r['id'] for r in result.structuredContent['results']]
        previous = pages.setdefault(key, ids)
        if previous != ids:
            note = 'page not stable across repeated identical request'
    return Call(key, elapsed_ms, not note, note)


async def open_session(stack: AsyncExitStack, url: str) -> ClientSession:
    read, write, _ = await stack.enter_async_context(streamable_http_client(url))
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    return session


async def run_workload(url: str, pool: list[dict[str, Any]], calls: int, concurrency: int,
                       pages: dict[str, list[str]], deadline_s: float,
                       name: str) -> Workload:
    """Issue `calls` requests from the ordered pool, keeping `concurrency` slots busy."""
    workload = Workload(name=name, concurrency=concurrency)
    schedule = [pool[i % len(pool)] for i in range(calls)]
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    for arguments in schedule:
        queue.put_nowait(arguments)

    async with AsyncExitStack() as stack:
        sessions = [await open_session(stack, url) for _ in range(concurrency)]

        async def worker(session: ClientSession) -> None:
            while True:
                try:
                    arguments = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                workload.calls.append(await timed_call(session, arguments, pages, deadline_s))

        await asyncio.gather(*(worker(s) for s in sessions))
    return workload


async def warm_up(url: str, pool: list[dict[str, Any]], count: int, deadline_s: float) -> None:
    async with AsyncExitStack() as stack:
        session = await open_session(stack, url)
        for i in range(count):
            await timed_call(session, pool[i % len(pool)], {}, deadline_s)


async def bench(url: str, calls: int, warmup: int, deadline_s: float) -> dict[str, Any]:
    pool = build_pool()
    pages: dict[str, list[str]] = {}
    await warm_up(url, pool, warmup, deadline_s)
    sequential = await run_workload(url, pool, calls, 1, pages, deadline_s, 'concurrency_1')
    concurrent = await run_workload(url, pool, calls, 5, pages, deadline_s, 'concurrency_5')

    pooled = Workload(name='pooled', concurrency=0,
                      calls=sequential.calls + concurrent.calls)
    return {
        'url': url,
        'pool_size': len(pool),
        'warmup_calls': warmup,
        'call_deadline_s': deadline_s,
        'workloads': {
            'concurrency_1': sequential.summary(),
            'concurrency_5': concurrent.summary(),
        },
        'pooled': pooled.summary(),
        'distinct_pages_checked': len(pages),
    }


def wait_for_health(url: str, timeout_s: float, proc: subprocess.Popen[bytes] | None
                    ) -> tuple[float, dict[str, Any]]:
    """Poll /healthz until ready; return elapsed seconds and the payload."""
    started = time.perf_counter()
    while time.perf_counter() - started < timeout_s:
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(f'server exited early with code {proc.returncode}')
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                payload = json.loads(response.read())
            return time.perf_counter() - started, payload
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            time.sleep(0.2)
    raise TimeoutError(f'server not ready within {timeout_s}s')


def read_rss_kb(pid: int) -> int | None:
    try:
        for line in Path(f'/proc/{pid}/status').read_text().splitlines():
            if line.startswith('VmRSS:'):
                return int(line.split()[1])
    except OSError:
        return None
    return None


def peak_rss_kb(pid: int) -> int | None:
    try:
        for line in Path(f'/proc/{pid}/status').read_text().splitlines():
            if line.startswith('VmHWM:'):
                return int(line.split()[1])
    except OSError:
        return None
    return None


def start_server(host: str, port: int, log_path: Path) -> subprocess.Popen[bytes]:
    env = {**os.environ, 'HOST': host, 'PORT': str(port), 'PYTHONUNBUFFERED': '1'}
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open('wb')
    return subprocess.Popen([sys.executable, str(ROOT / 'server.py')],
                            cwd=str(ROOT), env=env, stdout=handle, stderr=handle)


def stop_server(proc: subprocess.Popen[bytes]) -> None:
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def run_smoke_test(url: str) -> dict[str, Any]:
    started = time.perf_counter()
    completed = subprocess.run([sys.executable, str(ROOT / 'smoke_test.py'), url],
                               cwd=str(ROOT), capture_output=True, text=True)
    return {
        'passed': completed.returncode == 0,
        'seconds': round(time.perf_counter() - started, 2),
        'stdout': completed.stdout.strip()[-500:],
        'stderr': completed.stderr.strip()[-500:],
    }


def print_report(report: dict[str, Any]) -> None:
    server = report.get('server', {})
    if server:
        print(f"startup to /healthz  {server['startup_seconds']}s   "
              f"products {server.get('products')}")
        print(f"RSS after startup    {server['rss_mb_after_startup']} MB   "
              f"after bench {server['rss_mb_after_bench']} MB   "
              f"peak {server['peak_rss_mb']} MB")
        print()
    bench_report = report['bench']
    print(f"{'workload':<16}{'calls':>7}{'errors':>8}{'p50 ms':>10}{'p95 ms':>10}"
          f"{'p99 ms':>10}{'max ms':>10}")
    rows = list(bench_report['workloads'].items()) + [('pooled', bench_report['pooled'])]
    for name, summary in rows:
        print(f"{name:<16}{summary['calls']:>7}{summary['errors']:>8}"
              f"{summary['p50_ms']:>10.2f}{summary['p95_ms']:>10.2f}"
              f"{summary['p99_ms']:>10.2f}{summary['max_ms']:>10.2f}")
    print()
    print(f"correct response rate {bench_report['pooled']['correct_response_rate']:.4f}  "
          f"distinct pages checked {bench_report['distinct_pages_checked']}")
    for name, summary in rows:
        for note in summary['failure_notes']:
            print(f"  ! {name}: {note}")
    smoke = report.get('smoke_test')
    if smoke:
        print(f"smoke test           {'PASS' if smoke['passed'] else 'FAIL'} "
              f"({smoke['seconds']}s)")
        if not smoke['passed']:
            print(smoke['stderr'] or smoke['stdout'])


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default=None,
                        help='MCP endpoint; implies an already running server')
    parser.add_argument('--manage', action='store_true',
                        help='start and stop server.py, measuring startup time and RSS')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--calls', type=int, default=100,
                        help='timed calls per workload (default 100)')
    parser.add_argument('--warmup', type=int, default=20)
    parser.add_argument('--deadline', type=float, default=10.0,
                        help='per-call deadline in seconds (default 10, as in the brief)')
    parser.add_argument('--startup-timeout', type=float, default=300.0)
    parser.add_argument('--smoke', action='store_true', help='also run smoke_test.py')
    parser.add_argument('--out', type=Path, default=None, help='write the JSON report here')
    args = parser.parse_args(argv)

    base = args.url.rsplit('/mcp', 1)[0] if args.url else f'http://{args.host}:{args.port}'
    mcp_url = args.url or f'{base}/mcp'
    health_url = f'{base}/healthz'

    report: dict[str, Any] = {
        'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'python': sys.version.split()[0],
    }
    proc: subprocess.Popen[bytes] | None = None
    log_path = ROOT / 'analysis/bench_server.log'
    try:
        if args.manage:
            proc = start_server(args.host, args.port, log_path)
        startup_seconds, health = wait_for_health(health_url, args.startup_timeout, proc)
        rss_after_startup = read_rss_kb(proc.pid) if proc else None

        bench_report = asyncio.run(bench(mcp_url, args.calls, args.warmup, args.deadline))
        report['bench'] = bench_report

        if proc:
            report['server'] = {
                'managed': True,
                'startup_seconds': round(startup_seconds, 2),
                'products': health.get('products'),
                'rss_mb_after_startup': round((rss_after_startup or 0) / 1024, 1),
                'rss_mb_after_bench': round((read_rss_kb(proc.pid) or 0) / 1024, 1),
                'peak_rss_mb': round((peak_rss_kb(proc.pid) or 0) / 1024, 1),
                'server_log': str(log_path),
            }
        else:
            report['server'] = {'managed': False,
                                'startup_seconds': round(startup_seconds, 2),
                                'products': health.get('products')}
        if args.smoke:
            report['smoke_test'] = run_smoke_test(mcp_url)
    finally:
        if proc is not None:
            stop_server(proc)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print_report(report)
    if args.out:
        print(f'report -> {args.out}')

    failed = report['bench']['pooled']['errors'] > 0
    failed = failed or (args.smoke and not report.get('smoke_test', {}).get('passed'))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
