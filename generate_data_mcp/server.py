"""MCP stdio server entrypoint."""
import json
import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from generate_data_mcp.client import GenerateDataClient

mcp = FastMCP('generate-data')


def _client() -> GenerateDataClient:
    return GenerateDataClient()


@mcp.tool()
def generate_data(
    fields: list[dict[str, Any]],
    num_rows: int = 100,
    format: str = 'csv',
    settings: dict[str, Any] | None = None,
) -> str:
    """Generate synthetic data rows. Returns CSV/text content or error message."""
    client = _client()
    response = client.generate_data(fields, num_rows=num_rows, fmt=format, settings=settings)
    if response.status_code >= 400:
        try:
            err = response.json()
            return json.dumps({'error': err.get('error', response.text), 'status': response.status_code})
        except Exception:
            return json.dumps({'error': response.text, 'status': response.status_code})
    content_type = response.headers.get('content-type', '')
    if 'json' in content_type:
        return response.text
    return response.text[:50000]


@mcp.tool()
def list_field_types() -> str:
    """List available field types grouped by category."""
    return json.dumps(_client().list_field_types())


@mcp.tool()
def get_field_options(field_type: str) -> str:
    """Get configuration schema for a field type."""
    return json.dumps(_client().get_field_options(field_type))


@mcp.tool()
def propose_schema(prompt: str, locale: str = 'en_US') -> str:
    """Propose a schema from natural language."""
    return json.dumps(_client().propose_schema(prompt, locale=locale))


@mcp.tool()
def refine_schema(messages: list[dict], current_schema: dict) -> str:
    """Refine an existing schema via conversation."""
    return json.dumps(_client().refine_schema(messages, current_schema))


@mcp.tool()
def get_api_usage() -> str:
    """Get current API key usage stats."""
    return json.dumps(_client().get_usage())


@mcp.tool()
def list_projects() -> str:
    """List user's Projects (Premium)."""
    try:
        return json.dumps(_client().list_projects())
    except Exception as exc:
        return json.dumps({'error': str(exc)})


@mcp.tool()
def generate_project(project_id: int, format: str = 'zip') -> str:
    """Generate all tables in a Project. Returns ZIP bytes as text or JSON error."""
    client = _client()
    response = client.generate_project(project_id, fmt=format)
    if response.status_code >= 400:
        try:
            return json.dumps({'error': response.json().get('error', response.text), 'status': response.status_code})
        except Exception:
            return json.dumps({'error': response.text, 'status': response.status_code})
    return response.content[:50000].decode('utf-8', errors='replace')


def main():
    mcp.run()


if __name__ == '__main__':
    main()
