"""MCP stdio server entrypoint.

Every tool is registered with `@_tool`, which makes it return the JSON envelope
as a `CallToolResult` (`isError: true` on failure) and turns any exception into
the same envelope, so no raw error reaches the client.

`gd_design_schema` covers both proposing and refining a schema. Project tools
accept the formats in `_PROJECT_FORMATS`.
"""
import argparse
import asyncio
import functools
import io
import json
import logging
import os
import re
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Awaitable, Callable, Literal, get_args

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, ToolAnnotations
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from generate_data_mcp import __version__

from generate_data_mcp._conventions import (
    BINARY_FORMATS,
    INLINE_BINARY_LIMIT,
    INLINE_TEXT_LIMIT,
    _encode_binary,
    _encode_saved,
    _encode_text,
    _err,
    _ok,
    _output_dir,
    _paginate,
    _save_output,
    _text_preview,
    _validate_field_type,
    _validate_pagination,
    _validate_project_id,
)
from generate_data_mcp.client import (
    ConfigurationError,
    GenerateDataClient,
    UnexpectedResponse,
    insecure_base_url_warning,
    is_dns_failure,
    retry_after_seconds,
)

logger = logging.getLogger(__name__)


class _GenerateDataServer(FastMCP):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """FastMCP validates arguments before tool code runs and reports failures
        as ToolError, as it does unknown tools. Return those as the envelope too.
        Everything else was already handled inside the tool by `@_tool`."""
        try:
            return await super().call_tool(name, arguments)
        except ToolError as exc:
            code = 'unsupported_format' if _is_format_error(exc) else 'invalid_input'
            return _err(code, _argument_error_message(exc))


_INSTRUCTIONS = """\
Generates synthetic data through the Generate-Data.com API.

One table: call gd_design_schema with a description (or gd_list_field_types and \
gd_get_field_type_options to build fields yourself), then gd_generate_dataset.

Several related tables: call gd_design_project, then gd_generate_project_inline for a \
one-off result, or gd_create_project and gd_generate_project to keep it. Use \
gd_list_projects and gd_get_project to reuse saved Projects.

Plan limits are enforced by the API. Free: 100 rows, 10 columns, 3 tables, 10 calls a \
day, CSV datasets and ZIP Projects only. Pro: 10,000 rows, 50 columns, 20 tables, 1,000 \
calls a day, all formats. gd_get_usage shows the current plan and remaining calls.

Small results come back inline. Larger ones are saved in full and returned as \
data.file_path: read that file rather than generating again. If data.warnings is \
present, the data may be incomplete or have empty values, so read it before using the \
result. Every failure is an error envelope that says what to do next. Only retry when \
error.retriable is true.

AI schema and Project design share their own daily quota (3 a day on Free). They count \
toward calls_today but aren't blocked by the daily call limit. Rows added by \
settings.duplication_config are limited to the plan's row cap, and rows for many-to-many \
relationships to a budget set by the plan, so a result can have fewer rows than requested. \
Both are reported in data.warnings when rows are trimmed.
"""

mcp = _GenerateDataServer('generate-data', instructions=_INSTRUCTIONS)
# FastMCP has no version argument and would otherwise report the SDK's version.
mcp._mcp_server.version = __version__


# One GenerateDataClient (and its one httpx.AsyncClient) per server process.
# There is no await between the check and the assignment, so concurrent tool
# calls on the event loop can't create two.
_client_singleton: GenerateDataClient | None = None


def _client() -> GenerateDataClient:
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = GenerateDataClient()
    return _client_singleton


# These mirror API_LIMITS in the backend's api/plan_config.py (the top tier's
# lists); the monorepo's test_mcp_client_contract.py fails if they drift.
_DatasetFormatName = Literal['csv', 'json', 'xml', 'yaml', 'sql', 'jsonl', 'parquet', 'xlsx', 'hf-datasets']
_ProjectFormatName = Literal['zip', 'xlsx', 'neo4j', 'jsonld', 'bundle']
_DATASET_FORMATS = frozenset(get_args(_DatasetFormatName))
_PROJECT_FORMATS = frozenset(get_args(_ProjectFormatName))
_PARQUET_MAGIC = b'PAR1'  # the rest of BINARY_FORMATS are ZIP containers
_JSON_FORMATS = frozenset({'json', 'jsonld', 'bundle'})
_JSON_PARSE_LIMIT = 8 * 1024 * 1024
_ERROR_BODY_LIMIT = 64 * 1024
_ERROR_BODY_KEYS = frozenset({'error', 'code', 'details'})
_WARNINGS_LIMIT = 20
_WARNING_TEXT_LIMIT = 500

