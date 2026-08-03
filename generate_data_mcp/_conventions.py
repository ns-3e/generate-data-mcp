"""Shared tool-response conventions (envelopes, binary/text encoding, pagination,
input validation) per contract `.claude/program/contracts/tool-conventions.md`.

Every rebuilt tool (F-002/F-003) MUST call through these helpers rather than
hand-rolling response shapes. Signatures here are the frozen surface (contract
Sec. 8); internals are free to change.
"""
import base64
import json
import re

_FIELD_TYPE_RE = re.compile(r'^[a-z0-9_]+$')

# Contract Sec. 3 closed error-code enum.
_ERROR_CODES = frozenset({
    'invalid_input',
    'auth_failed',
    'tier_forbidden',
    'rate_limited',
    'not_found',
    'unsupported_format',
    'upstream_error',
    'upstream_unavailable',
})


def _ok(summary: str, data: dict) -> str:
    """Success envelope (contract Sec. 2). `summary` must be a non-empty
    natural-language answer; `data` is the curated payload."""
    if not summary:
        raise ValueError('summary must be a non-empty string')
    return json.dumps({'ok': True, 'summary': summary, 'data': data})


def _err(code: str, message: str, retriable: bool = False, details: dict | None = None) -> str:
    """Error envelope (contract Sec. 3). `code` must be in the closed enum;
    `message`/`details` must never carry secrets and any embedded upstream
    text must already be delimited by the caller (injection guard)."""
    if code not in _ERROR_CODES:
        raise ValueError(f'unknown error code: {code!r}')
    return json.dumps({
        'ok': False,
        'error': {
            'code': code,
            'message': message,
            'retriable': retriable,
            'details': details or {},
        },
    })


def _encode_binary(fmt: str, content: bytes) -> dict:
    """Binary `data` shape (contract Sec. 4). Never decodes bytes as utf-8;
    base64-encodes as-is. No truncation applied to binary payloads — the
    `truncated` flag is always False here (binary is returned whole)."""
    return {
        'format': fmt,
        'encoding': 'base64',
        'content_base64': base64.b64encode(content).decode('ascii'),
        'size_bytes': len(content),
        'truncated': False,
    }


def _encode_text(fmt: str, content: str, max_len: int = 50000) -> dict:
    """Text `data` shape (contract Sec. 4). Reports full `size_bytes`
    (utf-8 byte length) regardless of truncation; sets `truncated: true`
    rather than silently slicing without signaling it."""
    size_bytes = len(content.encode('utf-8'))
    truncated = len(content) > max_len
    return {
        'format': fmt,
        'encoding': 'utf-8',
        'content': content[:max_len] if truncated else content,
        'size_bytes': size_bytes,
        'truncated': truncated,
    }


def _paginate(items: list, limit: int, offset: int, total_count: int) -> dict:
    """List `data` shape (contract Sec. 5)."""
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
    """Contract Sec. 7: `field_type` must match `^[a-z0-9_]+$`; rejects
    `..`, `/`, `%`, backslash, whitespace (all excluded by the regex)."""
    if not isinstance(value, str) or not _FIELD_TYPE_RE.match(value):
        raise ValueError('field_type must match ^[a-z0-9_]+$ (lowercase letters, digits, underscore only)')
    return value


def _validate_project_id(value: int) -> int:
    """Contract Sec. 7: `project_id` must be a positive integer."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError('project_id must be a positive integer')
    return value


def _validate_pagination(limit: int, offset: int) -> tuple[int, int]:
    """Contract Sec. 5/7: `limit` default 20, max 100, min 1; `offset` >= 0."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 100):
        raise ValueError('limit must be an integer between 1 and 100')
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError('offset must be a non-negative integer')
    return limit, offset
