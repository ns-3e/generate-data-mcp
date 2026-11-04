"""MCP stdio server entrypoint.

DOC: F-002 rebuild renames the 5 free-tier tools per the frozen contract
(`.claude/program/contracts/tool-conventions.md` Sec. 1):
`generate_data`->`gd_generate_dataset`, `list_field_types`->`gd_list_field_types`,
`get_field_options`->`gd_get_field_type_options`, `propose_schema`+`refine_schema`
merged into one `gd_design_schema` (first call proposes; passing both `messages`
and `current_schema` refines), `get_api_usage`->`gd_get_usage`. This is a
breaking rename for any existing MCP client config pointing at the old names.
Project tools rebuilt by F-003 per the same contract:
`list_projects`->`gd_list_projects` (paginated), `generate_project`->
`gd_generate_project` (binary-aware — fixes the F-001/B-001 corruption bug
where zip bytes were force-decoded as utf-8).
"""
import json
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from generate_data_mcp._conventions import (
    _encode_binary,
    _encode_text,
    _err,
    _ok,
    _paginate,
    _validate_field_type,
    _validate_pagination,
    _validate_project_id,
)
from generate_data_mcp.client import GenerateDataClient

mcp = FastMCP('generate-data')


# DOC: process-lifetime singleton — one GenerateDataClient (and its one
# reused httpx.Client) per stdio server process, per contract Sec. 8 /
# brief F-008. Do not construct a fresh GenerateDataClient per tool call.
_client_singleton: GenerateDataClient | None = None


def _client() -> GenerateDataClient:
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = GenerateDataClient()
    return _client_singleton


# These mirror API_LIMITS in the backend's api/plan_config.py (the top tier's
# lists); the monorepo's test_mcp_client_contract.py fails if they drift.
_DATASET_FORMATS = frozenset({'csv', 'json', 'xml', 'parquet', 'xlsx', 'yaml', 'sql', 'jsonl', 'hf-datasets'})
_PROJECT_FORMATS = frozenset({'zip', 'xlsx', 'neo4j', 'jsonld', 'bundle'})
_BINARY_FORMATS = frozenset({'parquet', 'xlsx', 'hf-datasets', 'zip', 'neo4j'})
_PARQUET_MAGIC = b'PAR1'
_ZIP_MAGIC = b'PK'  # xlsx, hf-datasets and neo4j are all ZIP containers


def _download_problem(fmt: str, response: Any) -> str | None:
    """Why a 2xx body is not really the requested file, or None if it looks right.

    A proxy or challenge page can answer 200 with HTML, and a redirect that is
    not followed has an empty body; neither may be reported as generated data.
    """
    content = response.content
    if not content:
        return 'an empty response body'
    if 'text/html' in response.headers.get('content-type', '').lower():
        return 'an HTML page instead of data'
    if fmt in _BINARY_FORMATS:
        magic = _PARQUET_MAGIC if fmt == 'parquet' else _ZIP_MAGIC
        if not content.startswith(magic):
            return f'a body that is not a valid {fmt} file'
    return None


def _render_download(fmt: str, response: Any, summary: str) -> str:
    """Turn a generate response into the success envelope, or an error if the
    status or body shows the data did not actually arrive."""
    if response.status_code >= 300:
        try:
            body = response.json()
        except Exception:
            body = None
        return _err_from_status(response.status_code, body)
    problem = _download_problem(fmt, response)
    if problem:
        return _err(
            'upstream_error',
            f'The API returned {problem}. Check GENERATE_DATA_API_BASE_URL, then retry later.',
            details={'status': response.status_code},
        )
    if fmt in _BINARY_FORMATS:
        data = _encode_binary(fmt, response.content)
    else:
        data = _encode_text(fmt, response.text)
    return _ok(f"{summary} as {fmt.upper()} ({data['size_bytes']:,} bytes).", data)


def _classify_http_error(status_code: int, body: Any) -> tuple[str, str, bool]:
    """Map an upstream HTTP status/body to the contract Sec. 3 (code, message,
    retriable) tuple. Never includes API key/header data in the message."""
    upstream_msg = ''
    if isinstance(body, dict):
        upstream_msg = str(body.get('error') or body.get('message') or '')
    lowered = upstream_msg.lower()
    if status_code == 400:
        return 'invalid_input', 'Upstream rejected the request as invalid. Check field names, types, and options against gd_list_field_types.', False
    if status_code in (401, 403):
        if status_code == 403 and any(kw in lowered for kw in ('tier', 'premium', 'upgrade', 'plan')):
            return 'tier_forbidden', 'This action requires a higher API tier. Upgrade your Generate-Data.com plan to use it.', False
        return 'auth_failed', 'Authentication failed. Verify GENERATE_DATA_API_KEY is set to a valid, active API key.', False
    if status_code == 429:
        return 'rate_limited', 'Rate limit exceeded. Wait before retrying this call.', True
    if status_code == 404:
        return 'not_found', 'The requested id or field_type was not found. Re-check it via gd_list_field_types or gd_list_projects.', False
    return 'upstream_error', f'Upstream API returned an unexpected error (status {status_code}). Retry later or check service status.', False


