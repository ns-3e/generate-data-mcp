# Changelog

All notable changes to `generate-data-mcp` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.2] - 2026-11-04

### Fixed

- **The default API host now works.** The default base URL was
  `https://api.generate-data.com`, which did not resolve, so every tool failed
  unless `GENERATE_DATA_API_BASE_URL` was set. It is now `https://generate-data.com`.
- **`gd_generate_project` failed on every format except `json`,** with a misleading
  `not_found`: it sent the format as `?format=`, which the API reserves. With `json`
  it returned a ZIP decoded as text and reported success. The format now goes in
  the request body.
- **`gd_design_schema` refine always failed** with a 422. It now sends `schema`, and
  `current_schema` is the `proposed_schema` array the previous call returned.
- **Bad upstream responses are no longer reported as generated data.** A redirect,
  an empty body, an HTML page or a corrupt binary now returns `upstream_error`.
- Tool `format` lists now match what the API accepts, and `format` is
  case-insensitive. `gd_generate_dataset` gains `xlsx`, `yaml`, `sql`, `jsonl` and
  `hf-datasets` and drops `zip`. `gd_generate_project` gains `xlsx`, `neo4j`,
  `jsonld` and `bundle` and drops `csv`, `json`, `xml` and `parquet`. The dropped
  values never returned usable output.
- `gd_design_schema` now returns `invalid_input` when given only one of
  `messages`/`current_schema` instead of silently proposing. `prompt` is optional
  when refining.
- Docs: Pro is 10,000 rows per request, not 100,000, and Free keys can use Projects.

### Changed

- Minimum `mcp` is now 1.30.0. Declared minimums below it did not import.

### Added

- End-to-end tests that run a real MCP client against the server over stdio, a CI
  job on the lowest supported dependencies, and an optional nightly smoke test.

## [2.0.1] - 2026-08-03

Non-breaking. Adds an `mcp-name: io.github.ns-3e/generate-data-mcp` ownership-verification
marker to the README (required by the official MCP Registry's PyPI-package verification —
it checks for this string in the published package description). Restructures the README
around a 30-second install flow. No code changes.

## [2.0.0] - 2026-08-03

Full rebuild of the MCP server against Generate-Data.com `/api/v2/`. **Every tool
was renamed and every response shape changed** — this is a breaking release for any
existing MCP client config or agent prompt referencing the v1 tool names or raw
responses. See [Migrating from v1](README.md#migrating-from-v1).

### Changed — BREAKING: tool renames

Every tool is renamed to the `gd_` prefix. Old → new:

| Old (v1) | New (2.0.0) | Notes |
|----------|-------------|-------|
| `generate_data` | `gd_generate_dataset` | |
| `list_field_types` | `gd_list_field_types` | |
| `get_field_options` | `gd_get_field_type_options` | |
| `propose_schema` | `gd_design_schema` | merged (see below) |
| `refine_schema` | `gd_design_schema` | merged (see below) |
| `get_api_usage` | `gd_get_usage` | |
| `list_projects` | `gd_list_projects` | now paginated (`limit`/`offset`) |
| `generate_project` | `gd_generate_project` | binary formats fixed (see below) |

Update your Claude Desktop / Cursor config and any agent prompts referencing the
old names — v1 names no longer exist and will fail as unknown tools.

- **`propose_schema` + `refine_schema` merged into `gd_design_schema`.** One tool
  now handles both. First call proposes: pass only `prompt` (and optional
  `locale`). Refine: pass **both** `messages` and `current_schema` together (the
  conversation and the prior schema). Passing only one of the pair falls back to
  a propose using `prompt`.

### Changed — BREAKING: response envelope

Every tool now returns a single consistent JSON envelope instead of raw or mixed
upstream JSON:

- Success: `{"ok": true, "summary": "<natural-language>", "data": {…}}`
- Failure: `{"ok": false, "error": {"code", "message", "retriable", "details"}}`

Tools no longer raise on upstream/HTTP failure — errors are returned as the error
envelope. `error.code` is a closed enum: `invalid_input`, `auth_failed`,
`tier_forbidden`, `rate_limited`, `not_found`, `unsupported_format`,
`upstream_error`, `upstream_unavailable`. Consumers that previously parsed raw
upstream payloads must read `data` (success) or `error` (failure).

### Changed — BREAKING: `gd_list_projects` pagination

`gd_list_projects` now takes `limit` (default 20, max 100) and `offset` (default
0) and returns a paged `data` shape: `items`, `limit`, `offset`, `total_count`,
`has_more`, `next_offset`. Pagination is applied client-side over the full
upstream list.

### Fixed

- **Binary format corruption (ZIP / Parquet).** v1 force-decoded binary response
  bodies as UTF-8, corrupting `zip` and `parquet` downloads. `gd_generate_dataset`
  and `gd_generate_project` now return binary formats base64-encoded in
  `data.content_base64` (with `encoding: "base64"`); text formats (csv/json/xml)
  stay in `data.content`. Never decode `content_base64` as UTF-8. Binary generate
  calls also send `Accept: */*` upstream so a content-negotiating backend does not
  coerce the body to JSON.

### Added

- Distribution/registry artifacts: `server.json`, `server.yaml`, and a `Dockerfile`
  for container-based invocation.

### Security

- Error messages and `details` never carry the API key or request headers; any
  embedded upstream text is JSON-delimited, not interpolated (response-injection
  guard). Input is validated before any upstream call: `field_type` must match
  `^[a-z0-9_]+$`, `project_id` must be a positive integer, `limit`/`offset` are
  range-checked.

[2.0.2]: https://github.com/ns-3e/generate-data-mcp/releases/tag/v2.0.2
[2.0.1]: https://github.com/ns-3e/generate-data-mcp/releases/tag/v2.0.1
[2.0.0]: https://github.com/ns-3e/generate-data-mcp/releases/tag/v2.0.0
