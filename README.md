# generate-data-mcp

Public MCP server for [Generate-Data.com](https://generate-data.com). Thin HTTP wrapper — no generation logic in this repo.

> **Monorepo sync:** A copy of this package also lives in the main [generate-data.com](https://github.com/generate-data/generate-data.com) repo under `generate-data-mcp/`. This repository is the canonical published source.

## Tools

| Tool | API endpoint | Description |
|------|--------------|-------------|
| `generate_data` | `POST /api/v2/generate` | Generate rows from a field schema |
| `list_field_types` | `GET /api/v2/field-types` | List field types by category |
| `get_field_options` | `GET /api/v2/field-types/{type}/options` | Options schema for a field type |
| `propose_schema` | `POST /api/v2/ai/schema/propose` | AI schema from natural language |
| `refine_schema` | `POST /api/v2/ai/schema/refine` | Refine schema via conversation |
| `get_api_usage` | `GET /api/v2/api-keys/usage` | Current key usage stats |
| `list_projects` | `GET /api/v2/projects` | List Projects (Premium) |
| `generate_project` | `POST /api/v2/projects/{id}/generate` | Generate Project ZIP |

**V2.1:** `list_projects` and `generate_project` require a Premium API key.

### Tool examples

**generate_data** — minimum payload:

```json
{
  "fields": [
    {"name": "first_name", "type": "first_name", "options": {}},
    {"name": "email", "type": "email", "options": {}}
  ],
  "num_rows": 10,
  "format": "csv"
}
```

**propose_schema** — `prompt`: `"E-commerce customers with name, email, and signup date"`

**get_api_usage** — no parameters; returns calls today, rows generated, tier.

## Tier limits (API key)

| Capability | Free | Premium |
|------------|------|---------|
| Max rows / request | 100 | 100,000 |
| Max columns | 10 | 50 |
| Formats | CSV | CSV, JSON, XML, Parquet |
| Daily API calls | 10 | 1,000 |

Limits are enforced by the Django API, not this MCP server.

## Configuration

| Variable | Required | Default |
|----------|----------|---------|
| `GENERATE_DATA_API_KEY` | Yes | — |
| `GENERATE_DATA_API_BASE_URL` | No | `https://api.generate-data.com` |

Create an API key in **Settings → API Access** on generate-data.com.

## Install

```bash
pip install git+https://github.com/generate-data/generate-data-mcp.git
# or for development:
git clone https://github.com/generate-data/generate-data-mcp.git
cd generate-data-mcp
pip install -e ".[dev]"
```

## Claude Desktop / Cursor

```json
{
  "mcpServers": {
    "generate-data": {
      "command": "python",
      "args": ["-m", "generate_data_mcp.server"],
      "env": {
        "GENERATE_DATA_API_KEY": "your-uuid-key-here",
        "GENERATE_DATA_API_BASE_URL": "https://api.generate-data.com"
      }
    }
  }
}
```

For local development against a running Django backend:

```json
"GENERATE_DATA_API_BASE_URL": "http://localhost:8000"
```

## Run

```bash
export GENERATE_DATA_API_KEY=your-key
python -m generate_data_mcp.server
```

## Verify

1. `export GENERATE_DATA_API_KEY=...`
2. Invoke `get_api_usage` from your MCP client — should return tier and call counts.
3. Invoke `list_field_types` — should return category map.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `GENERATE_DATA_API_KEY is required` | Set env var before starting the server |
| HTTP 401 | Invalid or deactivated key |
| HTTP 429 / `rate_limit` | Per-minute or daily cap hit; wait or upgrade tier |
| HTTP 403 / `format_not_allowed` | Free tier is CSV-only |
| Connection refused | Check `GENERATE_DATA_API_BASE_URL` |

## Tests

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

## API docs

See [docs/API_V2.md](https://github.com/generate-data/generate-data.com/blob/main/docs/API_V2.md) in the main repo.
