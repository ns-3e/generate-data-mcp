"""Tests for the MCP tool wrappers in generate_data_mcp.server (mocked at
`generate_data_mcp.server._client`, per contract Sec. 2/3/4/5). One happy-path
+ one failure-path test per tool at minimum, plus binary round-trip and
input-validation regressions called out by the F-004 ticket."""
import base64
import json

import httpx
import pytest
from unittest.mock import MagicMock, patch

from generate_data_mcp.server import (
    gd_design_schema,
    gd_generate_dataset,
    gd_generate_project,
    gd_get_field_type_options,
    gd_get_usage,
    gd_list_field_types,
    gd_list_projects,
)

_ERROR_CODES = {
    'invalid_input', 'auth_failed', 'tier_forbidden', 'rate_limited',
    'not_found', 'unsupported_format', 'upstream_error', 'upstream_unavailable',
}


def _http_status_error(status_code: int, body: dict) -> httpx.HTTPStatusError:
    request = httpx.Request('GET', 'http://test/x')
    response = httpx.Response(status_code, json=body, request=request)
    return httpx.HTTPStatusError(f'{status_code} error', request=request, response=response)


def _assert_ok_envelope(result: str) -> dict:
    parsed = json.loads(result)
    assert parsed['ok'] is True
    assert isinstance(parsed['summary'], str) and parsed['summary']
    assert 'data' in parsed
    return parsed


def _assert_err_envelope(result: str, api_key: str = '00000000-0000-0000-0000-000000000001') -> dict:
    parsed = json.loads(result)
    assert parsed['ok'] is False
    assert parsed['error']['code'] in _ERROR_CODES
    assert api_key not in parsed['error']['message']
    assert api_key not in json.dumps(parsed['error'].get('details', {}))
    return parsed


# ---------------------------------------------------------------------------
# gd_generate_dataset
# ---------------------------------------------------------------------------

def test_gd_generate_dataset_success_text():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = 'first_name\nAlice\nBob'

    with patch('generate_data_mcp.server._client') as mock_client:
        mock_client.return_value.generate_data.return_value = mock_response
        result = gd_generate_dataset(fields=[{'name': 'first_name', 'type': 'first_name', 'options': {}}], num_rows=2)

    parsed = _assert_ok_envelope(result)
    assert parsed['data']['encoding'] == 'utf-8'
    assert parsed['data']['content'] == 'first_name\nAlice\nBob'


def test_gd_generate_dataset_binary_round_trip():
    """Regression for F-001/B-001: zip bytes must survive base64 round-trip
    unmangled, never force-decoded as utf-8."""
    raw = b'PK\x03\x04\xff\xfe\x00\x01binary-payload\x80\x81\x82'
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = raw

    with patch('generate_data_mcp.server._client') as mock_client:
        mock_client.return_value.generate_data.return_value = mock_response
        result = gd_generate_dataset(fields=[{'name': 'x', 'type': 'first_name', 'options': {}}], format='zip')

    parsed = _assert_ok_envelope(result)
    assert parsed['data']['encoding'] == 'base64'
    assert parsed['data']['format'] == 'zip'
    decoded = base64.b64decode(parsed['data']['content_base64'])
    assert decoded == raw


def test_gd_generate_dataset_unsupported_format_never_calls_client():
    with patch('generate_data_mcp.server._client') as mock_client:
        result = gd_generate_dataset(fields=[], format='yaml')

    parsed = _assert_err_envelope(result)
    assert parsed['error']['code'] == 'unsupported_format'
    mock_client.assert_not_called()


def test_gd_generate_dataset_upstream_unavailable():
    with patch('generate_data_mcp.server._client') as mock_client:
        mock_client.return_value.generate_data.side_effect = httpx.ConnectError('boom')
        result = gd_generate_dataset(fields=[{'name': 'x', 'type': 'first_name', 'options': {}}])

    parsed = _assert_err_envelope(result)
    assert parsed['error']['code'] == 'upstream_unavailable'
    assert parsed['error']['retriable'] is True


def test_gd_generate_dataset_rate_limited_status():
    mock_response = MagicMock()
    mock_response.status_code = 429
    mock_response.json.return_value = {'error': 'Rate limit exceeded'}

    with patch('generate_data_mcp.server._client') as mock_client:
        mock_client.return_value.generate_data.return_value = mock_response
        result = gd_generate_dataset(fields=[{'name': 'x', 'type': 'first_name', 'options': {}}])

    parsed = _assert_err_envelope(result)
    assert parsed['error']['code'] == 'rate_limited'
    assert parsed['error']['retriable'] is True


# ---------------------------------------------------------------------------
# gd_list_field_types
# ---------------------------------------------------------------------------

def test_gd_list_field_types_success():
    with patch('generate_data_mcp.server._client') as mock_client:
        mock_client.return_value.list_field_types.return_value = {'categories': {'personal': ['first_name']}}
        result = gd_list_field_types()

    parsed = _assert_ok_envelope(result)
    assert parsed['data']['categories'] == {'personal': ['first_name']}


