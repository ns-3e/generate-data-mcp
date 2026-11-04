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
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

API_KEY = 'e2e-test-key'
FIELDS = [{'name': 'id', 'type': 'row_number', 'options': {}}]


def _zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('customers.csv', 'id\n1\n')
    return buffer.getvalue()


class FakeApi(BaseHTTPRequestHandler):
    """Applies the same rules as the DRF views the MCP client calls."""

    override = None  # (status, body, content_type[, headers]) forced for failure-mode tests
    delay = 0  # seconds to wait before answering

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
        if FakeApi.delay:
            time.sleep(FakeApi.delay)
        if FakeApi.override:
            return self._send(*FakeApi.override)
        if self.headers.get('X-API-Key') != API_KEY:
            return self._send(401, {'detail': 'Invalid API key'})
        url = urlsplit(self.path)
        # DRF treats ?format= as renderer selection: unknown renderer means 404.
        if parse_qs(url.query).get('format', ['json'])[0] not in ('json', 'api'):
            return self._send(404, {'detail': 'Not found.'})
        return self._route(url.path, body)

    def _route(self, path, body):
        if path == '/api/v2/generate':
            rows = body.get('num_rows', 1)
            csv = 'id\n' + ''.join(f'{i + 1}\n' for i in range(rows))
            return self._send(200, csv.encode(), 'text/csv', {'X-Usage-Rows-Generated': str(rows)})
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

    do_GET = do_POST = _handle


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
    yield
    FakeApi.override = None
    FakeApi.delay = 0


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
