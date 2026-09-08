#!/usr/bin/env node
/**
 * check-intair-seam.mjs — assert the shipped skailr <-> Intair seam artifacts satisfy AC-1..AC-10.
 * Usage:
 *   node scripts/skailr/check-intair-seam.mjs [--json]
 *   npm run check:intair
 *
 * Run from the repo root; paths resolve against process.cwd(). Node stdlib only.
 *
 * Content scans cover docs/intair-seam.md, .claude/skills/call-intair/SKILL.md, README.md,
 * CHANGELOG.md, manifest.json, package.json. This script excludes itself from every content
 * scan, because its own assertion needles are the very strings it forbids elsewhere. For the
 * same reason the forbidden reference-paste needle is assembled at runtime rather than written
 * as a literal, so grepping the repo for it does not hit this file.
 */
import { readFileSync, existsSync, readdirSync, statSync } from "node:fs";
import { resolve, join } from "node:path";

const GUIDE = "docs/intair-seam.md";
const SKILL = ".claude/skills/call-intair/SKILL.md";
const README = "README.md";
const CHANGELOG = "CHANGELOG.md";
const MANIFEST = "manifest.json";
const PKG = "package.json";
const SELF = "scripts/skailr/check-intair-seam.mjs";
const SHIPPED = [GUIDE, SKILL, README, CHANGELOG, MANIFEST, PKG];

const PASTE_NEEDLE = "src/" + "intair";

const MCP_TOOLS = [
  "intair_write_node",
  "intair_write_edge",
  "intair_get_node",
  "intair_get_subgraph",
  "intair_query",
  "intair_get_schema",
  "intair_propose_schema_change",
  "intair_open_scope",
  "intair_discard_scope",
  "intair_promote_scope",
  "intair_analyze",
  "intair_ask",
  "intair_submit_global_job",
  "intair_get_job",
  "intair_list_algorithms",
];

const GROUP_LABELS = [
  "Write knowledge / nodes / edges",
  "Query / subgraph / schema",
  "Schema proposal (propose only)",
  "Scope lifecycle",
  "Reasoning / analysis",
  "Job submission",
];

const REST_ROUTES = [
  "POST /nodes",
  "POST /edges",
  "GET /nodes/{id}",
  "POST /subgraph",
  "POST /query",
  "GET /schema",
  "POST /schema/proposals",
  "POST /scopes",
  "DELETE /scopes/{id}",
  "POST /scopes/{id}/promote",
  "POST /analyze",
  "POST /ask",
  "POST /jobs",
  "GET /jobs/{id}",
  "GET /algorithms",
];

const REST_ONLY_ROUTES = [
  "GET /health",
  "GET /schema/proposals",
  "POST /schema/proposals/{id}/approve",
  "POST /schema/proposals/{id}/reject",
];

const CONCEPT_ROWS = [
  "Agent invocation",
  "Team",
  "Task",
  "Decision",
  "Outcome",
  "Contract",
  "Channel",
  "Channel message",
  "Discovered knowledge",
  "Relationship",
];

const ERROR_CODES = [
  "UNAUTHORIZED",
  "NOT_FOUND",
  "VALIDATION",
  "SCHEMA_VIOLATION",
  "SUBGRAPH_TOO_LARGE",
  "PROPOSAL_STATE",
  "QUERY",
  "CONFLICT",
  "INTERNAL",
];

const DELIBERATE = "Every Intair call is a deliberate step taken by an agent or operator";

const AUTO_TRIGGER = ["automatically call", "on every commit", "on every message", "as a side effect", "subscribe to"];

const NEGATION = /\b(no|not|never|without|nothing|none|neither|nor)\b/i;

const failures = [];
function fail(ac, message) {
  failures.push(`${ac}: ${message}`);
}

function read(path) {
  const abs = resolve(path);
  return existsSync(abs) ? readFileSync(abs, "utf8") : null;
}

