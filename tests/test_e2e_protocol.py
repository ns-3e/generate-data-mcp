"""End-to-end tests: a real MCP client talks stdio to the real server process,
which talks HTTP to a stub that reproduces the Generate-Data.com API's actual
request rules. Mocked unit tests passed while `gd_generate_project` and refine
failed on every real call, so these assert the wire contract itself.
"""
import asyncio
import base64
import io
import json
import os
import sys
import subprocess
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
import generate_data_mcp
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

API_KEY = 'e2e-test-key'
FIELDS = [{'name': 'id', 'type': 'row_number', 'options': {}}]
PROJECT = {
    'name': 'demo',
    'tables': [{'name': 'customers', 'num_rows': 2, 'schema': FIELDS}],
    'relationships': [],
}


def _zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('customers.csv', 'id\n1\n')
    return buffer.getvalue()


class FakeApi(BaseHTTPRequestHandler):
    """Applies the same rules as the DRF views the MCP client calls."""

    override = None  # (status, body, content_type[, headers]) forced for failure-mode tests
    delay = 0  # seconds to wait before answering
    sequence = []  # one-shot overrides, used in order before `override`
    requests = []  # (method, path, parsed body, headers) of every request received

    def log_message(self, *args):
        pass

    def _send(self, status, body=b'', content_type='application/json', headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _handle(self):
        length = int(self.headers.get('content-length') or 0)
        raw = self.rfile.read(length)
        body = json.loads(raw) if raw else {}
        FakeApi.requests.append((self.command, self.path, body, dict(self.headers)))
        if FakeApi.delay:
            time.sleep(FakeApi.delay)
        if FakeApi.sequence:
            return self._send(*FakeApi.sequence.pop(0))
        if FakeApi.override:
            return self._send(*FakeApi.override)
        if self.headers.get('X-API-Key') != API_KEY:
            return self._send(401, {'detail': 'Invalid API key'})
        url = urlsplit(self.path)
        # DRF treats ?format= as renderer selection: unknown renderer means 404.
        if parse_qs(url.query).get('format', ['json'])[0] not in ('json', 'api'):
            return self._send(404, {'detail': 'Not found.'})
        return self._route(self.command, url.path, body)

    def _route(self, method, path, body):
        if path == '/api/v2/generate':
            rows = body.get('num_rows', 1)
            csv = 'id\n' + ''.join(f'{i + 1}\n' for i in range(rows))
            return self._send(200, csv.encode(), 'text/csv', {
                'X-Usage-Rows-Generated': str(rows),
                'X-Usage-Calls-Today': '4',
                'X-Usage-Calls-Remaining': '6',
            })
        if path == '/api/v2/field-types/word/validate':
            problems = [] if body.get('options') == {'ok': True} else ['bad option']
            return self._send(200, {'is_valid': not problems, 'errors': problems})
        if path == '/api/v2/ai/project/propose':
            if not (body.get('prompt') or '').strip():
                return self._send(400, {'error': 'prompt is required'})
            return self._send(200, {'project': {**PROJECT, 'settings': {}, 'domain_pack': ''}})
        if path == '/api/v2/projects/generate-inline':
            if not body.get('tables'):
                return self._send(400, {'error': 'tables are required'})
            if body.get('format') == 'bundle':
                return self._send(200, {'tables': {t['name']: [] for t in body['tables']}})
            return self._send(200, _zip_bytes(), 'application/zip')
        if path == '/api/v2/projects/7' and method == 'GET':
            return self._send(200, {'id': 7, **PROJECT})
        if path == '/api/v2/projects/7' and method == 'PUT':
            return self._send(200, {'id': 7, **PROJECT, **body})
        if path == '/api/v2/projects/7' and method == 'DELETE':
            return self._send(204)
        if path == '/api/v2/projects' and method == 'POST':
            if not (body.get('name') or '').strip():
                return self._send(400, {'error': 'name is required'})
            if any(r.get('from_table') == 'missing' for r in body.get('relationships') or []):
                return self._send(400, {
                    'error': 'Invalid relationships',
                    'details': ['relationships[0]: unknown from_table'],
                })
            return self._send(201, {'id': 8, **body})
        if path == '/api/v2/field-types':
            return self._send(200, {'categories': {'Basic': [{'value': 'word'}]}})
        if path == '/api/v2/field-types/word/options':
            return self._send(200, {'type_options': {}})
        if path == '/api/v2/api-keys/usage':
            return self._send(200, {'tier': 'FREE', 'calls_today': 1})
        if path == '/api/v2/ai/schema/propose':
            return self._send(200, {'proposed_schema': FIELDS})
        if path == '/api/v2/ai/schema/refine':
            if not isinstance(body.get('schema'), list):
                return self._send(422, {'error': 'schema must be an array'})
            return self._send(200, {'proposed_schema': body['schema'] + FIELDS})
        if path == '/api/v2/projects':
            return self._send(200, [{'id': 7, 'name': 'demo'}])
        if path == '/api/v2/projects/7/generate':
            fmt = body.get('format', 'zip')
            if fmt == 'bundle':
                return self._send(200, {'tables': {'customers': [{'id': 1}]}})
            return self._send(200, _zip_bytes(), 'application/zip')
        return self._send(404, {'detail': 'Not found.'})

    do_GET = do_POST = do_PUT = do_DELETE = _handle


@pytest.fixture(scope='module')
def api_url():
    server = ThreadingHTTPServer(('127.0.0.1', 0), FakeApi)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_address[1]}'
    server.shutdown()


