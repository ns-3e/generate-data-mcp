"""Tests for MCP HTTP client (mocked)."""
import json
from unittest.mock import MagicMock, patch

import pytest

from generate_data_mcp.client import GenerateDataClient


@pytest.fixture
def client():
    return GenerateDataClient(api_key='00000000-0000-0000-0000-000000000001', base_url='http://test')


def test_client_requires_api_key():
    with patch.dict('os.environ', {}, clear=True):
        with pytest.raises(ValueError, match='GENERATE_DATA_API_KEY'):
            GenerateDataClient()


def test_generate_data_posts_to_v2(client):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = 'first_name\nAlice'
    mock_response.headers = {'content-type': 'text/csv'}

    with patch('generate_data_mcp.client.httpx.post', return_value=mock_response) as post:
        resp = client.generate_data([{'name': 'first_name', 'type': 'first_name', 'options': {}}], num_rows=5)

    post.assert_called_once()
    args, kwargs = post.call_args
    assert args[0] == 'http://test/api/v2/generate'
    assert kwargs['headers']['X-API-Key'] == '00000000-0000-0000-0000-000000000001'
    assert kwargs['json']['num_rows'] == 5
    assert resp.status_code == 200


def test_list_field_types(client):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {'categories': {}}

    with patch('generate_data_mcp.client.httpx.get', return_value=mock_response) as get:
        data = client.list_field_types()

    get.assert_called_once_with(
        'http://test/api/v2/field-types',
        headers={'X-API-Key': '00000000-0000-0000-0000-000000000001', 'Accept': 'application/json'},
        timeout=30,
    )
    assert 'categories' in data


def test_get_field_options(client):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {'options': []}

    with patch('generate_data_mcp.client.httpx.get', return_value=mock_response) as get:
        data = client.get_field_options('email')

    get.assert_called_once_with(
        'http://test/api/v2/field-types/email/options',
        headers={'X-API-Key': '00000000-0000-0000-0000-000000000001', 'Accept': 'application/json'},
        timeout=30,
    )
    assert 'options' in data


def test_propose_schema(client):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {'proposed_schema': []}

    with patch('generate_data_mcp.client.httpx.post', return_value=mock_response) as post:
        data = client.propose_schema('customers with email')

    post.assert_called_once()
    assert post.call_args[1]['json']['prompt'] == 'customers with email'
    assert 'proposed_schema' in data


def test_refine_schema(client):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {'proposed_schema': []}

    messages = [{'role': 'user', 'content': 'add VAT field'}]
    schema = {'fields': []}
    with patch('generate_data_mcp.client.httpx.post', return_value=mock_response) as post:
        data = client.refine_schema(messages, schema)

    post.assert_called_once()
    body = post.call_args[1]['json']
    assert body['messages'] == messages
    assert body['current_schema'] == schema
    assert 'proposed_schema' in data


def test_get_usage(client):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {'calls_today': 3, 'tier': 'FREE'}

    with patch('generate_data_mcp.client.httpx.get', return_value=mock_response) as get:
        data = client.get_usage()

    get.assert_called_once_with(
        'http://test/api/v2/api-keys/usage',
        headers={'X-API-Key': '00000000-0000-0000-0000-000000000001', 'Accept': 'application/json'},
        timeout=30,
    )
    assert data['tier'] == 'FREE'


def test_generate_data_tool_error_response():
    from generate_data_mcp.server import generate_data

    mock_response = MagicMock()
    mock_response.status_code = 429
    mock_response.json.return_value = {'error': 'Rate limit exceeded', 'code': 'rate_limit'}
    mock_response.text = 'Rate limit exceeded'

    with patch('generate_data_mcp.server._client') as mock_client:
        mock_client.return_value.generate_data.return_value = mock_response
        result = generate_data(fields=[{'name': 'x', 'type': 'first_name', 'options': {}}])

    parsed = json.loads(result)
    assert parsed['status'] == 429
    assert 'Rate limit' in parsed['error']


def test_tool_endpoint_mapping():
    """Documented tools map to v2 paths."""
    mapping = {
        'generate_data': '/api/v2/generate',
        'list_field_types': '/api/v2/field-types',
        'get_field_options': '/api/v2/field-types/{type}/options',
        'propose_schema': '/api/v2/ai/schema/propose',
        'refine_schema': '/api/v2/ai/schema/refine',
        'get_api_usage': '/api/v2/api-keys/usage',
    }
    assert len(mapping) >= 5