function walk(dir, skip = []) {
  const abs = resolve(dir);
  if (!existsSync(abs)) return [];
  const out = [];
  for (const entry of readdirSync(abs)) {
    const rel = join(dir, entry);
    if (skip.some((s) => rel === s || rel.startsWith(s.replace(/\/$/, "") + "/"))) continue;
    if (statSync(resolve(rel)).isDirectory()) out.push(...walk(rel, skip));
    else out.push(rel);
  }
  return out;
}

/** Lines of a markdown section: from the matching heading to the next heading of the same or higher level. */
function section(lines, headingRe) {
  const start = lines.findIndex((l) => headingRe.test(l));
  if (start === -1) return null;
  const level = (lines[start].match(/^#+/) || ["#"])[0].length;
  let end = lines.length;
  for (let i = start + 1; i < lines.length; i++) {
    const m = lines[i].match(/^(#+)\s/);
    if (m && m[1].length <= level) {
      end = i;
      break;
    }
  }
  return { start, end, lines: lines.slice(start, end), text: lines.slice(start, end).join("\n") };
}

function checkAc1(guide) {
  const lines = guide.split("\n");
  const h1 = lines.map((l, i) => [l, i]).filter(([l]) => /^# /.test(l));
  if (h1.length !== 1) {
    fail("AC-1", `expected exactly one "# " heading in ${GUIDE}, found ${h1.length}`);
    return;
  }
  const firstNonEmpty = lines.findIndex((l) => l.trim() !== "");
  if (firstNonEmpty !== h1[0][1]) fail("AC-1", `the H1 in ${GUIDE} is not the first non-empty line`);

  const after = lines.slice(h1[0][1] + 1);
  const nextHeading = after.findIndex((l) => /^#{1,6}\s/.test(l));
  const prose = (nextHeading === -1 ? after : after.slice(0, nextHeading)).join(" ").trim();
  const sentences = prose.split(/\.(?:\s|$)/).filter((s) => s.trim() !== "").length;
  if (sentences < 1 || sentences > 3) fail("AC-1", `capability statement must be 1 to 3 sentences, found ${sentences}`);
  if (!/client/i.test(prose)) fail("AC-1", "capability statement does not describe skailr agents as clients");
  if (!/\bREST\b/i.test(prose) || !/\bMCP\b/i.test(prose)) fail("AC-1", "capability statement does not name both the REST and MCP faces");
  if (!/no live runtime coupling/i.test(prose)) fail("AC-1", 'capability statement is missing the "no live runtime coupling" claim');
}

function checkAc2(guide) {
  for (const tool of MCP_TOOLS) {
    if (!guide.includes(tool)) fail("AC-2", `MCP tool ${tool} is not documented in ${GUIDE}`);
  }
  for (const label of GROUP_LABELS) {
    if (!guide.includes(label)) fail("AC-2", `operation group label "${label}" missing from ${GUIDE}`);
  }
  for (const key of ['"error"', '"code"', '"message"', '"detail"']) {
    if (!guide.includes(key)) fail("AC-2", `error envelope key ${key} missing from ${GUIDE}`);
  }
  if (!/ever rais|never rais/i.test(guide) || !/error envelope/i.test(guide)) {
    fail("AC-2", `${GUIDE} does not state that tools return the error envelope rather than raising`);
  }
}

function restOps(text) {
  const found = new Set();
  for (const m of text.matchAll(/\b(GET|POST|PUT|PATCH|DELETE) (\/[A-Za-z0-9/{}_.=?-]*)/g)) {
    found.add(`${m[1]} ${m[2].split("?")[0].replace(/\/$/, "")}`);
  }
  return found;
}

function checkAc3(guide) {
  for (const route of [...REST_ROUTES, ...REST_ONLY_ROUTES]) {
    if (!guide.includes(route)) fail("AC-3", `REST route "${route}" is not documented in ${GUIDE}`);
  }
  if (!/Bearer/.test(guide)) fail("AC-3", `${GUIDE} does not state the bearer-token auth mechanism`);
  if (!/open\s*\/\s*dev mode/i.test(guide)) fail("AC-3", `${GUIDE} does not state that an unset token means open / dev mode`);
  if (!/approval is reachable over REST only/i.test(guide)) {
    fail("AC-3", `${GUIDE} does not state that proposal approval is reachable over REST only`);
  }
  const ops = restOps(guide).size;
  const tools = new Set([...guide.matchAll(/intair_[a-z_]+/g)].map((m) => m[0])).size;
  if (ops < tools) fail("AC-3", `documented REST operations (${ops}) fewer than documented MCP tools (${tools}); not 1:1`);
}

function checkAc4(guide) {
  for (const key of ['"actor"', '"actor_kind"', '"at"', '"basis"']) {
    if (!guide.includes(key)) fail("AC-4", `attribution field ${key} missing from ${GUIDE}`);
  }
  if (!/ISO-8601/.test(guide) || !/UTC/.test(guide)) fail("AC-4", `${GUIDE} does not state that "at" is ISO-8601 UTC`);
  if (!/agent attribution/i.test(guide)) fail("AC-4", `${GUIDE} does not name the agent attribution convenience form`);
  if (!/system attribution/i.test(guide)) fail("AC-4", `${GUIDE} does not name the system attribution convenience form`);
  if (!/basis[^\n]*task:/i.test(guide)) fail("AC-4", `${GUIDE} does not document the "task:<id>" basis default`);
}

function checkAc5(guide) {
  if (!/may propose additive schema changes/i.test(guide)) fail("AC-5", `${GUIDE} does not state that agents may propose additive schema changes`);
  if (!/approval is performed by a human operator/i.test(guide)) {
    fail("AC-5", `${GUIDE} does not state that approval is performed by a human operator`);
  }
  if (!guide.includes("intair_propose_schema_change")) fail("AC-5", `${GUIDE} does not name the propose-side operation`);
  for (const path of SHIPPED) {
    const text = read(path);
    if (text && /intair_(approve|reject)/i.test(text)) {
      fail("AC-5", `${path} names an agent-reachable approve or reject tool`);
    }
  }
}

function checkAc6(guide) {
  const lines = guide.split("\n");
  const header = lines.findIndex((l) => /^\|\s*skailr\s*\|\s*Intair\s*\|\s*Layer\s*\|/i.test(l));
  if (header === -1) {
    fail("AC-6", `${GUIDE} has no concept-map table with a "skailr | Intair | Layer" header`);
    return;
  }
  let last = header + 1;
  const body = [];
  for (let i = header + 2; i < lines.length && /^\|/.test(lines[i]); i++) {
    body.push(lines[i]);
    last = i;
  }
  if (body.length !== 10) fail("AC-6", `concept-map table must have exactly 10 body rows, found ${body.length}`);

  const cells = body.map((r) => r.split("|").slice(1, -1).map((c) => c.trim()));
  const labels = cells.map((c) => c[0]);
  for (const want of CONCEPT_ROWS) {
    if (!labels.includes(want)) fail("AC-6", `concept-map table is missing a row for "${want}"`);
  }
  const layers = cells.map((c) => c[2] || "").join(" ");
  for (const layer of ["operational", "context", "semantic"]) {
    if (!layers.includes(layer)) fail("AC-6", `concept-map table never names the "${layer}" layer`);
  }
  const trailer = lines.slice(last + 1, last + 6).join(" ");
  if (!/live schema/i.test(trailer) || !/intair_get_schema/.test(trailer)) {
    fail("AC-6", "the live-schema note must follow the concept-map table within 5 lines");
  }
}

function outOfScopeSection(guide) {
  return section(guide.split("\n"), /^#{1,6}\s+Out of scope for v1\s*$/i);
}

function checkAc7(guide) {
  const sec = outOfScopeSection(guide);
  if (!sec) {
    fail("AC-7", `${GUIDE} has no "Out of scope for v1" heading`);
    return;
  }
  const items = sec.lines.filter((l) => /^\s*(\d+\.|[-*])\s+\S/.test(l));
  if (items.length < 5) fail("AC-7", `out-of-scope list must have at least 5 items, found ${items.length}`);
  const checks = [
    [/live runtime coupling/i, "live runtime coupling"],
    [/ingest of channel messages/i, "automatic ingest of channel messages"],
    [/webhook/i, "webhooks"],
    [/subscription|subscribe/i, "event subscriptions"],
    [/\bpush\b/i, "push sync"],
    [/(document|conversation)-to-graph/i, "document or conversation to graph extraction"],
    [/skailr-side approval/i, "skailr-side approval of schema proposals"],
  ];
  for (const [re, label] of checks) {
    if (!re.test(sec.text)) fail("AC-7", `out-of-scope list does not name ${label}`);
  }
}

function checkAc8(guide, skill) {
  const skillDir = ".claude/skills/call-intair";
  const scanned = [...walk(".claude/commands"), ...walk(".claude/skills", [skillDir])];
  for (const path of scanned) {
    const text = read(path);
    if (!text || !/intair/i.test(text)) continue;
    // Deliberate wiring that routes through skill `call-intair` is the one allowed
    // coupling (e.g. /map-repo Phase 5, /mint-expert optional Outcome). Anything else
    // still fails, and even allowed files must stay free of auto-trigger phrasing.
    if (!text.includes("call-intair")) {
      fail("AC-8", `${path} mentions Intair without routing through skill call-intair; no other coupling is allowed`);
      continue;
    }
    for (const [i, line] of text.split("\n").entries()) {
      const hit = AUTO_TRIGGER.find((p) => line.toLowerCase().includes(p));
      if (!hit) continue;
      if (NEGATION.test(line)) continue;
      fail("AC-8", `${path}:${i + 1} reads as an auto-trigger instruction ("${hit}")`);
    }
  }

  if (!guide.includes(DELIBERATE)) fail("AC-8", `${GUIDE} is missing the deliberate-invocation statement`);
  if (skill && !skill.includes(DELIBERATE)) fail("AC-8", `${SKILL} is missing the deliberate-invocation statement`);

  const scope = outOfScopeSection(guide);
  for (const path of SHIPPED) {
    const text = read(path);
    if (!text) continue;
    const exempt = path === GUIDE && scope ? scope.text : "";
    for (const [i, line] of text.split("\n").entries()) {
      const hit = AUTO_TRIGGER.find((p) => line.toLowerCase().includes(p));
      if (!hit) continue;
      if (exempt.includes(line)) continue;
      if (NEGATION.test(line)) continue;
      fail("AC-8", `${path}:${i + 1} reads as an auto-trigger instruction ("${hit}")`);
    }
  }
}

function checkAc9() {
  const readme = read(README);
  if (!readme) {
    fail("AC-9", `${README} not found`);
    return;
  }
  const refs = readme.split(GUIDE).length - 1;
  if (refs < 2) fail("AC-9", `${README} references ${GUIDE} ${refs} time(s); expected at least 2 (existing link plus a router or section row)`);
  if (!existsSync(resolve(GUIDE))) fail("AC-9", `${README} links ${GUIDE} but the file does not exist`);
  for (const m of new Set([...readme.matchAll(/docs\/[A-Za-z0-9_.-]+\.md/g)].map((x) => x[0]))) {
    if (!existsSync(resolve(m))) fail("AC-9", `${README} links ${m}, which does not exist`);
  }
}

function checkAc10() {
  for (const path of SHIPPED) {
    if (path === SELF) continue;
    const text = read(path);
    if (text && text.includes(PASTE_NEEDLE)) {
      fail("AC-10", `${path} references the local Intair reference paste; shipped files must be self-contained`);
    }
  }
}

function checkStructural(skill) {
  if (!skill) fail("Structural", `${SKILL} not found`);
  else if (!/^---\nname: call-intair\n/m.test(skill)) fail("Structural", `${SKILL} frontmatter must declare name: call-intair`);

  const manifestRaw = read(MANIFEST);
  if (!manifestRaw) fail("Structural", `${MANIFEST} not found`);
  else {
    let manifest;
    try {
      manifest = JSON.parse(manifestRaw);
    } catch (err) {
      fail("Structural", `${MANIFEST} is not valid JSON: ${err.message}`);
    }
    if (manifest) {
      const rows = (manifest.artifacts || []).filter((a) => a.claude_path === SKILL);
      if (rows.length !== 1) fail("Structural", `${MANIFEST} must contain exactly one artifact for ${SKILL}, found ${rows.length}`);
      else {
        const row = rows[0];
        if (row.type !== "skill") fail("Structural", `${MANIFEST} row for ${SKILL} must have type "skill"`);
        if (row.cursor_path !== null) fail("Structural", `${MANIFEST} row for ${SKILL} must have cursor_path null`);
      }
    }
  }

  const pkgRaw = read(PKG);
  if (!pkgRaw) fail("Structural", `${PKG} not found`);
  else {
    let pkg;
    try {
      pkg = JSON.parse(pkgRaw);
    } catch (err) {
      fail("Structural", `${PKG} is not valid JSON: ${err.message}`);
    }
    const want = `node ${SELF}`;
    if (pkg && pkg.scripts?.["check:intair"] !== want) {
      fail("Structural", `${PKG} scripts["check:intair"] must be "${want}", found "${pkg?.scripts?.["check:intair"] ?? "(absent)"}"`);
    }
  }

  const changelog = read(CHANGELOG);
  if (!changelog) fail("Structural", `${CHANGELOG} not found`);
  else {
    const unreleased = section(changelog.split("\n"), /^##\s*\[Unreleased\]/i);
    if (!unreleased) fail("Structural", `${CHANGELOG} has no [Unreleased] section`);
    // Prefer [Unreleased] > Added while the seam is mid-flight; once released, any ### Added
    // section that mentions intair-seam (e.g. [1.6.0]) satisfies the inventory gate.
    const unreleasedAdded = unreleased
      ? section(unreleased.lines, /^###\s*Added/i)
      : null;
    const inUnreleased =
      unreleasedAdded && unreleasedAdded.text.includes("intair-seam");
    const inAnyAdded = /^###\s*Added\b[\s\S]*?intair-seam/m.test(changelog);
    if (!inUnreleased && !inAnyAdded) {
      fail(
        "Structural",
        `${CHANGELOG} must mention intair-seam under ### Added ([Unreleased] or a released version)`,
      );
    }
  }
}

function checkErrorCodes(guide) {
  for (const code of ERROR_CODES) {
    if (!guide.includes(code)) fail("AC-2", `error code ${code} missing from ${GUIDE}`);
  }
}

function main() {
  const args = process.argv.slice(2);
  if (args.includes("--help")) {
    console.log("Usage: check-intair-seam.mjs [--json] [--help]");
    console.log("Asserts the skailr <-> Intair seam guide, skill, and inventory rows satisfy AC-1..AC-10.");
    process.exit(0);
  }
  const asJson = args.includes("--json");

  const guide = read(GUIDE);
  const skill = read(SKILL);

  if (!guide) {
    fail("AC-1", `${GUIDE} not found`);
  } else {
    checkAc1(guide);
    checkAc2(guide);
    checkErrorCodes(guide);
    checkAc3(guide);
    checkAc4(guide);
    checkAc5(guide);
    checkAc6(guide);
    checkAc7(guide);
    checkAc8(guide, skill);
  }
  checkAc9();
  checkAc10();
  checkStructural(skill);

  if (asJson) {
    console.log(JSON.stringify({ ok: failures.length === 0, failures }, null, 2));
    process.exit(failures.length ? 1 : 0);
  }

  if (failures.length) {
    console.error("Intair seam violations:\n" + failures.join("\n"));
    process.exit(1);
  }
  console.log(`OK: Intair seam intact; ${MCP_TOOLS.length} MCP tools, ${REST_ROUTES.length + REST_ONLY_ROUTES.length} REST routes, 10 concept rows verified`);
}

main();
