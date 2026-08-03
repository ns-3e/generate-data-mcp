# generate-data-mcp

Public MCP server for [Generate-Data.com](https://generate-data.com). Thin HTTP wrapper — no generation logic in this repo. May also be vendored elsewhere; this repository is the canonical published source.

## Tools

| Tool | API endpoint | Description |
|------|--------------|-------------|
| `gd_generate_dataset` | `POST /api/v2/generate` | Generate synthetic dataset rows from a field list |
| `gd_list_field_types` | `GET /api/v2/field-types` | List all available field types grouped by category |
| `gd_get_field_type_options` | `GET /api/v2/field-types/{type}/options` | Get the configuration option schema for one field type |
| `gd_design_schema` | `POST /api/v2/ai/schema/propose` \| `POST /api/v2/ai/schema/refine` | Design a dataset schema from natural language — one tool for both the first proposal and follow-up refinements |
| `gd_get_usage` | `GET /api/v2/api-keys/usage` | Get current API key usage stats (calls today, tier, limits) |
| `gd_list_projects` | `GET /api/v2/projects` | List the user's Projects (Premium) |
| `gd_generate_project` | `POST /api/v2/projects/{id}/generate` | Generate all tables in a Project and download the result (Premium) |

**Premium:** `gd_list_projects` and `gd_generate_project` require a Premium API key.

### Migrating from v1

v2.0.0 renames every tool (breaking change). Old name -> new name:

- `generate_data` -> `gd_generate_dataset`
- `list_field_types` -> `gd_list_field_types`
- `get_field_options` -> `gd_get_field_type_options`
- `propose_schema` -> `gd_design_schema` (first call, no `messages`/`current_schema`)
- `refine_schema` -> `gd_design_schema` (pass `messages` + `current_schema` together)
- `get_api_usage` -> `gd_get_usage`
- `list_projects` -> `gd_list_projects` (now paginated: `limit`/`offset`)
- `generate_project` -> `gd_generate_project` (binary formats now returned base64-encoded, not corrupted utf-8)

### Tool examples

**gd_generate_dataset** — minimum payload:

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

**gd_design_schema** — propose: `prompt`: `"E-commerce customers with name, email, and signup date"`. Refine: pass `messages` (conversation so far) and `current_schema` (prior result) together.

**gd_get_usage** — no parameters; returns calls today, rows generated, tier.

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

Not yet published to PyPI — until then, run directly from this repo:

```bash
uvx --from git+https://github.com/ns-3e/generate-data-mcp generate-data-mcp
```

Once published to PyPI:

```bash
uvx generate-data-mcp
# or persistent install:
uv tool install generate-data-mcp
```

Fallback (pip):

```bash
pip install git+https://github.com/ns-3e/generate-data-mcp.git
# or for development:
git clone https://github.com/ns-3e/generate-data-mcp.git
cd generate-data-mcp
pip install -e ".[dev]"
```

## Claude Desktop / Cursor

> Do not commit a config file containing your real API key.

```json
{
  "mcpServers": {
    "generate-data": {
      "command": "uvx",
      "args": ["generate-data-mcp"],
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
generate-data-mcp
# or: python -m generate_data_mcp.server
```

## Verify

1. `export GENERATE_DATA_API_KEY=...`
2. Invoke `gd_get_usage` from your MCP client — should return tier and call counts.
3. Invoke `gd_list_field_types` — should return category map.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `GENERATE_DATA_API_KEY is required` | Set env var before starting the server |
| HTTP 401 | Invalid or deactivated key |
| HTTP 429 / `rate_limit` | Per-minute or daily cap hit; wait or upgrade tier |
| HTTP 403 / `tier_forbidden` | Free tier lacks access; upgrade plan |

## Tests

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

## API docs

Docs live on [generate-data.com](https://generate-data.com); a canonical `docs/API_V2.md` cross-repo link could not be confirmed as still valid — see this repo's tool docstrings (`generate_data_mcp/server.py`) for the authoritative request/response shapes.
