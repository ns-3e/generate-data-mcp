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
import threading
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

    override = None  # (status, body, content_type) forced for failure-mode tests

    def log_message(self, *args):
        pass

    def _send(self, status, body=b'', content_type='application/json'):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self):
        length = int(self.headers.get('content-length') or 0)
        raw = self.rfile.read(length)
        body = json.loads(raw) if raw else {}
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
            return self._send(200, 'id\n1\n'.encode(), 'text/csv')
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
def _reset_override():
    FakeApi.override = None
    yield
    FakeApi.override = None


def _call_tools(api_url, *calls, key=API_KEY):
    """Run tool calls over one stdio session; return their parsed envelopes."""
    params = StdioServerParameters(
        command=sys.executable,
        args=['-m', 'generate_data_mcp.server'],
        env={
            'PATH': os.environ.get('PATH', ''),
            'GENERATE_DATA_API_KEY': key,
            'GENERATE_DATA_API_BASE_URL': api_url,
        },
    )

    async def run():
        results = []
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                for name, arguments in calls:
                    result = await session.call_tool(name, arguments)
                    results.append(json.loads(result.content[0].text))
        return results

    return asyncio.run(run())


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
