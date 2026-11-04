# generate-data-mcp

<!-- mcp-name: io.github.ns-3e/generate-data-mcp -->

[![PyPI](https://img.shields.io/pypi/v/generate-data-mcp)](https://pypi.org/project/generate-data-mcp/)

An MCP server for [Generate-Data.com](https://generate-data.com) — generate synthetic datasets, design schemas from natural language, and manage Projects, straight from your agent.

Thin HTTP wrapper over the Generate-Data.com API. No generation logic lives in this repo — it's a curated, agent-friendly interface onto the real thing: 14 tools, one consistent response shape, binary-safe output, and server-side validation on every input.

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

`uvx` fetches and runs the latest published version on demand — no separate install step, nothing to update by hand. Restart your client and the 14 `gd_*` tools are available.

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
4. **Refine if needed.** Call `gd_design_schema` again, this time passing `messages` (the running conversation) + `current_schema` (the prior `proposed_schema` array) together — it refines instead of proposing fresh.

Every tool returns the same envelope: `{"ok": true, "summary": "...", "data": {...}}` on success, or `{"ok": false, "error": {"code": ..., "message": ..., "retriable": ..., "details": {...}}}` on failure, with MCP's `isError` set to true. Errors always tell you what to do next, never a raw stack trace.

### Large results

Small results come back inline. Text over 50,000 characters and binary over 32 KiB are saved in full to a new file and returned as `data.file_path`, with `data.preview` for text formats. Pass `save_to_file: true` to save a result of any size.

Files go to `GENERATE_DATA_OUTPUT_DIR` (default `~/generate-data-output`). They're never overwritten and never deleted automatically, so clear the directory yourself when you're done. In the Docker image, that path is inside the container: mount a volume there and set `GENERATE_DATA_OUTPUT_DIR` to read the files from outside.

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

All 14 tools work on every plan, within that plan's limits.

### Generate data

- **gd_generate_dataset** — Generate rows from a field list. `format`: `csv`, `json`, `xml`, `yaml`, `sql`, `jsonl`, `parquet`, `xlsx`, or `hf-datasets` (the last three return base64-encoded). Optional `settings`, `ai_groups` (Pro, for `ai_generated` columns that should relate) and `save_to_file`. Returns `rows` and `calls_remaining` when the API reports them.
- **gd_generate_project** — Generate all tables in a saved Project. `format`: `zip`, `xlsx`, `neo4j` (base64-encoded), `jsonld`, or `bundle`.
- **gd_generate_project_inline** — Generate multi-table data once, without saving a Project. Same formats as `gd_generate_project`.

### Design and discover

- **gd_design_schema** — Propose a dataset schema from natural language, or refine one by passing `messages` and `current_schema`.
- **gd_design_project** — Propose a multi-table Project from natural language. Nothing is saved.
- **gd_list_field_types** — List all field types grouped by category.
- **gd_get_field_type_options** — Get the option schema for one field type. `field_type` must match `^[a-z0-9_]+$`.
- **gd_validate_field_options** — Check options for a field type without generating anything. Returns `is_valid` and `errors`.
- **gd_get_usage** — Get your plan, calls today and limits.

### Manage Projects

- **gd_list_projects** — List your Projects, paginated (`limit`/`offset`, default 20/0).
- **gd_get_project** — Get one Project with its tables and relationships.
- **gd_create_project** — Save a Project from `name`, `tables` and `relationships`.
- **gd_update_project** — Change a Project. Only the arguments you pass change, but each one replaces the saved value completely, so sending `tables` replaces every table.
- **gd_delete_project** — Permanently delete a Project. Does nothing unless you pass `confirm: true`.

Every tool declares read-only and destructive hints, so MCP clients can decide which calls need your approval. Read-only tools are retried automatically (up to twice) on connection failures, throttling and gateway errors. Anything that can create data or use quota is never retried automatically.

### Tier limits (API key)

| Capability | Free | Pro |
|------------|------|-----|
| Max rows / request | 100 | 10,000 |
| Max columns | 10 | 50 |
| Max tables / project | 3 | 20 |
| Dataset formats | CSV | CSV, JSON, XML, YAML, SQL, JSONL, Parquet, XLSX, HF Datasets |
| Project formats | ZIP | ZIP, XLSX, Neo4j, JSON-LD, bundle |
| Daily API calls | 10 | 1,000 |
| Daily AI design calls (`gd_design_schema`, `gd_design_project`) | 3 | No daily quota |

Limits are enforced by the Django API, not this MCP server. Daily limits reset at 00:00 UTC. AI design calls have their own quota: they count toward your daily calls but aren't blocked by that limit. Rows added by `duplication_config` stay within the row limit, and many-to-many junction rows have their own budget equal to the row limit, so a project can deliver up to twice it. `data.warnings` says when rows were trimmed.

## Configuration

| Variable | Required | Default |
|----------|----------|---------|
| `GENERATE_DATA_API_KEY` | Yes | — |
| `GENERATE_DATA_API_BASE_URL` | No | `https://generate-data.com` |
| `GENERATE_DATA_OUTPUT_DIR` | No | `~/generate-data-output` |

## Error codes

| `error.code` | Meaning | `retriable` |
|--------------|---------|-------------|
| `invalid_input` | Bad arguments, or the API rejected the request; `message` says why | No |
| `auth_failed` | Invalid or deactivated API key | No |
| `tier_forbidden` | The plan doesn't allow this format or size | No |
| `rate_limited` | Per-minute limit (`details.retry_after`) or daily limit (`details.resets_at`) | Per-minute yes, daily no |
| `not_found` | Unknown project id or field type | No |
| `unsupported_format` | Format isn't offered by this tool | No |
| `configuration_error` | Missing key, unusable base URL, or unwritable output directory | No |
| `upstream_unavailable` | Connection failure, timeout, or 502/503/504 | Yes |
| `upstream_error` | Unexpected API response | No |

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `configuration_error` naming `GENERATE_DATA_API_KEY` | Set the variable in your MCP client config and restart the server |
| `configuration_error` naming `GENERATE_DATA_API_BASE_URL` | Use the site origin, such as `https://generate-data.com`, without `/api/v2` |
| `configuration_error` naming `GENERATE_DATA_OUTPUT_DIR` | Point it at a writable directory |
| `auth_failed` | Invalid or deactivated key |
| `rate_limited` | Wait for `details.retry_after` seconds, or until `details.resets_at` for the daily limit, or upgrade |
| `tier_forbidden` | Reduce the request to fit your plan, or upgrade |
| `unsupported_format` | Use a format listed for that tool: datasets and Projects support different formats |
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