def test_gd_list_field_types_auth_failed():
    with patch('generate_data_mcp.server._client') as mock_client:
        mock_client.return_value.list_field_types.side_effect = _http_status_error(401, {'error': 'bad key'})
        result = gd_list_field_types()

    parsed = _assert_err_envelope(result)
    assert parsed['error']['code'] == 'auth_failed'


# ---------------------------------------------------------------------------
# gd_get_field_type_options
# ---------------------------------------------------------------------------

def test_gd_get_field_type_options_success():
    with patch('generate_data_mcp.server._client') as mock_client:
        mock_client.return_value.get_field_options.return_value = {'options': [{'name': 'domain'}]}
        result = gd_get_field_type_options('email')

    parsed = _assert_ok_envelope(result)
    assert parsed['data']['options'] == [{'name': 'domain'}]


@pytest.mark.parametrize('bad_value', ['../etc/passwd', 'a/b', 'a\\b', 'a b', 'a%2e', ''])
def test_gd_get_field_type_options_path_validation_rejects_without_calling_client(bad_value):
    """Contract Sec. 7: reject before building the URL; upstream never called."""
    with patch('generate_data_mcp.server._client') as mock_client:
        result = gd_get_field_type_options(bad_value)

    parsed = _assert_err_envelope(result)
    assert parsed['error']['code'] == 'invalid_input'
    mock_client.assert_not_called()


def test_gd_get_field_type_options_not_found():
    """Finding 2: a 404 maps to `not_found`, not the generic `upstream_error`."""
    with patch('generate_data_mcp.server._client') as mock_client:
        mock_client.return_value.get_field_options.side_effect = _http_status_error(404, {'error': 'unknown field type'})
        result = gd_get_field_type_options('email')

    parsed = _assert_err_envelope(result)
    assert parsed['error']['code'] == 'not_found'
    assert parsed['error']['retriable'] is False


def test_gd_get_field_type_options_upstream_error():
    with patch('generate_data_mcp.server._client') as mock_client:
        mock_client.return_value.get_field_options.side_effect = _http_status_error(500, {'error': 'oops'})
        result = gd_get_field_type_options('email')

    parsed = _assert_err_envelope(result)
    assert parsed['error']['code'] == 'upstream_error'


# ---------------------------------------------------------------------------
# gd_design_schema
# ---------------------------------------------------------------------------

def test_gd_design_schema_propose_success():
    with patch('generate_data_mcp.server._client') as mock_client:
        mock_client.return_value.propose_schema.return_value = {'proposed_schema': {'fields': []}}
        result = gd_design_schema(prompt='customers with email')

    parsed = _assert_ok_envelope(result)
    assert parsed['data']['proposed_schema'] == {'fields': []}
    mock_client.return_value.propose_schema.assert_called_once_with('customers with email', locale='en_US')
    mock_client.return_value.refine_schema.assert_not_called()


def test_gd_design_schema_refine_success():
    messages = [{'role': 'user', 'content': 'add VAT'}]
    current = {'fields': [{'name': 'x'}]}
    with patch('generate_data_mcp.server._client') as mock_client:
        mock_client.return_value.refine_schema.return_value = {'proposed_schema': {'fields': [{'name': 'x'}, {'name': 'vat'}]}}
        result = gd_design_schema(prompt='irrelevant here', messages=messages, current_schema=current)

    parsed = _assert_ok_envelope(result)
    mock_client.return_value.refine_schema.assert_called_once_with(messages, current)
    mock_client.return_value.propose_schema.assert_not_called()
    assert len(parsed['data']['proposed_schema']['fields']) == 2


def test_gd_design_schema_upstream_unavailable():
    with patch('generate_data_mcp.server._client') as mock_client:
        mock_client.return_value.propose_schema.side_effect = httpx.ConnectTimeout('timeout')
        result = gd_design_schema(prompt='customers')

    parsed = _assert_err_envelope(result)
    assert parsed['error']['code'] == 'upstream_unavailable'


# ---------------------------------------------------------------------------
# gd_get_usage
# ---------------------------------------------------------------------------

def test_gd_get_usage_success():
    with patch('generate_data_mcp.server._client') as mock_client:
        mock_client.return_value.get_usage.return_value = {'tier': 'FREE', 'calls_today': 3}
        result = gd_get_usage()

    parsed = _assert_ok_envelope(result)
    assert parsed['data']['tier'] == 'FREE'
    assert 'FREE' in parsed['summary']


def test_gd_get_usage_auth_failed():
    with patch('generate_data_mcp.server._client') as mock_client:
        mock_client.return_value.get_usage.side_effect = _http_status_error(403, {'error': 'invalid credentials'})
        result = gd_get_usage()

    parsed = _assert_err_envelope(result)
    assert parsed['error']['code'] == 'auth_failed'


# ---------------------------------------------------------------------------
# gd_list_projects
# ---------------------------------------------------------------------------

