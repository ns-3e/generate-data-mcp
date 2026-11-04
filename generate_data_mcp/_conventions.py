"""Shared tool-response conventions: envelopes, inline/file output, pagination
and input validation. Every tool calls through these helpers rather than
hand-rolling response shapes.

Tools return an MCP `CallToolResult` whose text content is the JSON envelope;
`isError` is true exactly when the envelope's `ok` is false.
"""
import base64
import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from mcp.types import CallToolResult, TextContent

_FIELD_TYPE_RE = re.compile(r'[a-z0-9_]+')

# Closed error-code enum, documented in the README.
_ERROR_CODES = frozenset({
    'invalid_input',
    'auth_failed',
    'tier_forbidden',
    'rate_limited',
    'not_found',
    'unsupported_format',
    'upstream_error',
    'upstream_unavailable',
    'configuration_error',
})

# Formats the API returns as binary bodies. `hf-datasets` and `neo4j` are ZIPs.
BINARY_FORMATS = frozenset({'parquet', 'xlsx', 'hf-datasets', 'zip', 'neo4j'})

# Larger results are saved to a file, because returning them would flood the
# agent's context. The binary limit is about 44K chars once base64-encoded.
INLINE_TEXT_LIMIT = 50_000
INLINE_BINARY_LIMIT = 32 * 1024
PREVIEW_CHARS = 500
DEFAULT_OUTPUT_DIR = '~/generate-data-output'

_FILE_EXTENSIONS = {
    'hf-datasets': 'zip',
    'neo4j': 'zip',
    'bundle': 'json',
}


def _result(envelope: dict, is_error: bool) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type='text', text=json.dumps(envelope))],
        isError=is_error,
    )


def _ok(summary: str, data: dict) -> CallToolResult:
    """Success envelope. `summary` must be a non-empty natural-language answer;
    `data` is the curated payload."""
    if not summary:
        raise ValueError('summary must be a non-empty string')
    return _result({'ok': True, 'summary': summary, 'data': data}, is_error=False)


def _err(code: str, message: str, retriable: bool = False, details: dict | None = None) -> CallToolResult:
    """Error envelope with `isError: true`. `code` must be in the closed enum;
    `message`/`details` must never carry secrets, and any embedded upstream text
    must already be delimited by the caller (injection guard)."""
    if code not in _ERROR_CODES:
        raise ValueError(f'unknown error code: {code!r}')
    envelope = {
        'ok': False,
        'error': {
            'code': code,
            'message': message,
            'retriable': retriable,
            'details': details or {},
        },
    }
    return _result(envelope, is_error=True)


def _encode_binary(fmt: str, content: bytes) -> dict:
    """Inline binary `data` shape. Never decodes bytes as utf-8."""
    return {
        'format': fmt,
        'encoding': 'base64',
        'content_base64': base64.b64encode(content).decode('ascii'),
        'size_bytes': len(content),
        'saved_to_file': False,
        'truncated': False,
    }


def _encode_text(fmt: str, content: str) -> dict:
    """Inline text `data` shape. Callers save oversized text to a file instead
    of cutting it, so `truncated` is always false."""
    return {
        'format': fmt,
        'encoding': 'utf-8',
        'content': content,
        'size_bytes': len(content.encode('utf-8')),
        'saved_to_file': False,
        'truncated': False,
    }


def _encode_saved(fmt: str, path: Path, size_bytes: int, preview: str | None) -> dict:
    """`data` shape for a result written to disk. The file holds the whole result."""
    data = {
        'format': fmt,
        'encoding': 'binary' if fmt in BINARY_FORMATS else 'utf-8',
        'file_path': str(path),
        'size_bytes': size_bytes,
        'saved_to_file': True,
        'truncated': False,
    }
    if preview is not None:
        data['preview'] = preview
    return data


def _text_preview(text: str) -> str:
    """The start of a text result, ending on a whole line where possible."""
    if len(text) <= PREVIEW_CHARS:
        return text
    head = text[:PREVIEW_CHARS]
    newline = head.rfind('\n')
    return head[:newline] if newline > 0 else head


def _output_dir() -> Path:
    return Path(os.environ.get('GENERATE_DATA_OUTPUT_DIR') or DEFAULT_OUTPUT_DIR).expanduser()


def _save_output(fmt: str, stem: str, content: bytes) -> Path:
    """Write `content` to a new file in the output directory and return its path.

    `stem` must come from server code, never from tool arguments, so callers
    cannot influence where the file lands. Exclusive creation means an existing
    file is never overwritten.
    """
    directory = _output_dir()
    directory.mkdir(parents=True, exist_ok=True)
    extension = _FILE_EXTENSIONS.get(fmt, fmt)
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    while True:
        path = directory / f'{stem}-{timestamp}-{secrets.token_hex(4)}.{extension}'
        try:
            with open(path, 'xb') as handle:
                handle.write(content)
        except FileExistsError:
            continue
        return path.resolve()


def _paginate(items: list, limit: int, offset: int, total_count: int) -> dict:
    """List `data` shape."""
    has_more = offset + len(items) < total_count
    return {
        'items': items,
        'limit': limit,
        'offset': offset,
        'total_count': total_count,
        'has_more': has_more,
        'next_offset': offset + limit if has_more else None,
    }


def _validate_field_type(value: str) -> str:
    """`field_type` must be only lowercase letters, digits and underscores.
    Uses fullmatch because `$` would allow a trailing newline."""
    if not isinstance(value, str) or not _FIELD_TYPE_RE.fullmatch(value):
        raise ValueError('field_type must match ^[a-z0-9_]+$ (lowercase letters, digits, underscore only)')
    return value


def _validate_project_id(value: int) -> int:
    """`project_id` must be a positive integer."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError('project_id must be a positive integer')
    return value


def _validate_pagination(limit: int, offset: int) -> tuple[int, int]:
    """`limit` is 1-100 and `offset` is at least 0."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 100):
        raise ValueError('limit must be an integer between 1 and 100')
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError('offset must be a non-negative integer')
    return limit, offset