@pytest.fixture(autouse=True)
def _reset_stub():
    FakeApi.override = None
    FakeApi.delay = 0
    FakeApi.sequence = []
    FakeApi.requests = []
    yield
    FakeApi.override = None
    FakeApi.delay = 0
    FakeApi.sequence = []


@pytest.fixture(autouse=True)
def output_dir(tmp_path, monkeypatch):
    path = tmp_path / 'output'
    monkeypatch.setenv('GENERATE_DATA_OUTPUT_DIR', str(path))
    return path


def _server_params(api_url, key=API_KEY):
    env = {
        'PATH': os.environ.get('PATH', ''),
        'GENERATE_DATA_API_BASE_URL': api_url,
        'GENERATE_DATA_OUTPUT_DIR': os.environ['GENERATE_DATA_OUTPUT_DIR'],
    }
    if key is not None:
        env['GENERATE_DATA_API_KEY'] = key
    return StdioServerParameters(command=sys.executable, args=['-m', 'generate_data_mcp.server'], env=env)


def _with_session(api_url, body, key=API_KEY):
    """Run `body(session)` inside one stdio session."""

    async def run():
        async with stdio_client(_server_params(api_url, key)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await body(session)

    return asyncio.run(run())


def _call_results(api_url, *calls, key=API_KEY):
    """Run tool calls over one stdio session; return (isError, envelope) pairs."""

    async def body(session):
        results = []
        for name, arguments in calls:
            result = await session.call_tool(name, arguments)
            # json.loads fails here if a raw "Error executing tool" string leaks.
            results.append((result.isError, json.loads(result.content[0].text)))
        return results

    return _with_session(api_url, body, key)


def _call_tools(api_url, *calls, key=API_KEY):
    return [envelope for _, envelope in _call_results(api_url, *calls, key=key)]


def test_project_zip_arrives_intact(api_url):
    [result] = _call_tools(api_url, ('gd_generate_project', {'project_id': 7}))
    assert result['ok'] is True
    archive = zipfile.ZipFile(io.BytesIO(base64.b64decode(result['data']['content_base64'])))
    assert archive.namelist() == ['customers.csv']


def test_project_text_format_is_sent_in_the_body(api_url):
    [result] = _call_tools(api_url, ('gd_generate_project', {'project_id': 7, 'format': 'bundle'}))
    assert json.loads(result['data']['content'])['tables']['customers'] == [{'id': 1}]


def test_propose_output_can_be_refined_then_generated(api_url):
    [proposed] = _call_tools(api_url, ('gd_design_schema', {'prompt': 'customers'}))
    schema = proposed['data']['proposed_schema']
    refined, generated = _call_tools(
        api_url,
        ('gd_design_schema', {'messages': [{'role': 'user', 'content': 'more'}], 'current_schema': schema}),
        ('gd_generate_dataset', {'fields': schema, 'num_rows': 1}),
    )
    assert len(refined['data']['proposed_schema']) == 2
    assert generated['data']['content'] == 'id\n1\n'


def test_list_projects_then_generate(api_url):
    listed, generated = _call_tools(
        api_url,
        ('gd_list_projects', {}),
        ('gd_generate_project', {'project_id': 7}),
    )
    assert listed['data']['items'][0]['id'] == 7
    assert generated['ok'] is True


def test_invalid_key_reports_auth_failed_and_never_echoes_it(api_url):
    secret = 'wrong-secret-key-value'
    [result] = _call_tools(api_url, ('gd_list_projects', {}), key=secret)
    assert result['error']['code'] == 'auth_failed'
    assert secret not in json.dumps(result)


@pytest.mark.parametrize('override', [
    (301, b'Moved Permanently', 'text/plain'),
    (200, b'', 'text/csv'),
    (200, b'<html>challenge</html>', 'text/html'),
], ids=['redirect', 'empty', 'html'])
def test_bad_upstream_is_never_reported_as_generated_data(api_url, override):
    FakeApi.override = override
    dataset, project = _call_tools(
        api_url,
        ('gd_generate_dataset', {'fields': FIELDS}),
        ('gd_generate_project', {'project_id': 7}),
    )
    assert dataset['ok'] is False and dataset['error']['code'] == 'upstream_error'
    assert project['ok'] is False and project['error']['code'] == 'upstream_error'


def _assert_failure(result, code):
    is_error, envelope = result
    assert is_error is True
    assert envelope['ok'] is False
    assert envelope['error']['code'] == code
    return envelope['error']


def test_is_error_matches_the_envelope(api_url):
    success, failure = _call_results(
        api_url,
        ('gd_get_usage', {}),
        ('gd_generate_project', {'project_id': 7, 'format': 'csv'}),
    )
    assert success[0] is False and success[1]['ok'] is True
    _assert_failure(failure, 'unsupported_format')


def test_large_result_is_saved_whole_with_true_row_count(api_url, output_dir):
    first, second = _call_tools(
        api_url,
        ('gd_generate_dataset', {'fields': FIELDS, 'num_rows': 20000}),
        ('gd_generate_dataset', {'fields': FIELDS, 'num_rows': 20000}),
    )
    data = first['data']
    assert first['ok'] is True and data['saved_to_file'] is True and data['truncated'] is False
    lines = open(data['file_path']).read().splitlines()
    assert len(lines) == 20001 and lines[-1] == '20000'
    assert data['rows'] == 20000 and '20,000 rows' in first['summary']
    assert data['preview'].startswith('id\n1\n') and 'content' not in data
    assert data['file_path'] != second['data']['file_path']
    assert len(list(output_dir.iterdir())) == 2


def test_save_to_file_forces_a_file(api_url):
    [result] = _call_tools(api_url, ('gd_generate_project', {'project_id': 7, 'save_to_file': True}))
    assert zipfile.is_zipfile(result['data']['file_path'])


def test_unwritable_output_dir_is_reported(api_url, output_dir, monkeypatch):
    output_dir.write_text('a file, not a directory')
    [result] = _call_results(api_url, ('gd_generate_dataset', {'fields': FIELDS, 'num_rows': 20000}))
    error = _assert_failure(result, 'configuration_error')
    assert 'GENERATE_DATA_OUTPUT_DIR' in error['message']


def test_payload_is_only_sent_once_on_the_wire(api_url):
    params = _server_params(api_url)
    messages = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 't', 'version': '1'},
        }},
        {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {
            'name': 'gd_generate_dataset', 'arguments': {'fields': FIELDS, 'num_rows': 5000},
        }},
    ]
    process = subprocess.Popen(
        [params.command, *params.args],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=params.env, text=True,
    )
    try:
        for message in messages:
            process.stdin.write(json.dumps(message) + '\n')
            process.stdin.flush()
        process.stdout.readline()
        line = process.stdout.readline()
    finally:
        process.kill()
        process.wait()
    result = json.loads(line)['result']
    assert 'structuredContent' not in result
    content_chars = len(result['content'][0]['text'])
    assert content_chars > 20_000
    assert len(line) < content_chars * 1.2