_UPSTREAM_DETAIL_LIMIT = 2_000
_UPSTREAM_MESSAGE_LIMIT = 500
_BASE_URL_HINT = 'Check GENERATE_DATA_API_BASE_URL.'

# The backend's `code` values that mean the plan does not allow the request.
def _lowercase(value: Any) -> Any:
    return value.lower() if isinstance(value, str) else value


DatasetFormat = Annotated[_DatasetFormatName, BeforeValidator(_lowercase)]
ProjectFormat = Annotated[_ProjectFormatName, BeforeValidator(_lowercase)]


def _reject_bool(value: Any) -> Any:
    # Pydantic would turn true into 1, which could address a real Project.
    if isinstance(value, bool):
        raise ValueError('must be an integer, not true or false')
    return value


# Digit strings stay accepted, because some MCP clients send numbers as strings.
_Integer = Annotated[int, BeforeValidator(_reject_bool)]
ProjectId = Annotated[int, Field(ge=1, description='A Project id from gd_list_projects.'), BeforeValidator(_reject_bool)]
Limit = Annotated[int, Field(ge=1, le=100), BeforeValidator(_reject_bool)]
Offset = Annotated[int, Field(ge=0), BeforeValidator(_reject_bool)]
# No upper bound here: the cap depends on the plan and on any grant, so the API enforces it.
NumRows = Annotated[
    int,
    Field(ge=1, description='Rows to generate. Capped by plan: 100 on Free, 10,000 on Pro.'),
    BeforeValidator(_reject_bool),
]
# Only JSON true confirms a deletion: lax parsing would accept "yes", "on", 1 and more.
Confirm = Annotated[bool, Field(strict=True, description='Must be exactly true to delete.')]


class GenerationSettings(BaseModel):
    """Optional generation settings. Keys not listed are passed to the API as they are."""

    model_config = ConfigDict(extra='allow')

    locale: str | None = Field(None, description='Locale requested for the data, as language_COUNTRY, e.g. en_US.')
    seed: _Integer | None = Field(None, description='Generate the same values again by reusing a seed.')
    include_header: bool | None = Field(None, description='Include a header row in CSV output. Default true.')
    line_ending: str | None = Field(None, description='CSV line endings: unix (default), windows or mac.')
    data_quality_level: str | None = Field(None, description='How clean the values are. Default clean.')
    is_distinct: bool | None = Field(None, description='Avoid duplicate rows. Default true.')
    duplication_config: dict[str, Any] | None = Field(None, description="Controls for adding duplicate records. Output stays within the plan's row cap; data.warnings says if rows were trimmed.")
    realism_mode: str | None = Field(None, description='independent, coherent (default) or dirty.')
    company_type: str | None = Field(None, description='Kind of companies to reflect in the data. Default mixed.')
    industry: str | None = Field(None, description='Limit the data to one industry.')
    filename_prefix: str | None = Field(None, description='Prefix for the file name the API suggests.')
    domain_pack: str | None = Field(None, description='A domain pack that shapes the values, e.g. ecommerce.')


class AIFieldGroup(BaseModel):
    """`ai_generated` columns to generate together, so their values relate."""

    group_id: str = Field(min_length=1)
    prompt: str = Field(min_length=1, description='What the group of columns should contain.')
    columns: list[str] = Field(min_length=1, description='Names of `ai_generated` fields in `fields`.')


def _plain(value: Any) -> Any:
    """Model arguments as JSON-ready dicts, without the unset optional keys."""
    if isinstance(value, BaseModel):
        return value.model_dump(exclude_none=True)
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


# Quotas that only reset at 00:00 UTC, so retrying the same day can't succeed.
_DAILY_QUOTAS = {
    'daily_call_limit': ('calls', 'daily API call limit'),
    'daily_export_limit': ('exports', 'daily export limit'),
    'ai_quota_exceeded': ('ai_design', 'daily AI design quota'),
}

_TIER_CODES = frozenset({
    'format_not_allowed',
    'max_rows',
    'max_columns',
    'max_tables',
    'max_relationships',
    'ai_not_entitled',
})


def _is_format_error(exc: ToolError) -> bool:
    errors = getattr(exc.__cause__, 'errors', None)
    return callable(errors) and any(e['loc'][:1] == ('format',) for e in errors())


def _argument_error_message(exc: ToolError) -> str:
    errors = getattr(exc.__cause__, 'errors', None)
    if not callable(errors):
        return str(exc)
    parts = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in errors()]
    return 'Invalid arguments. ' + '; '.join(parts)


def _upstream_text(body: Any) -> str:
    if not isinstance(body, dict):
        return ''
    for key in ('error', 'message', 'detail'):
        value = body.get(key)
        if value:
            text = str(value)
            # Project validation errors list the specifics under `details`.
            specifics = body.get('details')
            if isinstance(specifics, list) and specifics:
                text += ': ' + '; '.join(str(item) for item in specifics)
            return text
    return ''


