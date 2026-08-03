# generate-data-mcp

[![PyPI](https://img.shields.io/pypi/v/generate-data-mcp)](https://pypi.org/project/generate-data-mcp/)

An MCP server for [Generate-Data.com](https://generate-data.com) — generate synthetic datasets, design schemas from natural language, and manage Projects, straight from your agent.

Thin HTTP wrapper over the Generate-Data.com API. No generation logic lives in this repo — it's a curated, agent-friendly interface onto the real thing: 7 tools, one consistent response shape, binary-safe output, and server-side validation on every input.

## Installation (30-second setup)

You need a Generate-Data.com API key first — create one in **Settings → API Access** on [generate-data.com](https://generate-data.com).

<details open>
<summary><strong>Claude Desktop / Cursor (recommended)</strong></summary>

Add this to your MCP client config (Claude Desktop: `claude_desktop_config.json`; Cursor: `.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "generate-data": {
      "command": "uvx",
      "args": ["generate-data-mcp"],
      "env": {
        "GENERATE_DATA_API_KEY": "your-uuid-key-here"
      }
    }
  }
}
```

`uvx` fetches and runs the latest published version on demand — no separate install step, nothing to update by hand. Restart your client and the 7 `gd_*` tools are available.

> Do not commit a config file containing your real API key.

</details>

<details>
<summary><strong>uv / uvx (any MCP client)</strong></summary>

```bash
# run once, ad hoc:
uvx generate-data-mcp

# or install it as a persistent CLI tool:
uv tool install generate-data-mcp
```

</details>

<details>
<summary><strong>pip (fallback)</strong></summary>

```bash
pip install generate-data-mcp
```

For local development against this repo directly:

```bash
git clone https://github.com/ns-3e/generate-data-mcp.git
cd generate-data-mcp
pip install -e ".[dev]"
```

</details>

### Verify it works

```bash
export GENERATE_DATA_API_KEY=your-key
generate-data-mcp
```

From your MCP client, invoke `gd_get_usage` — it should return your tier and call counts. Then invoke `gd_list_field_types` — it should return the category map.

### Bam — you're ready to generate data.

Ask your agent something like *"generate 50 rows of fake e-commerce customers as CSV"* and it will call `gd_design_schema` then `gd_generate_dataset` on its own.

## Quick start

A typical session looks like this — the agent chains tools on its own, you just describe the outcome:

1. **Discover what's possible.** `gd_list_field_types` — see every field type, grouped by category.
2. **Design a schema.** `gd_design_schema(prompt="E-commerce customers with name, email, and signup date")` — proposes a `fields` array from plain English.
3. **Generate the data.** `gd_generate_dataset(fields=..., num_rows=10, format="csv")` — returns the rows.
4. **Refine if needed.** Call `gd_design_schema` again, this time passing `messages` (the running conversation) + `current_schema` (the prior result) together — it refines instead of proposing fresh.

Every tool returns the same envelope: `{"ok": true, "summary": "...", "data": {...}}` on success, or `{"ok": false, "error": {"code": ..., "message": ...}}` on failure — errors always tell you what to do next, never a raw stack trace.

### Local development

```json
{
  "env": { "GENERATE_DATA_API_BASE_URL": "http://localhost:8000" }
}
```

Point at a locally running Django backend instead of the hosted API.

### Migrating from v1

v2.0.0 renames every tool (breaking change). Old name → new name:

- `generate_data` → `gd_generate_dataset`
- `list_field_types` → `gd_list_field_types`
- `get_field_options` → `gd_get_field_type_options`
- `propose_schema` → `gd_design_schema` (first call, no `messages`/`current_schema`)
- `refine_schema` → `gd_design_schema` (pass `messages` + `current_schema` together)
- `get_api_usage` → `gd_get_usage`
- `list_projects` → `gd_list_projects` (now paginated: `limit`/`offset`)
- `generate_project` → `gd_generate_project` (binary formats now returned base64-encoded, not corrupted utf-8)

## Reference

All 7 tools, split by tier.

### Free tier

- **[gd_generate_dataset](./generate_data_mcp/server.py)** — Generate synthetic dataset rows from a field list. `format`: `csv`, `json`, `xml`, `parquet`, or `zip` (binary formats return base64-encoded).
- **[gd_list_field_types](./generate_data_mcp/server.py)** — List all available field types grouped by category. Takes no arguments.
- **[gd_get_field_type_options](./generate_data_mcp/server.py)** — Get the configuration option schema for one field type. `field_type` must match `^[a-z0-9_]+$`.
- **[gd_design_schema](./generate_data_mcp/server.py)** — Design a dataset schema from natural language, or refine an existing one — one tool for both the first proposal and follow-up conversation turns.
- **[gd_get_usage](./generate_data_mcp/server.py)** — Get current API key usage stats: calls today, tier, limits. Takes no arguments.

### Premium tier

Requires a Premium API key — Free-tier keys get a `tier_forbidden` error.

- **[gd_list_projects](./generate_data_mcp/server.py)** — List the user's Projects, paginated (`limit`/`offset`, default 20/0).
- **[gd_generate_project](./generate_data_mcp/server.py)** — Generate all tables in a Project and download the result. Same format/binary rules as `gd_generate_dataset`.

### Tier limits (API key)

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

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `GENERATE_DATA_API_KEY is required` | Set env var before starting the server |
| HTTP 401 / `auth_failed` | Invalid or deactivated key |
| HTTP 429 / `rate_limited` | Per-minute or daily cap hit; wait or upgrade tier |
| HTTP 403 / `tier_forbidden` | Free tier lacks access; upgrade plan |
| `unsupported_format` | `format` must be one of `csv`, `json`, `xml`, `parquet`, `zip` |
| `invalid_input` on a field type or project ID | Value failed server-side validation before any request was sent — check spelling/type |

## Development

```bash
git clone https://github.com/ns-3e/generate-data-mcp.git
cd generate-data-mcp
pip install -e ".[dev]"
pytest tests/ -v
```

## API docs

Docs live on [generate-data.com](https://generate-data.com). See this repo's tool docstrings (`generate_data_mcp/server.py`) for the authoritative request/response shapes.