def test_slow_calls_run_concurrently(api_url):
    FakeApi.delay = 3

    async def body(session):
        started = time.monotonic()
        await asyncio.gather(*[session.call_tool('gd_get_usage', {}) for _ in range(3)])
        return time.monotonic() - started

    assert _with_session(api_url, body) < 5


def test_ping_answers_during_a_slow_call(api_url):
    FakeApi.delay = 3

    async def body(session):
        call = asyncio.create_task(session.call_tool('gd_get_usage', {}))
        await asyncio.sleep(0.5)
        started = time.monotonic()
        await session.send_ping()
        elapsed = time.monotonic() - started
        await call
        return elapsed

    assert _with_session(api_url, body) < 1


def test_daily_limit_is_not_retriable(api_url):
    FakeApi.override = (429, {'error': 'Daily API call limit reached (10 calls/day)', 'code': 'daily_call_limit'})
    [result] = _call_results(api_url, ('gd_generate_dataset', {'fields': FIELDS}))
    error = _assert_failure(result, 'rate_limited')
    assert error['retriable'] is False
    assert 'resets_at' in error['details']


def test_retry_after_is_surfaced(api_url):
    FakeApi.override = (429, {'detail': 'Request was throttled.'}, 'application/json', {'Retry-After': '37'})
    [result] = _call_results(api_url, ('gd_get_usage', {}))
    error = _assert_failure(result, 'rate_limited')
    assert error['retriable'] is True
    assert error['details']['retry_after'] == 37


def test_plan_limit_keeps_the_upstream_message(api_url):
    FakeApi.override = (400, {'error': 'Request exceeds max columns (10 on FREE plan)', 'code': 'max_columns'})
    [result] = _call_results(api_url, ('gd_generate_dataset', {'fields': FIELDS}))
    error = _assert_failure(result, 'tier_forbidden')
    assert 'max columns (10 on FREE plan)' in error['message']


READ_CALLS = [
    ('gd_get_usage', {}),
    ('gd_list_field_types', {}),
    ('gd_get_field_type_options', {'field_type': 'word'}),
    ('gd_list_projects', {}),
    ('gd_design_schema', {'prompt': 'pets'}),
]


@pytest.mark.parametrize('override', [
    (200, b'<html>challenge</html>', 'text/html'),
    (200, {'results': [1, 2, 3]}),
    (200, [1, 2, 3]),
], ids=['html', 'wrong-dict', 'wrong-list'])
def test_bad_bodies_on_read_tools_yield_the_envelope(api_url, override):
    FakeApi.override = override
    for call, result in zip(READ_CALLS, _call_results(api_url, *READ_CALLS)):
        if call[0] == 'gd_list_projects' and override[1] == [1, 2, 3]:
            continue  # a list is the expected top-level shape here
        if call[0] == 'gd_get_field_type_options' and override[1] == {'results': [1, 2, 3]}:
            continue  # options are a free-form object
        assert result[0] is True, call
        _assert_failure(result, 'upstream_error')


