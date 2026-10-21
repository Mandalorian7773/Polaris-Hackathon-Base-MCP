"""Extended live MCP acceptance checks.

Usage: python protocol_acceptance_test.py http://127.0.0.1:8000/mcp
"""
import asyncio
import json
import sys
import time
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


ROOT = Path(__file__).resolve().parent
PRODUCTS = json.loads((ROOT / 'data/products.json').read_text(encoding='utf-8'))
BY_ID = {product['id']: product for product in PRODUCTS}


async def call(session, arguments):
    result = await session.call_tool('search_products', arguments)
    assert not result.isError, result
    output = result.structuredContent
    assert json.loads(result.content[0].text) == output
    ids = [item['id'] for item in output['results']]
    recommendation_ids = [item['id'] for item in output['recommendations']]
    assert len(ids) == len(set(ids))
    assert len(recommendation_ids) == len(set(recommendation_ids))
    assert all(product_id in BY_ID for product_id in ids + recommendation_ids)
    assert all(item['for_product_id'] in ids for item in output['recommendations'])
    assert not set(ids).intersection(recommendation_ids)
    return output


async def main(url):
    async with streamable_http_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            initialized = await session.initialize()
            assert initialized.protocolVersion == '2025-11-25'

            mixed = await call(session, {
                'query': 'black jeans and white t-shirts under INR 2000 each',
                'limit': 10,
            })
            mixed_products = [BY_ID[item['id']] for item in mixed['results']]
            assert any(p['product_type'] == 'Trousers' and p['pattern'] == 'Denim' and
                       p['color'] == 'Black' and p['price']['amount'] <= 2000
                       for p in mixed_products)
            assert any(p['product_type'] == 'T-shirt' and p['color'] == 'White' and
                       p['price']['amount'] <= 2000 for p in mixed_products)

            recommendation = await call(session, {
                'query': 'dress under INR 2000 with a matching bag under INR 1000',
                'limit': 5,
                'include_recommendations': True,
            })
            assert recommendation['recommendations']
            assert all(BY_ID[item['id']]['category'] == 'Bags' and
                       BY_ID[item['id']]['price']['amount'] <= 1000
                       for item in recommendation['recommendations'])

            irrelevant = await call(session, {'query': 'spaceship engine'})
            assert irrelevant == {'results': [], 'recommendations': []}

            page = await call(session, {
                'filters': {'category': 'Shoes'}, 'sort_by': 'price',
                'sort_order': 'asc', 'offset': 5, 'limit': 5,
            })
            repeated = await call(session, {
                'filters': {'category': 'Shoes'}, 'sort_by': 'price',
                'sort_order': 'asc', 'offset': 5, 'limit': 5,
            })
            prefix = await call(session, {
                'filters': {'category': 'Shoes'}, 'sort_by': 'price',
                'sort_order': 'asc', 'limit': 10,
            })
            assert page == repeated
            assert page['results'] == prefix['results'][5:10]

            for arguments in (
                    {'limit': 0}, {'filters': {'color': ' '}},
                    {'filters': {'min_price': 2, 'max_price': 1}},
                    {'unexpected': True}):
                result = await session.call_tool('search_products', arguments)
                assert result.isError, result

            workload = [
                {'filters': {'color': 'Black', 'max_price': 2000},
                 'sort_by': 'price', 'sort_order': 'asc', 'limit': 10},
                {'filters': {'category': 'Shoes'}, 'sort_by': 'rating',
                 'sort_order': 'desc', 'limit': 10},
                {'query': 'cotton top', 'limit': 10},
                {'query': 'red dress and black bag', 'limit': 10},
                {'query': 'dress with a matching bag',
                 'include_recommendations': True, 'limit': 5},
            ] * 4
            started = time.perf_counter()
            concurrent = await asyncio.gather(*(call(session, args) for args in workload))
            elapsed = time.perf_counter() - started
            assert len(concurrent) == 20
            print(f'PASS: extended MCP protocol, constraints, pagination, '
                  f'mixed choices, recommendations, invalid calls, and '
                  f'20-call concurrency ({elapsed:.3f}s)')


if __name__ == '__main__':
    asyncio.run(asyncio.wait_for(
        main(sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:8000/mcp'),
        timeout=120,
    ))