def _quoted(text: str) -> str:
    """Upstream text, JSON-quoted and capped so it reads as data, not instructions."""
    if len(text) > _UPSTREAM_MESSAGE_LIMIT:
        text = text[:_UPSTREAM_MESSAGE_LIMIT] + '...'
    return json.dumps(text, ensure_ascii=False)


def _next_utc_midnight() -> str:
    """The API counts daily calls from 00:00 UTC."""
    tomorrow = datetime.now(timezone.utc).date() + timedelta(days=1)
    return datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=timezone.utc).isoformat()


def _classify_http_error(status_code: int, body: Any, headers: Any = None) -> tuple[str, str, bool, dict[str, Any]]:
    """Map an upstream response to (code, message, retriable, details), from the
    backend's `code` first and then the status. Never includes API key or header
    data; upstream text is quoted by `_quoted`."""
    upstream_msg = _upstream_text(body)
    upstream_code = body.get('code') if isinstance(body, dict) else None
    said = f' The API said: {_quoted(upstream_msg)}.' if upstream_msg else ''
    details: dict[str, Any] = {}

    if upstream_code in _DAILY_QUOTAS:
        quota, name = _DAILY_QUOTAS[upstream_code]
        resets_at = _next_utc_midnight()
        details.update(limit='daily', quota=quota, resets_at=resets_at)
        return (
            'rate_limited',
            f'The {name} is used up, so retrying today will not help. It resets at {resets_at}.{said}',
            False,
            details,
        )

    plan_body = isinstance(body, dict) and body.get('upgrade_required')
    keyword_403 = status_code == 403 and any(k in upstream_msg.lower() for k in ('tier', 'premium', 'upgrade', 'plan'))
    if upstream_code in _TIER_CODES or plan_body or keyword_403:
        return (
            'tier_forbidden',
            f'The current plan does not allow this request. Reduce it to fit the plan, or upgrade.{said}',
            False,
            details,
        )

    if status_code == 429 or upstream_code == 'rate_limit':
        retry_after = retry_after_seconds(headers, upstream_msg)
        wait = f'Retry after {retry_after} seconds.' if retry_after is not None else 'Wait before retrying.'
        if retry_after is not None:
            details['retry_after'] = retry_after
        return 'rate_limited', f'Rate limit exceeded. {wait}', True, details

    if status_code in (400, 422):
        fix = said or ' Check field names, types and options against gd_list_field_types.'
        return 'invalid_input', f'The API rejected the request as invalid.{fix}', False, details
    if status_code in (401, 403):
        return (
            'auth_failed',
            f'Authentication failed. Verify GENERATE_DATA_API_KEY is set to a valid, active API key.{said}',
            False,
            details,
        )
    if status_code == 404:
        return (
            'not_found',
            'The requested id or field_type was not found. Re-check it via gd_list_field_types or gd_list_projects.',
            False,
            details,
        )
    if status_code in (502, 503, 504):
        retry_after = retry_after_seconds(headers, '')
        if retry_after is not None:
            details['retry_after'] = retry_after
        source = 'The AI model provider' if upstream_code == 'provider_unavailable' else 'The API'
        return 'upstream_unavailable', f'{source} is temporarily unavailable (status {status_code}). Retry later.', True, details
    if 300 <= status_code < 400:
        return 'upstream_error', f'The API answered with a redirect (status {status_code}). {_BASE_URL_HINT}', False, details
    return (
        'upstream_error',
        f'The API returned an unexpected error (status {status_code}). Check service status before retrying.',
        False,
        details,
    )