@pytest.mark.parametrize('name,arguments', [
    ('gd_generate_project', {'project_id': 'abc'}),
    ('gd_generate_dataset', {}),
    ('gd_generate_dataset', {'fields': 'first_name'}),
    ('gd_get_field_type_options', {'field_type': 'email\n'}),
    ('gd_nope', {}),
], ids=['bad-int', 'missing-required', 'bad-list', 'trailing-newline', 'unknown-tool'])
def test_bad_arguments_yield_the_envelope(api_url, name, arguments):
    [result] = _call_results(api_url, (name, arguments))
    _assert_failure(result, 'invalid_input')


def test_missing_key_yields_the_envelope(api_url):
    [result] = _call_results(api_url, ('gd_get_usage', {}), key=None)
    assert 'GENERATE_DATA_API_KEY' in _assert_failure(result, 'configuration_error')['message']


@pytest.mark.parametrize('base_url', ['ftp://example.com', 'http://[bad', 'https://generate-data.com/api/v2'])
def test_bad_base_url_says_to_check_it_and_is_not_retriable(base_url):
    [result] = _call_results(base_url, ('gd_get_usage', {}))
    error = _assert_failure(result, 'configuration_error')
    assert error['retriable'] is False
    assert 'GENERATE_DATA_API_BASE_URL' in error['message']


def test_refused_connection_is_retriable():
    [result] = _call_results('http://127.0.0.1:9', ('gd_get_usage', {}))
    assert _assert_failure(result, 'upstream_unavailable')['retriable'] is True


# ---------------------------------------------------------------------------
# Project tools
# ---------------------------------------------------------------------------

def _writes():
    return [r for r in FakeApi.requests if r[0] in ('POST', 'PUT', 'DELETE')]


def test_design_then_generate_inline_then_create(api_url):
    [designed] = _call_tools(api_url, ('gd_design_project', {'prompt': 'a store'}))
    project = designed['data']['project']
    arguments = {key: project[key] for key in ('name', 'tables', 'relationships')}
    inline, created = _call_tools(
        api_url,
        ('gd_generate_project_inline', arguments),
        ('gd_create_project', arguments),
    )
    assert zipfile.ZipFile(io.BytesIO(base64.b64decode(inline['data']['content_base64']))).namelist()
    assert created['data']['id'] == 8


def test_inline_format_goes_in_the_body(api_url):
    [result] = _call_tools(api_url, ('gd_generate_project_inline', {'tables': PROJECT['tables'], 'format': 'bundle'}))
    assert json.loads(result['data']['content'])['tables'] == {'customers': []}
    method, path, body, _ = FakeApi.requests[-1]
    assert (method, path) == ('POST', '/api/v2/projects/generate-inline')
    assert body['format'] == 'bundle'


def test_get_project(api_url):
    [result] = _call_tools(api_url, ('gd_get_project', {'project_id': 7}))
    assert result['data']['tables'] == PROJECT['tables']


def test_get_project_not_found(api_url):
    [result] = _call_tools(api_url, ('gd_get_project', {'project_id': 99}))
    assert result['error']['code'] == 'not_found'


def test_update_sends_only_the_arguments_given(api_url):
    empty_rels, renamed = _call_tools(
        api_url,
        ('gd_update_project', {'project_id': 7, 'relationships': []}),
        ('gd_update_project', {'project_id': 7, 'name': 'renamed'}),
    )
    assert empty_rels['ok'] is True and renamed['data']['name'] == 'renamed'
    assert [(r[0], r[2]) for r in _writes()] == [('PUT', {'relationships': []}), ('PUT', {'name': 'renamed'})]


def test_update_with_nothing_to_change_never_calls_the_api(api_url):
    [result] = _call_tools(api_url, ('gd_update_project', {'project_id': 7}))
    assert result['error']['code'] == 'invalid_input'
    assert _writes() == []


@pytest.mark.parametrize('arguments', [{'project_id': 7}, {'project_id': 7, 'confirm': False}], ids=['omitted', 'false'])
def test_delete_without_confirm_never_calls_the_api(api_url, arguments):
    [result] = _call_tools(api_url, ('gd_delete_project', arguments))
    assert result['error']['code'] == 'invalid_input'
    assert 'Nothing was deleted' in result['error']['message']
    assert FakeApi.requests == []


def test_delete_with_confirm(api_url):
    [result] = _call_tools(api_url, ('gd_delete_project', {'project_id': 7, 'confirm': True}))
    assert result['data'] == {'id': 7, 'deleted': True}
    assert [(r[0], r[1]) for r in _writes()] == [('DELETE', '/api/v2/projects/7')]


def test_relationship_errors_reach_the_agent(api_url):
    [result] = _call_tools(api_url, ('gd_create_project', {
        'name': 'x', 'tables': PROJECT['tables'], 'relationships': [{'from_table': 'missing'}],
    }))
    assert result['error']['code'] == 'invalid_input'
    assert 'unknown from_table' in result['error']['message']


