"""HTTP client for Generate-Data.com API v2."""
import os
from typing import Any, Optional

import httpx

DEFAULT_BASE_URL = 'https://api.generate-data.com'


class GenerateDataClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = 60.0,
    ):
        self.api_key = api_key or os.environ.get('GENERATE_DATA_API_KEY', '')
        self.base_url = (base_url or os.environ.get('GENERATE_DATA_API_BASE_URL') or DEFAULT_BASE_URL).rstrip('/')
        self.timeout = timeout
        if not self.api_key:
            raise ValueError('GENERATE_DATA_API_KEY is required')

    def _headers(self) -> dict[str, str]:
        return {'X-API-Key': self.api_key, 'Accept': 'application/json'}

    def _url(self, path: str) -> str:
        return f'{self.base_url}{path}'

    def generate_data(
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
        return httpx.post(
            self._url('/api/v2/generate'),
            json=payload,
            headers=self._headers(),
            timeout=self.timeout,
        )

    def list_field_types(self) -> dict:
        r = httpx.get(self._url('/api/v2/field-types'), headers=self._headers(), timeout=30)
        r.raise_for_status()
        return r.json()

    def get_field_options(self, field_type: str) -> dict:
        r = httpx.get(
            self._url(f'/api/v2/field-types/{field_type}/options'),
            headers=self._headers(),
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def propose_schema(self, prompt: str, locale: str = 'en_US') -> dict:
        r = httpx.post(
            self._url('/api/v2/ai/schema/propose'),
            json={'prompt': prompt, 'locale': locale},
            headers=self._headers(),
            timeout=60,
        )
        r.raise_for_status()
        return r.json()

    def refine_schema(self, messages: list[dict], current_schema: dict) -> dict:
        r = httpx.post(
            self._url('/api/v2/ai/schema/refine'),
            json={'messages': messages, 'current_schema': current_schema},
            headers=self._headers(),
            timeout=60,
        )
        r.raise_for_status()
        return r.json()

    def get_usage(self) -> dict:
        r = httpx.get(self._url('/api/v2/api-keys/usage'), headers=self._headers(), timeout=30)
        r.raise_for_status()
        return r.json()

    def list_projects(self) -> list:
        r = httpx.get(self._url('/api/v2/projects'), headers=self._headers(), timeout=30)
        r.raise_for_status()
        return r.json()

    def generate_project(self, project_id: int, fmt: str = 'zip') -> httpx.Response:
        return httpx.post(
            self._url(f'/api/v2/projects/{project_id}/generate'),
            params={'format': fmt},
            headers=self._headers(),
            timeout=120,
        )
