"""Async HTTP client for Generate-Data.com API v2.

One GenerateDataClient, with one reused httpx.AsyncClient, serves the whole
server process. The server creates it on the first tool call, so its connection
pool belongs to the event loop that runs the tools.
"""
import os
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx

from generate_data_mcp._conventions import BINARY_FORMATS

DEFAULT_BASE_URL = 'https://generate-data.com'


class ConfigurationError(ValueError):
    """The server's environment is wrong; retrying cannot help."""


class UnexpectedResponse(Exception):
    """A 2xx response whose body is not what this API returns."""


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
        self.api_key = api_key or os.environ.get('GENERATE_DATA_API_KEY', '')
        if not self.api_key:
            raise ConfigurationError(
                'GENERATE_DATA_API_KEY is required. Set it in the MCP client config and restart the server.'
            )
        self.base_url = _validated_base_url(
            base_url or os.environ.get('GENERATE_DATA_API_BASE_URL') or DEFAULT_BASE_URL
        )
        self.timeout = timeout
        self._http = httpx.AsyncClient(timeout=timeout)

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

    async def _get_json(self, path: str, expected: type = dict) -> Any:
        response = await self._http.get(self._url(path), headers=self._headers(), timeout=30)
        return self._parse(response, expected)

    async def _post_json(self, path: str, payload: dict, expected: type = dict) -> Any:
        response = await self._http.post(self._url(path), json=payload, headers=self._headers(), timeout=60)
        return self._parse(response, expected)

    async def generate_data(
        self,
        fields: list[dict],
        num_rows: int = 100,
        fmt: str = 'csv',
        settings: Optional[dict] = None,
    ) -> httpx.Response:
        payload: dict[str, Any] = {
            'fields': fields,
            'num_rows': num_rows,
            'format': fmt,
        }
        if settings:
            payload['settings'] = settings
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