def test_validate_field_options_invalid_is_a_normal_result(api_url):
    valid, invalid, unknown = _call_tools(
        api_url,
        ('gd_validate_field_options', {'field_type': 'word', 'options': {'ok': True}}),
        ('gd_validate_field_options', {'field_type': 'word', 'options': {'ok': False}}),
        ('gd_validate_field_options', {'field_type': 'nope', 'options': {}}),
    )
    assert valid['data']['is_valid'] is True
    assert invalid['ok'] is True and invalid['data']['errors'] == ['bad option']
    assert unknown['error']['code'] == 'not_found'


# ---------------------------------------------------------------------------
# Schemas, annotations and server metadata
# ---------------------------------------------------------------------------

def _list_tools(api_url):
    async def body(session):
        return {tool.name: tool for tool in (await session.list_tools()).tools}
    return _with_session(api_url, body)


EXPECTED_HINTS = {
    # name: (readOnly, destructive, idempotent)
    'gd_list_field_types': (True, False, True),
    'gd_get_field_type_options': (True, False, True),
    'gd_validate_field_options': (True, False, True),
    'gd_get_usage': (True, False, True),
    'gd_list_projects': (True, False, True),
    'gd_get_project': (True, False, True),
    'gd_design_schema': (False, False, False),
    'gd_design_project': (False, False, False),
    'gd_generate_dataset': (False, False, False),
    'gd_generate_project': (False, False, False),
    'gd_generate_project_inline': (False, False, False),
    'gd_create_project': (False, False, False),
    'gd_update_project': (False, True, True),
    'gd_delete_project': (False, True, True),
}


def test_every_tool_has_title_and_correct_annotations(api_url):
    tools = _list_tools(api_url)
    assert set(tools) == set(EXPECTED_HINTS)
    for name, (read_only, destructive, idempotent) in EXPECTED_HINTS.items():
        hints = tools[name].annotations
        assert tools[name].title, name
        assert (hints.readOnlyHint, hints.destructiveHint, hints.idempotentHint) == (read_only, destructive, idempotent), name
        assert hints.openWorldHint is True, name


def test_input_schemas_declare_enums_and_ranges(api_url):
    tools = _list_tools(api_url)
    dataset = tools['gd_generate_dataset'].inputSchema
    assert set(dataset['properties']['format']['enum']) >= {'csv', 'parquet', 'hf-datasets'}
    assert dataset['properties']['num_rows']['minimum'] == 1
    settings = dataset['$defs']['GenerationSettings']
    assert settings['additionalProperties'] is True
    assert {'locale', 'seed', 'realism_mode', 'domain_pack'} <= set(settings['properties'])
    project_format = tools['gd_generate_project_inline'].inputSchema['properties']['format']
    assert set(project_format['enum']) == {'zip', 'xlsx', 'neo4j', 'jsonld', 'bundle'}
    limit = tools['gd_list_projects'].inputSchema['properties']['limit']
    assert (limit['minimum'], limit['maximum']) == (1, 100)
    assert tools['gd_get_project'].inputSchema['properties']['project_id']['minimum'] == 1


@pytest.mark.parametrize('name,arguments,code', [
    ('gd_generate_dataset', {'fields': FIELDS, 'format': 'zip'}, 'unsupported_format'),
    ('gd_generate_project', {'project_id': 7, 'format': 'csv'}, 'unsupported_format'),
    ('gd_generate_project_inline', {'tables': [], 'format': 'csv'}, 'unsupported_format'),
    ('gd_generate_dataset', {'fields': FIELDS, 'num_rows': 0}, 'invalid_input'),
    ('gd_list_projects', {'limit': 101}, 'invalid_input'),
    ('gd_get_project', {'project_id': 0}, 'invalid_input'),
], ids=['dataset-format', 'project-format', 'inline-format', 'rows', 'limit', 'project-id'])
def test_schema_violations_are_rejected_before_calling_the_api(api_url, name, arguments, code):
    [result] = _call_tools(api_url, (name, arguments))
    assert result['error']['code'] == code
    assert FakeApi.requests == []


def test_format_stays_case_insensitive(api_url):
    [result] = _call_tools(api_url, ('gd_generate_dataset', {'fields': FIELDS, 'format': 'CSV'}))
    assert result['ok'] is True
    assert FakeApi.requests[-1][2]['format'] == 'csv'


def test_settings_and_ai_groups_reach_the_api_correctly(api_url):
    group = {'group_id': 'g1', 'prompt': 'related', 'columns': ['id']}
    _call_tools(api_url, ('gd_generate_dataset', {
        'fields': FIELDS,
        'settings': {'seed': 42, 'some_new_setting': 'kept'},
        'ai_groups': [group],
    }))
    body = FakeApi.requests[-1][2]
    # Unset settings stay out: a null would override the API's own default.
    assert body['settings'] == {'seed': 42, 'some_new_setting': 'kept'}
    assert body['ai_groups'] == [group]