def test_gd_list_projects_pagination():
    items = [{'id': i, 'name': f'project-{i}'} for i in range(1, 6)]
    with patch('generate_data_mcp.server._client') as mock_client:
        mock_client.return_value.list_projects.return_value = items
        result = gd_list_projects(limit=2, offset=1)

    parsed = _assert_ok_envelope(result)
    data = parsed['data']
    assert data['limit'] == 2
    assert data['offset'] == 1
    assert data['total_count'] == 5
    assert data['has_more'] is True
    assert data['next_offset'] == 3
    assert [p['id'] for p in data['items']] == [2, 3]


def test_gd_list_projects_invalid_pagination_never_calls_client():
    with patch('generate_data_mcp.server._client') as mock_client:
        result = gd_list_projects(limit=0, offset=0)

    parsed = _assert_err_envelope(result)
    assert parsed['error']['code'] == 'invalid_input'
    mock_client.assert_not_called()


def test_gd_list_projects_tier_forbidden():
    with patch('generate_data_mcp.server._client') as mock_client:
        mock_client.return_value.list_projects.side_effect = _http_status_error(403, {'error': 'Requires Premium tier'})
        result = gd_list_projects()

    parsed = _assert_err_envelope(result)
    assert parsed['error']['code'] == 'tier_forbidden'


# ---------------------------------------------------------------------------
# gd_generate_project
# ---------------------------------------------------------------------------

def test_gd_generate_project_success_text():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = 'table,rows\ncustomers,10'

    with patch('generate_data_mcp.server._client') as mock_client:
        mock_client.return_value.generate_project.return_value = mock_response
        result = gd_generate_project(project_id=7, format='csv')

    parsed = _assert_ok_envelope(result)
    assert parsed['data']['content'] == 'table,rows\ncustomers,10'


def test_gd_generate_project_binary_round_trip():
    """Regression for F-001/B-001, project export path."""
    raw = b'PK\x03\x04\xff\xfe\x00\x01project-zip-bytes\x80\x81\x82'
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = raw

    with patch('generate_data_mcp.server._client') as mock_client:
        mock_client.return_value.generate_project.return_value = mock_response
        result = gd_generate_project(project_id=7, format='zip')

    parsed = _assert_ok_envelope(result)
    assert parsed['data']['encoding'] == 'base64'
    decoded = base64.b64decode(parsed['data']['content_base64'])
    assert decoded == raw


def test_gd_generate_project_invalid_id_never_calls_client():
    with patch('generate_data_mcp.server._client') as mock_client:
        result = gd_generate_project(project_id=-1)

    parsed = _assert_err_envelope(result)
    assert parsed['error']['code'] == 'invalid_input'
    mock_client.assert_not_called()


def test_gd_generate_project_unsupported_format_never_calls_client():
    with patch('generate_data_mcp.server._client') as mock_client:
        result = gd_generate_project(project_id=7, format='yaml')

    parsed = _assert_err_envelope(result)
    assert parsed['error']['code'] == 'unsupported_format'
    mock_client.assert_not_called()


def test_gd_generate_project_upstream_error_status():
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.json.return_value = {'error': 'internal'}

    with patch('generate_data_mcp.server._client') as mock_client:
        mock_client.return_value.generate_project.return_value = mock_response
        result = gd_generate_project(project_id=7)

    parsed = _assert_err_envelope(result)
    assert parsed['error']['code'] == 'upstream_error'


# ---------------------------------------------------------------------------
# Registration sanity (replaces placebo test_tool_endpoint_mapping, which
# asserted a hardcoded dict against itself and exercised no real code)
# ---------------------------------------------------------------------------

def test_all_seven_tools_registered_with_fastmcp():
    import asyncio
    from generate_data_mcp.server import mcp

    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    assert names == {
        'gd_generate_dataset', 'gd_list_field_types', 'gd_get_field_type_options',
        'gd_design_schema', 'gd_get_usage', 'gd_list_projects', 'gd_generate_project',
    }


# ---------------------------------------------------------------------------
# E2E composability (contract Sec. 7 brief / Principle 7): tools must chain,
# not just work in isolation.
# ---------------------------------------------------------------------------

def test_e2e_discover_field_type_then_generate_dataset():
    with patch('generate_data_mcp.server._client') as mock_client:
        mock_client.return_value.list_field_types.return_value = {
            'categories': {'personal': ['first_name', 'last_name']}
        }
        catalog_result = json.loads(gd_list_field_types())
        assert catalog_result['ok'] is True
        chosen_type = catalog_result['data']['categories']['personal'][0]
        assert chosen_type == 'first_name'

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = f'{chosen_type}\nAlice\nBob'
        mock_client.return_value.generate_data.return_value = mock_response

        dataset_result = json.loads(
            gd_generate_dataset(fields=[{'name': chosen_type, 'type': chosen_type, 'options': {}}], num_rows=2)
        )

    assert dataset_result['ok'] is True
    assert dataset_result['data']['content'] == f'{chosen_type}\nAlice\nBob'
    assert dataset_result['data']['encoding'] == 'utf-8'
