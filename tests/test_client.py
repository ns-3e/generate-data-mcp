"""Tests for GenerateDataClient (HTTP layer, mocked at the instance's reused
httpx.Client — `client._http` — per contract Sec. 8 / brief F-008, not the old
module-level httpx.get/post pattern)."""
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


def test_default_base_url_is_a_routed_host():
    with patch.dict('os.environ', {}, clear=True):
        assert GenerateDataClient(api_key='k').base_url == 'https://generate-data.com'


def test_client_reuses_one_httpx_client(client):
    """brief F-008: one reused httpx.Client per GenerateDataClient instance."""
    import httpx
    assert isinstance(client._http, httpx.Client)


def test_generate_data_posts_to_v2(client):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = 'first_name\nAlice'
    mock_response.headers = {'content-type': 'text/csv'}

    with patch.object(client._http, 'post', return_value=mock_response) as post:
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

    with patch.object(client._http, 'get', return_value=mock_response) as get:
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

    with patch.object(client._http, 'get', return_value=mock_response) as get:
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

    with patch.object(client._http, 'post', return_value=mock_response) as post:
        data = client.propose_schema('customers with email')

    post.assert_called_once()
    assert post.call_args[1]['json']['prompt'] == 'customers with email'
    assert 'proposed_schema' in data


def test_refine_schema(client):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {'proposed_schema': []}

    messages = [{'role': 'user', 'content': 'add VAT field'}]
    schema = [{'name': 'id', 'type': 'row_number', 'options': {}}]
    with patch.object(client._http, 'post', return_value=mock_response) as post:
        data = client.refine_schema(messages, schema)

    post.assert_called_once()
    body = post.call_args[1]['json']
    assert body['messages'] == messages
    assert body['schema'] == schema
    assert 'current_schema' not in body
    assert 'proposed_schema' in data


def test_get_usage(client):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {'calls_today': 3, 'tier': 'FREE'}

    with patch.object(client._http, 'get', return_value=mock_response) as get:
        data = client.get_usage()

    get.assert_called_once_with(
        'http://test/api/v2/api-keys/usage',
        headers={'X-API-Key': '00000000-0000-0000-0000-000000000001', 'Accept': 'application/json'},
        timeout=30,
    )
    assert data['tier'] == 'FREE'


def test_list_projects(client):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = [{'id': 1}, {'id': 2}]

    with patch.object(client._http, 'get', return_value=mock_response) as get:
        data = client.list_projects()

    get.assert_called_once_with(
        'http://test/api/v2/projects',
        headers={'X-API-Key': '00000000-0000-0000-0000-000000000001', 'Accept': 'application/json'},
        timeout=30,
    )
    assert data == [{'id': 1}, {'id': 2}]


def test_generate_data_binary_format_sends_accept_star(client):
    """Finding 1: binary generate calls send Accept: */* not application/json,
    so an upstream honoring Accept doesn't coerce the binary body to JSON."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = b'PK\x03\x04fake-zip-bytes'
    mock_response.headers = {'content-type': 'application/zip'}

    with patch.object(client._http, 'post', return_value=mock_response) as post:
        client.generate_data([{'name': 'first_name', 'type': 'first_name', 'options': {}}], fmt='parquet')

    assert post.call_args[1]['headers']['Accept'] == '*/*'


def test_generate_data_text_format_keeps_accept_json(client):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = 'first_name\nAlice'
    mock_response.headers = {'content-type': 'text/csv'}

    with patch.object(client._http, 'post', return_value=mock_response) as post:
        client.generate_data([{'name': 'first_name', 'type': 'first_name', 'options': {}}], fmt='csv')

    assert post.call_args[1]['headers']['Accept'] == 'application/json'


def test_generate_project_sends_format_in_body_not_query(client):
    """`?format=` is DRF renderer selection: the real API answers 404 to it."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = b'PK\x03\x04fake-zip-bytes'
    mock_response.headers = {'content-type': 'application/zip'}

    with patch.object(client._http, 'post', return_value=mock_response) as post:
        resp = client.generate_project(42, fmt='zip')

    post.assert_called_once()
    args, kwargs = post.call_args
    assert args[0] == 'http://test/api/v2/projects/42/generate'
    assert kwargs['json'] == {'format': 'zip'}
    assert 'params' not in kwargs
    assert kwargs['headers']['Accept'] == '*/*'
    assert resp.status_code == 200