def test_malformed_ai_group_is_rejected_not_silently_dropped(api_url):
    [result] = _call_tools(api_url, ('gd_generate_dataset', {
        'fields': FIELDS, 'ai_groups': [{'group_id': 'g1', 'prompt': 'x', 'columns': []}],
    }))
    assert result['error']['code'] == 'invalid_input'
    assert FakeApi.requests == []


def test_generate_reports_rows_and_remaining_calls(api_url):
    [result] = _call_tools(api_url, ('gd_generate_dataset', {'fields': FIELDS, 'num_rows': 2}))
    assert (result['data']['rows'], result['data']['calls_today'], result['data']['calls_remaining']) == (2, 4, 6)


def test_server_info_reports_the_package_version_and_instructions(api_url):
    params = _server_params(api_url)

    async def run():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                return await session.initialize()

    info = asyncio.run(run())
    assert info.serverInfo.name == 'generate-data'
    assert info.serverInfo.version == generate_data_mcp.__version__
    assert 'gd_design_schema' in info.instructions and '10,000 rows' in info.instructions


def test_version_flag_prints_and_exits_without_serving():
    done = subprocess.run(
        [sys.executable, '-m', 'generate_data_mcp.server', '--version'],
        capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL,
        env={'PATH': os.environ.get('PATH', '')},
    )
    assert done.returncode == 0
    assert done.stdout.strip() == f'generate-data-mcp {generate_data_mcp.__version__}'


# ---------------------------------------------------------------------------
# User-Agent and retries
# ---------------------------------------------------------------------------

def test_requests_identify_the_client(api_url):
    _call_tools(api_url, ('gd_get_usage', {}), ('gd_generate_dataset', {'fields': FIELDS}))
    for _, _, _, headers in FakeApi.requests:
        lowered = {k.lower(): v for k, v in headers.items()}
        assert lowered['user-agent'] == f'generate-data-mcp/{generate_data_mcp.__version__}'


USAGE_OK = (200, {'tier': 'FREE', 'calls_today': 1})
BUSY = (503, {'detail': 'down'})


def test_get_retries_gateway_errors_then_succeeds(api_url):
    FakeApi.sequence = [BUSY, (502, b'', 'text/html'), USAGE_OK]
    [result] = _call_tools(api_url, ('gd_get_usage', {}))
    assert result['ok'] is True
    assert len(FakeApi.requests) == 3


def test_get_gives_up_after_two_retries(api_url):
    FakeApi.override = BUSY
    [result] = _call_tools(api_url, ('gd_get_usage', {}))
    assert result['error']['retriable'] is True
    assert len(FakeApi.requests) == 3


def test_get_retries_throttling_using_retry_after(api_url):
    FakeApi.sequence = [(429, {'detail': 'throttled'}, 'application/json', {'Retry-After': '1'}), USAGE_OK]
    started = time.monotonic()
    [result] = _call_tools(api_url, ('gd_get_usage', {}))
    assert result['ok'] is True
    assert len(FakeApi.requests) == 2
    assert time.monotonic() - started >= 1


def test_get_does_not_sleep_through_a_long_retry_after(api_url):
    FakeApi.override = (429, {'detail': 'throttled'}, 'application/json', {'Retry-After': '120'})
    [result] = _call_tools(api_url, ('gd_get_usage', {}))
    assert result['error']['details']['retry_after'] == 120
    assert len(FakeApi.requests) == 1


def test_get_never_retries_the_daily_limit(api_url):
    FakeApi.override = (429, {'error': 'Daily limit', 'code': 'daily_call_limit'})
    [result] = _call_tools(api_url, ('gd_get_usage', {}))
    assert result['error']['retriable'] is False
    assert len(FakeApi.requests) == 1


@pytest.mark.parametrize('status', [429, 503])
@pytest.mark.parametrize('name,arguments', [
    ('gd_generate_dataset', {'fields': FIELDS}),
    ('gd_generate_project', {'project_id': 7}),
    ('gd_generate_project_inline', {'tables': PROJECT['tables']}),
    ('gd_create_project', {'name': 'x', 'tables': PROJECT['tables']}),
    ('gd_update_project', {'project_id': 7, 'name': 'x'}),
    ('gd_delete_project', {'project_id': 7, 'confirm': True}),
    ('gd_design_schema', {'prompt': 'x'}),
    ('gd_design_project', {'prompt': 'x'}),
    ('gd_validate_field_options', {'field_type': 'word', 'options': {}}),
])
def test_writes_and_posts_are_never_retried(api_url, name, arguments, status):
    FakeApi.override = (status, {'detail': 'busy'}, 'application/json', {'Retry-After': '1'})
    [result] = _call_tools(api_url, (name, arguments))
    assert result['ok'] is False
    assert len(FakeApi.requests) == 1


def _start_with_base_url(base_url):
    return subprocess.run(
        [sys.executable, '-m', 'generate_data_mcp.server'],
        capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
        env={
            'PATH': os.environ.get('PATH', ''),
            'GENERATE_DATA_API_KEY': 'very-secret-key',
            'GENERATE_DATA_API_BASE_URL': base_url,
        },
    )