def _err_from_status(status_code: int, body: Any) -> str:
    code, message, retriable = _classify_http_error(status_code, body)
    details: dict[str, Any] = {}
    if body is not None:
        # Contract Sec. 3/8: upstream text must be delimited, never interpolated.
        details['upstream'] = json.dumps(body)
    return _err(code, message, retriable=retriable, details=details)


def _err_from_http_status_error(exc: httpx.HTTPStatusError) -> str:
    response = exc.response
    try:
        body = response.json()
    except Exception:
        body = None
    return _err_from_status(response.status_code, body)


@mcp.tool()
def gd_generate_dataset(
    fields: list[dict[str, Any]],
    num_rows: int = 100,
    format: str = 'csv',
    settings: dict[str, Any] | None = None,
) -> str:
    """Generate synthetic dataset rows from a field list. Use this when the user
    wants actual generated data (as opposed to schema design or field-type
    discovery). `fields` is a list of `{"name": str, "type": str, "options": dict}`
    (get valid `type` values from `gd_list_field_types`/`gd_get_field_type_options`).
    `format` is one of `csv`, `json`, `xml`, `yaml`, `sql`, `jsonl`, `parquet`,
    `xlsx`, `hf-datasets`; Free keys can only use `csv` (others return
    `tier_forbidden`). `num_rows` is capped by plan: 100 on Free, 10,000 on Pro.
    `settings` is an optional passthrough dict of generation settings.
    Returns the JSON envelope: on success `data.content` (text formats) or
    `data.content_base64` (`parquet`, `xlsx`, `hf-datasets` — never decode these
    as utf-8) plus `data.size_bytes`/`data.truncated`. On failure returns a
    corrective error envelope."""
    fmt = format.lower()
    if fmt not in _DATASET_FORMATS:
        return _err(
            'unsupported_format',
            f'format must be one of {sorted(_DATASET_FORMATS)}; got {format!r}.',
        )
    client = _client()
    try:
        response = client.generate_data(fields, num_rows=num_rows, fmt=fmt, settings=settings)
    except httpx.HTTPError as exc:
        return _err('upstream_unavailable', f'Could not reach the Generate-Data.com API: {exc.__class__.__name__}. Retry later.', retriable=True)
    return _render_download(fmt, response, f'Generated {num_rows} rows')


@mcp.tool()
def gd_list_field_types() -> str:
    """List all available field types grouped by category. Use this to discover
    valid `type` values before calling `gd_generate_dataset` or to look up which
    category a field type belongs to. Takes no arguments. Returns the categorized
    field-type catalog in `data.categories`."""
    try:
        result = _client().list_field_types()
    except httpx.HTTPStatusError as exc:
        return _err_from_http_status_error(exc)
    except httpx.HTTPError as exc:
        return _err('upstream_unavailable', f'Could not reach the Generate-Data.com API: {exc.__class__.__name__}. Retry later.', retriable=True)
    categories = result.get('categories', {}) if isinstance(result, dict) else {}
    return _ok(f'Found {len(categories)} field type categories.', result)


@mcp.tool()
def gd_get_field_type_options(field_type: str) -> str:
    """Get the configuration option schema for one field type (e.g. what options
    `email` or `date` accept). Use this before building a `fields` entry for
    `gd_generate_dataset` that needs non-default options. `field_type` must match
    `^[a-z0-9_]+$` (lowercase letters, digits, underscore only) — anything else
    returns `invalid_input` without calling upstream. Returns the option schema
    in `data`."""
    try:
        field_type = _validate_field_type(field_type)
    except ValueError as exc:
        return _err('invalid_input', str(exc))
    try:
        result = _client().get_field_options(field_type)
    except httpx.HTTPStatusError as exc:
        return _err_from_http_status_error(exc)
    except httpx.HTTPError as exc:
        return _err('upstream_unavailable', f'Could not reach the Generate-Data.com API: {exc.__class__.__name__}. Retry later.', retriable=True)
    return _ok(f"Retrieved configuration options for field type '{field_type}'.", result)