def _redacted(value: Any) -> Any:
    """`value` with the API key removed from every string, in case upstream echoes it."""
    key = os.environ.get('GENERATE_DATA_API_KEY', '').strip()
    if not key:
        return value
    if isinstance(value, str):
        return value.replace(key, '[REDACTED]')
    if isinstance(value, dict):
        return {_redacted(k): _redacted(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redacted(item) for item in value]
    return value


def _err_from_status(status_code: int, body: Any, headers: Any = None) -> CallToolResult:
    body = _redacted(body)
    code, message, retriable, details = _classify_http_error(status_code, body, headers)
    if body is not None:
        # Upstream text stays delimited as JSON and capped, never trusted.
        upstream = json.dumps(body)
        if len(upstream) > _UPSTREAM_DETAIL_LIMIT:
            upstream = upstream[:_UPSTREAM_DETAIL_LIMIT] + '...[truncated]'
        details['upstream'] = upstream
    return _err(code, message, retriable=retriable, details=details)


def _response_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _err_from_exception(exc: Exception) -> CallToolResult:
    """The one place exceptions become envelopes. Messages name the exception
    class at most, so request details can't leak."""
    if isinstance(exc, ConfigurationError):
        return _err('configuration_error', str(exc))
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        return _err_from_status(response.status_code, _response_json(response), response.headers)
    if isinstance(exc, (httpx.UnsupportedProtocol, httpx.InvalidURL)):
        return _err('configuration_error', f'The API URL is not usable. {_BASE_URL_HINT}')
    if isinstance(exc, httpx.ConnectError) and is_dns_failure(exc):
        return _err('configuration_error', f'The API host name could not be resolved. {_BASE_URL_HINT}')
    if isinstance(exc, httpx.TimeoutException):
        return _err(
            'upstream_unavailable',
            f'The API did not respond in time ({exc.__class__.__name__}). Retry later, or with a smaller request.',
            retriable=True,
        )
    if isinstance(exc, httpx.HTTPError):
        return _err(
            'upstream_unavailable',
            f'Could not reach the Generate-Data.com API ({exc.__class__.__name__}). '
            f'Retry later. If this persists: {_BASE_URL_HINT}',
            retriable=True,
        )
    if isinstance(exc, UnexpectedResponse):
        return _err('upstream_error', f'The API returned {exc}. {_BASE_URL_HINT}')
    logger.exception('Unexpected error in tool')
    return _err('upstream_error', f'The MCP server hit an unexpected error ({exc.__class__.__name__}).')


def _tool(
    title: str,
    *,
    read_only: bool = False,
    destructive: bool = False,
    idempotent: bool = False,
) -> Callable[[Callable[..., Awaitable[CallToolResult]]], Callable[..., Awaitable[CallToolResult]]]:
    """Register an async tool. It returns `_ok`/`_err` or raises; either way the
    client gets the envelope. Returning CallToolResult rather than `str` is what
    keeps FastMCP from sending the payload again as `structuredContent`;
    `structured_output=False` stops it declaring a matching `outputSchema`.

    The annotations say how each tool affects the user's account. Anything that
    uses quota or writes a file is not read-only."""
    annotations = ToolAnnotations(
        title=title,
        readOnlyHint=read_only,
        destructiveHint=destructive,
        idempotentHint=idempotent,
        openWorldHint=True,
    )

    def register(fn: Callable[..., Awaitable[CallToolResult]]) -> Callable[..., Awaitable[CallToolResult]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> CallToolResult:
            try:
                return await fn(*args, **kwargs)
            except Exception as exc:
                return _err_from_exception(exc)

        return mcp.tool(title=title, annotations=annotations, structured_output=False)(wrapper)

    return register


def _error_only_text(parsed: Any) -> str | None:
    """The error message if a parsed body is nothing but an error object."""
    if isinstance(parsed, dict) and 'error' in parsed and parsed.keys() <= _ERROR_BODY_KEYS:
        return str(parsed['error'])
    return None


def _inspect_download(fmt: str, content: bytes, content_type: str) -> tuple[str | None, list[Any]]:
    """Check a 2xx body really is the requested file. Returns (problem, warnings
    embedded in the body). A proxy or challenge page can answer 200 with HTML, an
    unfollowed redirect has an empty body, and an error object can arrive with a
    200; none may be reported as generated data.

    Stays cheap: a ZIP's central directory is read without decompressing anything,
    and JSON too large to parse quickly only has its first byte checked.
    """
    if not content:
        return 'an empty response body', []
    if 'text/html' in content_type.lower():
        return 'an HTML page instead of data', []
    invalid = f'a body that is not a valid {fmt} file'
    if fmt == 'parquet':
        return (None if content.startswith(_PARQUET_MAGIC) and content.endswith(_PARQUET_MAGIC) else invalid), []
    if fmt in BINARY_FORMATS:
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                return (None if archive.infolist() else invalid), []
        except zipfile.BadZipFile:
            return invalid, []

    if fmt in _JSON_FORMATS:
        if len(content) > _JSON_PARSE_LIMIT:
            return (None if content.lstrip()[:1] in (b'{', b'[') else invalid), []
    elif not (content.lstrip()[:1] == b'{' and len(content) <= _ERROR_BODY_LIMIT):
        return None, []
    # One JSONL row with a single column named `error` is legitimate data.
    elif fmt == 'jsonl' and 'json' not in content_type.lower().replace('x-ndjson', ''):
        return None, []
    try:
        parsed = json.loads(content)
    except ValueError:
        return (invalid if fmt in _JSON_FORMATS else None), []
    error_text = _error_only_text(parsed)
    if error_text is not None:
        return f'an error instead of data: {_quoted(_redacted(error_text))}', []
    meta = parsed.get('meta') if isinstance(parsed, dict) else None
    warnings = meta.get('warnings') if isinstance(meta, dict) else None
    return None, warnings if fmt == 'json' and isinstance(warnings, list) else []


_WARNING_CODE_PREFIX = re.compile(r'^[a-z][a-z0-9_]*: ')


def _warning_text(warning: Any) -> str:
    if isinstance(warning, dict) and isinstance(warning.get('message'), str):
        warning = warning['message']
    text = warning if isinstance(warning, str) else json.dumps(warning, default=str)
    return _redacted(text)[:_WARNING_TEXT_LIMIT]


def _generation_warnings(response: httpx.Response, embedded: list[Any]) -> list[str]:
    """Warnings from the X-Generation-Warnings header (a JSON array of strings)
    and from the body. A header that can't be read still counts as a warning, so
    a degraded result is never presented as clean."""
    found: list[Any] = []
    raw = response.headers.get('x-generation-warnings')
    if raw is not None and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            # The header prefixes AI warnings with a code; the body holds the bare message.
            found.extend(_WARNING_CODE_PREFIX.sub('', item) if isinstance(item, str) else item for item in parsed)
        else:
            found.append('The API reported generation warnings in a form this server could not read.')
    found.extend(embedded)
    unique = list(dict.fromkeys(_warning_text(item) for item in found))
    return unique[:_WARNINGS_LIMIT]


def _usage_header(response: httpx.Response, name: str) -> int | None:
    raw = response.headers.get(name, '')
    return int(raw) if raw.isdigit() else None


async def _render_download(fmt: str, response: httpx.Response, label: str, stem: str, save_to_file: bool) -> CallToolResult:
    """Turn a generate response into an envelope: inline when small enough, else
    saved to a file so the whole result is always available. A failure to save is
    an error rather than a shortened result.

    `label` starts the summary ("Generated project 7"); `stem` starts the file name
    and must not come from tool arguments.
    """
    if response.status_code >= 300:
        return _err_from_status(response.status_code, _response_json(response), response.headers)
    content = response.content
    problem, embedded_warnings = await asyncio.to_thread(
        _inspect_download, fmt, content, response.headers.get('content-type', ''),
    )
    if problem:
        return _err(
            'upstream_error',
            f'The API returned {problem}. {_BASE_URL_HINT}',
            details={'status': response.status_code},
        )

    binary = fmt in BINARY_FORMATS
    text = None if binary else response.text
    too_large = len(content) > INLINE_BINARY_LIMIT if binary else len(text) > INLINE_TEXT_LIMIT
    rows = _usage_header(response, 'x-usage-rows-generated')
    what = f'{label} ({rows:,} rows)' if rows is not None else label

    if not (save_to_file or too_large):
        data = _encode_binary(fmt, content) if binary else _encode_text(fmt, text)
        summary = f"{what} as {fmt.upper()} ({data['size_bytes']:,} bytes)."
    else:
        try:
            path = await asyncio.to_thread(_save_output, fmt, stem, content)
        except OSError as exc:
            return _err(
                'configuration_error',
                f'Could not save the result to {_output_dir()}: {exc.strerror or exc.__class__.__name__}. '
                'Set GENERATE_DATA_OUTPUT_DIR to a writable directory, or request less data.',
            )
        data = _encode_saved(fmt, path, len(content), None if binary else _text_preview(text))
        summary = f'{what} as {fmt.upper()} ({len(content):,} bytes), saved in full to {path}.'
    if rows is not None:
        data['rows'] = rows
    for key, header in (('calls_today', 'x-usage-calls-today'), ('calls_remaining', 'x-usage-calls-remaining')):
        value = _usage_header(response, header)
        if value is not None:
            data[key] = value
    warnings = _generation_warnings(response, embedded_warnings)
    if warnings:
        data['warnings'] = warnings
        summary += (
            f' WARNING: generation reported {len(warnings)} problem(s), so this data may be '
            'incomplete or have empty values. Read data.warnings before using it.'
        )
    return _ok(summary, data)


@_tool('Generate dataset')
async def gd_generate_dataset(
    fields: list[dict[str, Any]],
    num_rows: NumRows = 100,
    format: DatasetFormat = 'csv',
    settings: GenerationSettings | None = None,
    save_to_file: bool = False,
    ai_groups: list[AIFieldGroup] | None = None,
) -> CallToolResult:
    """Generate synthetic dataset rows from a field list. Use this when the user
    wants actual generated data (as opposed to schema design or field-type
    discovery). `fields` is a list of `{"name": str, "type": str, "options": dict}`
    (get valid `type` values from `gd_list_field_types`/`gd_get_field_type_options`).
    `format` is one of `csv`, `json`, `xml`, `yaml`, `sql`, `jsonl`, `parquet`,
    `xlsx`, `hf-datasets`; Free keys can only use `csv` (others return
    `tier_forbidden`). `num_rows` is capped by plan: 100 on Free, 10,000 on Pro,
    and rows added by `settings.duplication_config` stay within that cap.
    `settings` holds optional generation settings. `ai_groups` is only for
    `ai_generated` fields (Pro) whose values should relate: each group names the
    `columns` to generate together from one `prompt`. Small results come back inline: `data.content` (text formats) or
    `data.content_base64` (`parquet`, `xlsx`, `hf-datasets` — never decode these
    as utf-8). Results too large to return inline, or any result when
    `save_to_file` is true, are saved in full and returned as `data.file_path`
    plus `data.preview` for text formats: read that file rather than regenerating.
    `data.rows` is the number of rows generated and `data.calls_remaining` the API
    calls left today, when the API reports them. If `data.warnings` is present the
    data may be incomplete or have empty values, so read it first. Failures return an error envelope
    saying what to do next; don't retry unless `error.retriable` is true."""
    fmt = format.lower()
    if fmt not in _DATASET_FORMATS:
        return _err(
            'unsupported_format',
            f'format must be one of {sorted(_DATASET_FORMATS)}; got {format!r}.',
        )
    response = await _client().generate_data(
        fields, num_rows=num_rows, fmt=fmt, settings=_plain(settings), ai_groups=_plain(ai_groups),
    )
    return await _render_download(fmt, response, 'Generated dataset', 'dataset', save_to_file)


@_tool('List field types', read_only=True, idempotent=True)
async def gd_list_field_types() -> CallToolResult:
    """List all available field types grouped by category. Use this to discover
    valid `type` values before calling `gd_generate_dataset` or to look up which
    category a field type belongs to. Takes no arguments. Returns the categorized
    field-type catalog in `data.categories`."""
    result = await _client().list_field_types()
    categories = result.get('categories')
    if not isinstance(categories, dict):
        raise UnexpectedResponse('JSON without a field-type catalog')
    return _ok(f'Found {len(categories)} field type categories.', result)


@_tool('Get field type options', read_only=True, idempotent=True)
async def gd_get_field_type_options(field_type: str) -> CallToolResult:
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
    result = await _client().get_field_options(field_type)
    return _ok(f"Retrieved configuration options for field type '{field_type}'.", result)


@_tool('Design schema')
async def gd_design_schema(
    prompt: str | None = None,
    locale: str = 'en_US',
    messages: list[dict[str, Any]] | None = None,
    current_schema: list[dict[str, Any]] | None = None,
) -> CallToolResult:
    """Design a dataset schema from natural language — one tool for both the
    first proposal and follow-up refinements. Behavior branches on args:
    - Propose: pass `prompt` (and optionally `locale`).
    - Refine: pass BOTH `messages` (the running conversation, a list of
      `{"role": "user"|"assistant", "content": str}`) AND `current_schema` (the
      `data.proposed_schema` array from the prior call). `prompt` is not used.
    Passing only one of `messages`/`current_schema`, or neither of them and no
    `prompt`, returns `invalid_input`. Returns `data.proposed_schema`, an array
    you can pass directly to `gd_generate_dataset` as `fields`. Each call uses the
    daily AI design quota (3 a day on Free, shared with `gd_design_project`): at the
    quota it returns a non-retriable `rate_limited` error with `details.resets_at`.
    It counts toward `calls_today` but isn't blocked by the daily call limit."""
    if (messages is None) != (current_schema is None):
        return _err('invalid_input', 'To refine, pass both messages and current_schema. To propose, pass only prompt.')
    if messages is None and not (prompt or '').strip():
        return _err('invalid_input', 'prompt is required to propose a schema.')
    client = _client()
    if messages is not None:
        result = await client.refine_schema(messages, current_schema)
        summary = 'Refined the schema based on the conversation.'
    else:
        result = await client.propose_schema(prompt, locale=locale)
        summary = f'Proposed a schema for: "{prompt}"'
    if not isinstance(result.get('proposed_schema'), list):
        raise UnexpectedResponse('JSON without a proposed schema')
    return _ok(summary, result)


@_tool('Get API usage', read_only=True, idempotent=True)
async def gd_get_usage() -> CallToolResult:
    """Get current API key usage stats (calls today, tier, limits). Use this to
    check remaining quota or confirm which tier the configured API key has
    before attempting a plan-limited call. Takes no arguments. Returns the
    usage stats in `data`."""
    result = await _client().get_usage()
    if 'tier' not in result:
        raise UnexpectedResponse('JSON without usage stats')
    calls = result.get('calls_today')
    summary = f"Current API tier: {result['tier']}." + (f' {calls} calls today.' if calls is not None else '')
    return _ok(summary, result)


@_tool('List Projects', read_only=True, idempotent=True)
async def gd_list_projects(limit: Limit = 20, offset: Offset = 0) -> CallToolResult:
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
    items = await _client().list_projects()
    total_count = len(items)
    page = items[offset:offset + limit]
    data = _paginate(page, limit, offset, total_count=total_count)
    return _ok(f'Found {total_count} project(s); returning {len(page)} starting at offset {offset}.', data)


@_tool('Generate saved Project')
async def gd_generate_project(project_id: ProjectId, format: ProjectFormat = 'zip', save_to_file: bool = False) -> CallToolResult:
    """Generate all tables in a Project and download the result. `project_id`
    must be a positive integer (get one from `gd_list_projects`). `format` is one
    of `zip` (default; one CSV per table), `xlsx`, `neo4j`, `jsonld`, `bundle`.
    Free keys can only use `zip` (others return `tier_forbidden`), and a project's
    rows and columns must fit the plan's limits. Rows for many-to-many relationships
    are limited to a plan budget, so a junction table can have fewer rows than the
    relationship could produce. Small results come back inline:
    `data.content_base64` (`zip`, `xlsx`, `neo4j` — never decode these as utf-8)
    or `data.content` (`jsonld`, `bundle`). Results too large to return inline, or
    any result when `save_to_file` is true, are saved in full and returned as
    `data.file_path`: read that file rather than regenerating. If `data.warnings`
    is present the data may be incomplete or have empty values, so read it first.
    Failures return an error envelope saying what to do next; don't retry unless
    `error.retriable` is true."""
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
    response = await _client().generate_project(project_id, fmt=fmt)
    return await _render_download(fmt, response, f'Generated project {project_id}', f'project-{project_id}', save_to_file)


@_tool('Generate Project without saving')
async def gd_generate_project_inline(
    tables: list[dict[str, Any]],
    relationships: list[dict[str, Any]] | None = None,
    name: str | None = None,
    settings: dict[str, Any] | None = None,
    domain_pack: str | None = None,
    format: ProjectFormat = 'zip',
    save_to_file: bool = False,
) -> CallToolResult:
    """Generate multi-table data once, without saving a Project. Takes the same
    `tables` and `relationships` as `gd_create_project`, or the `project` from
    `gd_design_project`, and returns results the same way as
    `gd_generate_project`, including the budget on many-to-many rows and
    `data.warnings`. Free keys can only use `zip`."""
    fmt = format.lower()
    if fmt not in _PROJECT_FORMATS:
        return _err(
            'unsupported_format',
            f'format must be one of {sorted(_PROJECT_FORMATS)}; got {format!r}.',
        )
    payload = _project_payload(name, tables, relationships, settings, domain_pack)
    response = await _client().generate_project_inline(payload, fmt=fmt)
    return await _render_download(fmt, response, 'Generated inline project', 'project-inline', save_to_file)


def _project_payload(
    name: str | None,
    tables: list[dict[str, Any]] | None,
    relationships: list[dict[str, Any]] | None,
    settings: dict[str, Any] | None,
    domain_pack: str | None,
) -> dict[str, Any]:
    """Only the keys the caller set: on update, a key that is sent replaces what's saved."""
    values = {
        'name': name,
        'tables': tables,
        'relationships': relationships,
        'settings': settings,
        'domain_pack': domain_pack,
    }
    return {key: value for key, value in values.items() if value is not None}


@_tool('Get Project', read_only=True, idempotent=True)
async def gd_get_project(project_id: ProjectId) -> CallToolResult:
    """Get one saved Project with its tables and relationships. Use this before
    `gd_update_project`, which replaces lists rather than merging them. Returns
    the Project in `data`."""
    try:
        project_id = _validate_project_id(project_id)
    except ValueError as exc:
        return _err('invalid_input', str(exc))
    project = await _client().get_project(project_id)
    return _ok(f"Project {project_id}: {project.get('name', 'unnamed')}.", project)


@_tool('Create Project')
async def gd_create_project(
    name: str,
    tables: list[dict[str, Any]],
    relationships: list[dict[str, Any]] | None = None,
    settings: dict[str, Any] | None = None,
    domain_pack: str | None = None,
) -> CallToolResult:
    """Save a multi-table Project to generate from later. `name` must be unique among
    your Projects. `gd_design_project` can propose the arguments. Each table is `{"name": str, "num_rows": int, "schema":
    [fields], "primary_key_field": str (optional)}`, where fields are as in
    `gd_generate_dataset`. Each relationship is `{"from_table": parent, "to_table":
    child, "from_field": the foreign key column on the child, "to_field": the
    parent's key, "cardinality": "one_to_many" (default)}`. Limits when saving: 3
    tables on Free, 20 on Pro. Total rows are limited when generating, not here (100
    on Free, 10,000 on Pro), so a saved Project can be too large to generate on the
    current plan. Returns the Project, with its new `id`, in `data`."""
    project = await _client().create_project(_project_payload(name, tables, relationships, settings, domain_pack))
    if 'id' not in project:
        raise UnexpectedResponse('JSON without a Project id')
    return _ok(f"Created project {project['id']}.", project)


@_tool('Update Project', destructive=True, idempotent=True)
async def gd_update_project(
    project_id: ProjectId,
    name: str | None = None,
    tables: list[dict[str, Any]] | None = None,
    relationships: list[dict[str, Any]] | None = None,
    settings: dict[str, Any] | None = None,
    domain_pack: str | None = None,
) -> CallToolResult:
    """Change a saved Project. Only the arguments you pass change, but each one
    replaces the saved value completely: `tables` replaces every table,
    `relationships` replaces every relationship, and `settings` replaces all
    settings. Sending new `tables` also needs `relationships` again, because
    relationships point at the tables being replaced. To change part of a list,
    call `gd_get_project`, edit the result and send the whole list. Takes the same
    shapes as `gd_create_project`. Returns the updated Project in `data`."""
    try:
        project_id = _validate_project_id(project_id)
    except ValueError as exc:
        return _err('invalid_input', str(exc))
    payload = _project_payload(name, tables, relationships, settings, domain_pack)
    if not payload:
        return _err('invalid_input', 'Pass at least one of name, tables, relationships, settings or domain_pack.')
    project = await _client().update_project(project_id, payload)
    return _ok(f'Updated project {project_id}.', project)


@_tool('Delete Project', destructive=True, idempotent=True)
async def gd_delete_project(project_id: ProjectId, confirm: Confirm = False) -> CallToolResult:
    """Permanently delete a saved Project and its tables. This cannot be undone,
    so only call it when the user has asked to delete this Project, and pass
    `confirm: true`. Without it, nothing is deleted and this returns
    `invalid_input`."""
    try:
        project_id = _validate_project_id(project_id)
    except ValueError as exc:
        return _err('invalid_input', str(exc))
    if confirm is not True:
        return _err(
            'invalid_input',
            f'Nothing was deleted. Deleting project {project_id} cannot be undone: '
            'check that the user wants this, then pass confirm: true.',
        )
    await _client().delete_project(project_id)
    return _ok(f'Deleted project {project_id}.', {'id': project_id, 'deleted': True})


@_tool('Design Project')
async def gd_design_project(prompt: str) -> CallToolResult:
    """Propose a multi-table Project from a description, e.g. "an online store with
    customers and orders". Nothing is saved. Returns `data.project` with `name`,
    `tables`, `relationships`, `settings` and `domain_pack`: pass those to
    `gd_generate_project_inline` for a one-off result or `gd_create_project` to
    save it. Row counts in the proposal may exceed the current plan's limits. Each
    call uses the daily AI design quota (3 a day on Free, shared with
    `gd_design_schema`): at the quota it returns a non-retriable `rate_limited`
    error with `details.resets_at`. It counts toward `calls_today` but isn't blocked
    by the daily call limit."""
    result = await _client().propose_project(prompt)
    project = result.get('project')
    if not isinstance(project, dict):
        raise UnexpectedResponse('JSON without a proposed Project')
    return _ok(f"Proposed project: {project.get('name', 'unnamed')}.", result)


@_tool('Validate field options', read_only=True, idempotent=True)
async def gd_validate_field_options(field_type: str, options: dict[str, Any]) -> CallToolResult:
    """Check whether `options` are valid for a field type, without generating
    anything. Invalid options are a normal result, not a failure: read
    `data.is_valid` and `data.errors`. An unknown `field_type` returns `not_found`."""
    try:
        field_type = _validate_field_type(field_type)
    except ValueError as exc:
        return _err('invalid_input', str(exc))
    result = await _client().validate_field_options(field_type, options)
    if 'is_valid' not in result:
        raise UnexpectedResponse('JSON without a validation result')
    verdict = 'are valid' if result['is_valid'] else 'are not valid'
    return _ok(f"Options for field type '{field_type}' {verdict}.", result)


def main():
    parser = argparse.ArgumentParser(prog='generate-data-mcp', description='MCP stdio server for Generate-Data.com.')
    parser.add_argument('--version', action='version', version=f'generate-data-mcp {__version__}')
    parser.parse_known_args()
    warning = insecure_base_url_warning(os.environ.get('GENERATE_DATA_API_BASE_URL'))
    if warning:
        print(warning, file=sys.stderr)
    mcp.run()


if __name__ == '__main__':
    main()