def test_plain_http_to_a_remote_host_warns_without_the_key():
    done = _start_with_base_url('http://api.example.test')
    assert 'plain http://' in done.stderr and 'api.example.test' in done.stderr
    assert 'very-secret-key' not in done.stderr
    assert done.stdout == ''


@pytest.mark.parametrize('base_url', ['https://example.test', 'http://localhost:8001', 'http://127.0.0.1:8001', 'http://[::1]:8001'])
def test_no_http_warning_for_https_or_loopback(base_url):
    assert 'plain http://' not in _start_with_base_url(base_url).stderr


# ---------------------------------------------------------------------------
# Deleting needs a real confirmation and a real 204
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('override', [
    (200, b'<html>challenge</html>', 'text/html'),
    (200, b'deleted', 'text/plain'),
    (200, b'', 'text/plain'),
    (202, {'queued': True}),
], ids=['html', 'text', 'empty', 'accepted'])
def test_delete_is_only_successful_on_204(api_url, override):
    FakeApi.override = override
    [result] = _call_tools(api_url, ('gd_delete_project', {'project_id': 7, 'confirm': True}))
    assert result['ok'] is False
    assert result['error']['code'] == 'upstream_error'


@pytest.mark.parametrize('confirm', ['true', 'True', 'yes', 'on', 't', '1', 1, 1.0], ids=repr)
def test_delete_confirm_accepts_only_json_true(api_url, confirm):
    [result] = _call_tools(api_url, ('gd_delete_project', {'project_id': 7, 'confirm': confirm}))
    assert result['error']['code'] == 'invalid_input'
    assert FakeApi.requests == []


def test_delete_with_boolean_project_id_leaves_project_1_alone(api_url):
    [result] = _call_tools(api_url, ('gd_delete_project', {'project_id': True, 'confirm': True}))
    assert result['error']['code'] == 'invalid_input'
    assert FakeApi.requests == []


@pytest.mark.parametrize('name,arguments', [
    ('gd_generate_dataset', {'fields': FIELDS, 'num_rows': True}),
    ('gd_generate_dataset', {'fields': FIELDS, 'settings': {'seed': True}}),
    ('gd_generate_project', {'project_id': True}),
    ('gd_get_project', {'project_id': True}),
    ('gd_update_project', {'project_id': True, 'name': 'x'}),
    ('gd_list_projects', {'limit': True}),
    ('gd_list_projects', {'offset': False}),
], ids=['num_rows', 'seed', 'generate-id', 'get-id', 'update-id', 'limit', 'offset'])
def test_booleans_are_rejected_for_every_integer_argument(api_url, name, arguments):
    [result] = _call_tools(api_url, (name, arguments))
    assert result['error']['code'] == 'invalid_input'
    assert FakeApi.requests == []


def test_digit_strings_stay_accepted_for_integers(api_url):
    project, listed = _call_tools(
        api_url,
        ('gd_get_project', {'project_id': '7'}),
        ('gd_list_projects', {'limit': '5'}),
    )
    assert project['data']['id'] == 7
    assert listed['data']['limit'] == 5


# ---------------------------------------------------------------------------
# Generation warnings
# ---------------------------------------------------------------------------

CSV_OK = b'id\n1\n'


@pytest.mark.parametrize('header,expected', [
    ('["AI provider unavailable"]', ['AI provider unavailable']),
    ('not json', ['The API reported generation warnings in a form this server could not read.']),
    ('{"a": 1}', ['The API reported generation warnings in a form this server could not read.']),
], ids=['array', 'malformed', 'wrong-shape'])
def test_generation_warnings_header_is_surfaced(api_url, header, expected):
    FakeApi.override = (200, CSV_OK, 'text/csv', {'X-Generation-Warnings': header})
    [result] = _call_tools(api_url, ('gd_generate_dataset', {'fields': FIELDS}))
    assert result['ok'] is True
    assert result['data']['warnings'] == expected
    assert 'WARNING' in result['summary'] and 'data.warnings' in result['summary']


def test_empty_warnings_header_reads_as_clean(api_url):
    FakeApi.override = (200, CSV_OK, 'text/csv', {'X-Generation-Warnings': '[]'})
    [result] = _call_tools(api_url, ('gd_generate_dataset', {'fields': FIELDS}))
    assert 'warnings' not in result['data']
    assert 'WARNING' not in result['summary']


def test_json_meta_warnings_are_surfaced_and_deduplicated(api_url):
    body = {'records': [{'id': 1, 'note': ''}], 'meta': {'warnings': [{'code': 'x', 'message': 'no provider'}, 'other']}}
    FakeApi.override = (200, body, 'application/json', {'X-Generation-Warnings': '["no provider"]'})
    [result] = _call_tools(api_url, ('gd_generate_dataset', {'fields': FIELDS, 'format': 'json'}))
    assert result['data']['warnings'] == ['no provider', 'other']
    assert 'WARNING' in result['summary']


