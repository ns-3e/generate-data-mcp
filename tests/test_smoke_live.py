"""Optional smoke test against a real deployment, run nightly against QA.

Skipped unless GENERATE_DATA_SMOKE_API_KEY is set. Uses a few API calls of that
key's daily quota and creates nothing.
"""
import asyncio
import json
import os

import pytest

from generate_data_mcp import server

pytestmark = pytest.mark.skipif(
    not os.environ.get('GENERATE_DATA_SMOKE_API_KEY'),
    reason='GENERATE_DATA_SMOKE_API_KEY is not set',
)


@pytest.fixture(autouse=True)
def _live_client(monkeypatch):
    monkeypatch.setenv('GENERATE_DATA_API_KEY', os.environ.get('GENERATE_DATA_SMOKE_API_KEY', ''))
    monkeypatch.setattr(server, '_client_singleton', None)


def _envelope(tool, **kwargs) -> dict:
    return json.loads(asyncio.run(tool(**kwargs)).content[0].text)


def test_usage_and_field_types():
    assert _envelope(server.gd_get_usage)['ok'] is True
    server._client_singleton = None  # its connection pool belongs to the last loop
    assert _envelope(server.gd_list_field_types)['data']['categories']


def test_generate_dataset_returns_requested_columns():
    fields = [{'name': 'id', 'type': 'row_number', 'options': {}}]
    result = _envelope(server.gd_generate_dataset, fields=fields, num_rows=2)
    assert result['ok'] is True, result
    assert result['data']['content'].startswith('id')
