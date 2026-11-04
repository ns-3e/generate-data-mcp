"""Async HTTP client for Generate-Data.com API v2.

One GenerateDataClient, with one reused httpx.AsyncClient, serves the whole
server process. The server creates it on the first tool call, so its connection
pool belongs to the event loop that runs the tools.
"""
import asyncio
import ipaddress
import os
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

import httpx

from generate_data_mcp import __version__
from generate_data_mcp._conventions import BINARY_FORMATS

DEFAULT_BASE_URL = 'https://generate-data.com'
USER_AGENT = f'generate-data-mcp/{__version__}'

# Only GETs are retried: a repeated POST could spend quota or create data twice.
_MAX_GET_RETRIES = 2
_RETRY_BASE_DELAY = 0.5
_RETRY_MAX_DELAY = 5.0
_RETRIABLE_STATUSES = frozenset({429, 502, 503, 504})
_RETRY_AFTER_IN_TEXT = re.compile(r'(\d+)\s*seconds?')
_LOOPBACK_HOSTS = frozenset({'localhost'})


class ConfigurationError(ValueError):
    """The server's environment is wrong; retrying cannot help."""


class UnexpectedResponse(Exception):
    """A 2xx response whose body is not what this API returns."""


def retry_after_seconds(headers: Optional[Mapping[str, str]], upstream_msg: str = '') -> Optional[int]:
    """Seconds to wait, from a Retry-After header (delta or HTTP-date) or the
    "available in N seconds" wording DRF puts in throttled responses."""
    raw = (headers.get('retry-after') or '').strip() if headers is not None else ''
    if raw.isdigit():
        return int(raw)
    if raw:
        try:
            delta = parsedate_to_datetime(raw) - datetime.now(timezone.utc)
            return max(0, int(delta.total_seconds()))
        except (TypeError, ValueError):
            pass
    match = _RETRY_AFTER_IN_TEXT.search(upstream_msg)
    return int(match.group(1)) if match else None


def is_dns_failure(exc: httpx.ConnectError) -> bool:
    text = str(exc).lower()
    return any(s in text for s in ('name or service not known', 'nodename nor servname', 'getaddrinfo', 'name resolution'))


def insecure_base_url_warning(base_url: Optional[str]) -> Optional[str]:
    """A warning when the API key would cross a network unencrypted, else None."""
    if not base_url:
        return None
    try:
        parts = urlsplit(base_url)
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme != 'http' or not host or host.lower() in _LOOPBACK_HOSTS:
        return None
    try:
        if ipaddress.ip_address(host).is_loopback:
            return None
    except ValueError:
        pass
    return (
        f'WARNING: GENERATE_DATA_API_BASE_URL uses plain http:// to {host}, '
        'so your API key is sent unencrypted. Use https:// unless this is a trusted local network.'
    )


def _is_daily_limit(response: httpx.Response) -> bool:
    try:
        body = response.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get('code') == 'daily_call_limit'


def _validated_base_url(raw: str) -> str:
    base_url = raw.rstrip('/')
    problem = 'must be an http(s) URL such as https://generate-data.com'
    try:
        parts = urlsplit(base_url)
        valid = parts.scheme in ('http', 'https') and bool(parts.hostname)
    except ValueError:
        valid = False
    if not valid:
        raise ConfigurationError(f'GENERATE_DATA_API_BASE_URL {problem}.')
    if '/api' in parts.path:
        raise ConfigurationError(
            'GENERATE_DATA_API_BASE_URL must be the site origin without a path: '
            'remove /api/v2, which is added automatically.'
        )
    return base_url


class GenerateDataClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = 60.0,
    ):
        self.api_key = (api_key or os.environ.get('GENERATE_DATA_API_KEY', '')).strip()
        if not self.api_key:
            raise ConfigurationError(
                'GENERATE_DATA_API_KEY is required. Set it in the MCP client config and restart the server.'
            )
        # httpx would fail on these before sending anything, as if the network had.
        # The message must never include the key.
        if not (self.api_key.isascii() and self.api_key.isprintable()) or any(c.isspace() for c in self.api_key):
            raise ConfigurationError(
                'GENERATE_DATA_API_KEY contains characters an API key cannot have. '
                'Copy it again from Settings -> API Access and restart the server.'
            )
        self.base_url = _validated_base_url(
            base_url or os.environ.get('GENERATE_DATA_API_BASE_URL') or DEFAULT_BASE_URL
        )
        self.timeout = timeout
        self._http = httpx.AsyncClient(timeout=timeout, headers={'User-Agent': USER_AGENT})

    def _headers(self, accept: str = 'application/json') -> dict[str, str]:
        # Binary downloads pass Accept: */* so an upstream that honors Accept
        # doesn't coerce the body to JSON.
        return {'X-API-Key': self.api_key, 'Accept': accept}

    def _url(self, path: str) -> str:
        return f'{self.base_url}{path}'

    async def close(self) -> None:
        await self._http.aclose()

    @staticmethod
    def _parse(response: httpx.Response, expected: type) -> Any:
        """Raise HTTPStatusError for non-2xx, UnexpectedResponse for a body that
        is not the JSON type this endpoint returns (e.g. a proxy's HTML page)."""
        response.raise_for_status()
        try:
            body = response.json()
        except ValueError as exc:
            raise UnexpectedResponse('a body that is not JSON') from exc
        if not isinstance(body, expected):
            raise UnexpectedResponse(f'JSON of an unexpected shape ({type(body).__name__})')
        return body

    async def _get_with_retries(self, path: str) -> httpx.Response:
        """GET, retrying connection failures, throttling and gateway errors. A wait
        longer than the cap is not slept through: the caller gets the response and
        can report `retry_after`. The daily limit won't lift, so it is never retried."""
        for attempt in range(_MAX_GET_RETRIES + 1):
            delay = _RETRY_BASE_DELAY * 2 ** attempt
            try:
                response = await self._http.get(self._url(path), headers=self._headers(), timeout=30)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                if attempt == _MAX_GET_RETRIES or (isinstance(exc, httpx.ConnectError) and is_dns_failure(exc)):
                    raise
            else:
                if (
                    attempt == _MAX_GET_RETRIES
                    or response.status_code not in _RETRIABLE_STATUSES
                    or _is_daily_limit(response)
                ):
                    return response
                retry_after = retry_after_seconds(response.headers)
                if retry_after is not None:
                    delay = retry_after
                if delay > _RETRY_MAX_DELAY:
                    return response
            await asyncio.sleep(delay)
        raise AssertionError('unreachable: the last attempt returns or raises')

    async def _get_json(self, path: str, expected: type = dict) -> Any:
        return self._parse(await self._get_with_retries(path), expected)

    async def _post_json(self, path: str, payload: dict, expected: type = dict) -> Any:
        response = await self._http.post(self._url(path), json=payload, headers=self._headers(), timeout=60)
        return self._parse(response, expected)

    async def _put_json(self, path: str, payload: dict) -> dict:
        response = await self._http.put(self._url(path), json=payload, headers=self._headers(), timeout=60)
        return self._parse(response, dict)

    async def generate_data(
        self,
        fields: list[dict],
        num_rows: int = 100,
        fmt: str = 'csv',
        settings: Optional[dict] = None,
        ai_groups: Optional[list[dict]] = None,
    ) -> httpx.Response:
        payload: dict[str, Any] = {
            'fields': fields,
            'num_rows': num_rows,
            'format': fmt,
        }
        if settings:
            payload['settings'] = settings
        if ai_groups:
            # A sibling of `fields`, not part of `settings`.
            payload['ai_groups'] = ai_groups
        accept = '*/*' if fmt in BINARY_FORMATS else 'application/json'
        return await self._http.post(
            self._url('/api/v2/generate'),
            json=payload,
            headers=self._headers(accept=accept),
        )

    async def list_field_types(self) -> dict:
        return await self._get_json('/api/v2/field-types')

    async def get_field_options(self, field_type: str) -> dict:
        return await self._get_json(f'/api/v2/field-types/{field_type}/options')

    async def propose_schema(self, prompt: str, locale: str = 'en_US') -> dict:
        return await self._post_json('/api/v2/ai/schema/propose', {'prompt': prompt, 'locale': locale})

    async def refine_schema(self, messages: list[dict], schema: list[dict]) -> dict:
        return await self._post_json('/api/v2/ai/schema/refine', {'messages': messages, 'schema': schema})

    async def get_usage(self) -> dict:
        return await self._get_json('/api/v2/api-keys/usage')

    async def list_projects(self) -> list:
        return await self._get_json('/api/v2/projects', expected=list)

    async def get_project(self, project_id: int) -> dict:
        return await self._get_json(f'/api/v2/projects/{project_id}')

    async def create_project(self, payload: dict) -> dict:
        return await self._post_json('/api/v2/projects', payload)

    async def update_project(self, project_id: int, payload: dict) -> dict:
        return await self._put_json(f'/api/v2/projects/{project_id}', payload)

    async def delete_project(self, project_id: int) -> None:
        response = await self._http.delete(self._url(f'/api/v2/projects/{project_id}'), headers=self._headers(), timeout=30)
        response.raise_for_status()
        # The API answers 204. Any other 2xx (e.g. a proxy's page) hasn't deleted anything we know of.
        if response.status_code != 204:
            raise UnexpectedResponse(f'status {response.status_code} instead of confirming the deletion')

    async def propose_project(self, prompt: str) -> dict:
        return await self._post_json('/api/v2/ai/project/propose', {'prompt': prompt})

    async def validate_field_options(self, field_type: str, options: dict) -> dict:
        return await self._post_json(f'/api/v2/field-types/{field_type}/validate', {'options': options})

    async def generate_project_inline(self, payload: dict, fmt: str = 'zip') -> httpx.Response:
        accept = '*/*' if fmt in BINARY_FORMATS else 'application/json'
        return await self._http.post(
            self._url('/api/v2/projects/generate-inline'),
            # In the body for the same reason as generate_project below.
            json={**payload, 'format': fmt},
            headers=self._headers(accept=accept),
            timeout=120,
        )

    async def generate_project(self, project_id: int, fmt: str = 'zip') -> httpx.Response:
        accept = '*/*' if fmt in BINARY_FORMATS else 'application/json'
        return await self._http.post(
            self._url(f'/api/v2/projects/{project_id}/generate'),
            # Must be in the body: DRF reserves `?format=` for renderer selection
            # and answers 404 for any value it has no renderer for (e.g. zip).
            json={'format': fmt},
            headers=self._headers(accept=accept),
            timeout=120,
        )