# ---------------------------------------------------------------------------
# Bodies that say 200 but aren't the file
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('name,arguments,body,content_type', [
    ('gd_generate_project', {'project_id': 7}, b'PK garbage', 'application/zip'),
    ('gd_generate_project', {'project_id': 7, 'format': 'neo4j'}, b'PK garbage', 'application/zip'),
    ('gd_generate_project', {'project_id': 7, 'format': 'jsonld'}, b'not json', 'application/json'),
    ('gd_generate_project', {'project_id': 7, 'format': 'bundle'}, {'error': 'x'}, 'application/json'),
    ('gd_generate_dataset', {'fields': FIELDS, 'format': 'csv'}, {'error': 'x', 'code': 'y'}, 'application/json'),
    ('gd_generate_dataset', {'fields': FIELDS, 'format': 'json'}, b'{broken', 'application/json'),
    ('gd_generate_dataset', {'fields': FIELDS, 'format': 'parquet'}, b'PAR1 truncated', 'application/octet-stream'),
], ids=['zip', 'neo4j', 'jsonld', 'bundle-error', 'csv-error', 'json-broken', 'parquet-truncated'])
def test_bad_200_bodies_are_failures(api_url, name, arguments, body, content_type):
    FakeApi.override = (200, body, content_type)
    [result] = _call_tools(api_url, (name, arguments))
    assert result['ok'] is False
    assert result['error']['code'] == 'upstream_error'


def test_jsonl_row_with_a_column_named_error_is_data(api_url):
    FakeApi.override = (200, b'{"error": "row value"}\n', 'application/x-ndjson')
    [result] = _call_tools(api_url, ('gd_generate_dataset', {'fields': FIELDS, 'format': 'jsonl'}))
    assert result['ok'] is True


def test_json_body_with_records_and_an_error_column_is_data(api_url):
    FakeApi.override = (200, {'records': [{'error': 'x'}]}, 'application/json')
    [result] = _call_tools(api_url, ('gd_generate_dataset', {'fields': FIELDS, 'format': 'json'}))
    assert result['ok'] is True


# ---------------------------------------------------------------------------
# Unusable keys and key redaction
# ---------------------------------------------------------------------------

def test_key_surrounded_by_whitespace_is_stripped(api_url):
    [result] = _call_tools(api_url, ('gd_get_usage', {}), key=f'  {API_KEY}\n')
    assert result['ok'] is True
    assert FakeApi.requests[-1][3]['X-API-Key'] == API_KEY


@pytest.mark.parametrize('key', ['bad key-value', 'bad\nkey-value', 'clé-secrète', '   '], ids=['space', 'newline', 'non-ascii', 'blank'])
def test_unusable_key_is_configuration_error_and_never_printed(api_url, key):
    [result] = _call_tools(api_url, ('gd_get_usage', {}), key=key)
    assert result['error']['code'] == 'configuration_error'
    assert result['error']['retriable'] is False
    assert FakeApi.requests == []
    if key.strip():
        assert key not in json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize('name,arguments', [
    ('gd_get_usage', {}),
    ('gd_generate_dataset', {'fields': FIELDS}),
])
def test_echoed_key_is_redacted_everywhere(api_url, name, arguments):
    FakeApi.override = (400, {'error': f'Bad key {API_KEY}', 'details': [f'got {API_KEY}']})
    [result] = _call_tools(api_url, (name, arguments))
    assert API_KEY not in json.dumps(result)
    assert '[REDACTED]' in result['error']['details']['upstream']


def test_key_echoed_in_a_200_error_or_warning_is_redacted(api_url):
    FakeApi.sequence = [
        (200, {'error': f'Bad key {API_KEY}'}, 'application/json'),
        (200, CSV_OK, 'text/csv', {'X-Generation-Warnings': json.dumps([f'used {API_KEY}'])}),
    ]
    results = _call_tools(
        api_url,
        ('gd_generate_dataset', {'fields': FIELDS}),
        ('gd_generate_dataset', {'fields': FIELDS}),
    )
    assert API_KEY not in json.dumps(results)
    assert results[1]['data']['warnings'] == ['used [REDACTED]']


# ---------------------------------------------------------------------------
# Failed writes leave nothing behind
# ---------------------------------------------------------------------------

def test_failed_write_leaves_no_partial_file(api_url, output_dir):
    """`ulimit -f` makes the real write fail part-way, inside the real server."""
    output_dir.mkdir()
    params = StdioServerParameters(
        command='/bin/sh',
        args=['-c', f'ulimit -f 8; exec "{sys.executable}" -m generate_data_mcp.server'],
        env={**_server_params(api_url).env, 'PYTHONDONTWRITEBYTECODE': '1'},
    )

    async def run():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool('gd_generate_dataset', {'fields': FIELDS, 'num_rows': 40_000})
                return json.loads(result.content[0].text)

    result = asyncio.run(run())
    assert result['error']['code'] == 'configuration_error'
    assert list(output_dir.iterdir()) == []


def test_saved_file_is_complete_with_no_temporary_left(api_url, output_dir):
    [result] = _call_tools(api_url, ('gd_generate_dataset', {'fields': FIELDS, 'num_rows': 3, 'save_to_file': True}))
    files = list(output_dir.iterdir())
    assert [str(p) for p in files] == [result['data']['file_path']]
    assert files[0].read_text() == 'id\n1\n2\n3\n'