@mcp.tool()
def gd_design_schema(
    prompt: str | None = None,
    locale: str = 'en_US',
    messages: list[dict[str, Any]] | None = None,
    current_schema: list[dict[str, Any]] | None = None,
) -> str:
    """Design a dataset schema from natural language — one tool for both the
    first proposal and follow-up refinements. Behavior branches on args:
    - Propose: pass `prompt` (and optionally `locale`).
    - Refine: pass BOTH `messages` (the running conversation, a list of
      `{"role": "user"|"assistant", "content": str}`) AND `current_schema` (the
      `data.proposed_schema` array from the prior call). `prompt` is not used.
    Passing only one of `messages`/`current_schema`, or neither of them and no
    `prompt`, returns `invalid_input`. Returns `data.proposed_schema`, an array
    you can pass directly to `gd_generate_dataset` as `fields`."""
    if (messages is None) != (current_schema is None):
        return _err('invalid_input', 'To refine, pass both messages and current_schema. To propose, pass only prompt.')
    if messages is None and not (prompt or '').strip():
        return _err('invalid_input', 'prompt is required to propose a schema.')
    client = _client()
    try:
        if messages is not None:
            result = client.refine_schema(messages, current_schema)
            summary = 'Refined the schema based on the conversation.'
        else:
            result = client.propose_schema(prompt, locale=locale)
            summary = f'Proposed a schema for: "{prompt}"'
    except httpx.HTTPStatusError as exc:
        return _err_from_http_status_error(exc)
    except httpx.HTTPError as exc:
        return _err('upstream_unavailable', f'Could not reach the Generate-Data.com API: {exc.__class__.__name__}. Retry later.', retriable=True)
    return _ok(summary, result)


@mcp.tool()
def gd_get_usage() -> str:
    """Get current API key usage stats (calls today, tier, limits). Use this to
    check remaining quota or confirm which tier the configured API key has
    before attempting a premium-only call. Takes no arguments. Returns the
    usage stats in `data`."""
    try:
        result = _client().get_usage()
    except httpx.HTTPStatusError as exc:
        return _err_from_http_status_error(exc)
    except httpx.HTTPError as exc:
        return _err('upstream_unavailable', f'Could not reach the Generate-Data.com API: {exc.__class__.__name__}. Retry later.', retriable=True)
    tier = result.get('tier', 'unknown') if isinstance(result, dict) else 'unknown'
    calls = result.get('calls_today') if isinstance(result, dict) else None
    summary = f'Current API tier: {tier}.' + (f' {calls} calls today.' if calls is not None else '')
    return _ok(summary, result)


@mcp.tool()
def gd_list_projects(limit: int = 20, offset: int = 0) -> str:
    """List the user's Projects. Use this to discover
    `project_id` values before calling `gd_generate_project`. `limit` (default
    20, max 100) and `offset` (default 0) page through the results; results are
    paginated client-side since the upstream API returns the full list.
    Returns the page in `data`
    (`items`, `limit`, `offset`, `total_count`, `has_more`, `next_offset`)."""
    try:
        limit, offset = _validate_pagination(limit, offset)
    except ValueError as exc:
        return _err('invalid_input', str(exc))
    try:
        items = _client().list_projects()
    except httpx.HTTPStatusError as exc:
        return _err_from_http_status_error(exc)
    except httpx.HTTPError as exc:
        return _err('upstream_unavailable', f'Could not reach the Generate-Data.com API: {exc.__class__.__name__}. Retry later.', retriable=True)
    total_count = len(items)
    page = items[offset:offset + limit]
    data = _paginate(page, limit, offset, total_count=total_count)
    return _ok(f'Found {total_count} project(s); returning {len(page)} starting at offset {offset}.', data)


@mcp.tool()
def gd_generate_project(project_id: int, format: str = 'zip') -> str:
    """Generate all tables in a Project and download the result. `project_id`
    must be a positive integer (get one from `gd_list_projects`). `format` is one
    of `zip` (default; one CSV per table), `xlsx`, `neo4j`, `jsonld`, `bundle`.
    Free keys can only use `zip` (others return `tier_forbidden`), and a project's
    rows and columns must fit the plan's limits. Returns the JSON envelope: on
    success `data.content_base64` (`zip`, `xlsx`, `neo4j` — never decode these as
    utf-8) or `data.content` (`jsonld`, `bundle`) plus
    `data.size_bytes`/`data.truncated`. On failure returns a corrective error
    envelope."""
    try:
        project_id = _validate_project_id(project_id)
    except ValueError as exc:
        return _err('invalid_input', str(exc))
    fmt = format.lower()
    if fmt not in _PROJECT_FORMATS:
        return _err(
            'unsupported_format',
            f'format must be one of {sorted(_PROJECT_FORMATS)}; got {format!r}.',
        )
    client = _client()
    try:
        response = client.generate_project(project_id, fmt=fmt)
    except httpx.HTTPError as exc:
        return _err('upstream_unavailable', f'Could not reach the Generate-Data.com API: {exc.__class__.__name__}. Retry later.', retriable=True)
    return _render_download(fmt, response, f'Generated project {project_id}')


def main():
    mcp.run()


if __name__ == '__main__':
    main()
