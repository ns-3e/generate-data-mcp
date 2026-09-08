#!/usr/bin/env node
/**
 * ticket-status.mjs — frontier query, claim, DAG + disjointness checks for the feature ticket board.
 *
 * Usage:
 *   node scripts/skailr/ticket-status.mjs [--root .claude/tmp] [--json]
 *   node scripts/skailr/ticket-status.mjs frontier [--root .claude/tmp] [--json]
 *   node scripts/skailr/ticket-status.mjs claim --id T-001 [--root .claude/tmp] [--json]
 *   node scripts/skailr/ticket-status.mjs validate [--root .claude/tmp] [--json]
 *   node scripts/skailr/ticket-status.mjs parallel [--ids T-001,T-002] [--root .claude/tmp] [--json]
 *   node scripts/skailr/ticket-status.mjs resolve --id T-001 --gist "..." [--root .claude/tmp] [--json]
 */
import {
  readFileSync,
  writeFileSync,
  existsSync,
  readdirSync,
  mkdirSync,
} from "node:fs";
import { join, resolve, basename } from "node:path";

const BUILD_ROLES = new Set(["backend", "frontend", "data"]);
const ALL_ROLES = new Set(["backend", "frontend", "data", "research", "decide"]);
const STATUSES = new Set(["open", "claimed", "done", "out-of-scope"]);

function minimatch(path, pattern) {
  const p = path.replace(/\\/g, "/");
  const g = pattern.replace(/\\/g, "/");
  if (g.endsWith("/**")) {
    const prefix = g.slice(0, -3);
    return (
      p === prefix.replace(/\/$/, "") ||
      p.startsWith(prefix.endsWith("/") ? prefix : prefix + "/")
    );
  }
  if (g.includes("*")) {
    const re = new RegExp(
      "^" +
        g
          .replace(/[.+^${}()|[\]\\]/g, "\\$&")
          .replace(/\*\*/g, ".*")
          .replace(/\*/g, "[^/]*") +
        "$",
    );
    return re.test(p);
  }
  return p === g || p.startsWith(g.replace(/\/$/, "") + "/");
}

const UNSUPPORTED_GLOB = /[{}?[\]]/;

function globsOverlap(a, b) {
  return a === b || minimatch(a, b) || minimatch(b, a);
}

function parseScalar(raw) {
  const v = raw.trim();
  if (
    (v.startsWith('"') && v.endsWith('"')) ||
    (v.startsWith("'") && v.endsWith("'"))
  ) {
    return v.slice(1, -1);
  }
  if (v.startsWith("[") && v.endsWith("]")) {
    const inner = v.slice(1, -1).trim();
    if (!inner) return [];
    return inner.split(",").map((s) => s.trim().replace(/^["']|["']$/g, ""));
  }
  return v;
}

/** Minimal YAML frontmatter for ticket files (flat keys + ownership list). */
function parseTicketFrontmatter(text) {
  if (!text.startsWith("---")) return null;
  const end = text.indexOf("\n---", 3);
  if (end === -1) return null;
  const block = text.slice(3, end).trim();
  const data = { ownership: [], acs: [], blocked_by: [] };
  const lines = block.split("\n");
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const listMatch = line.match(/^(\w+):\s*$/);
    if (listMatch) {
      const key = listMatch[1];
      const items = [];
      i++;
      while (i < lines.length && /^\s+-\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s+-\s+/, "").trim());
        i++;
      }
      data[key] = items;
      continue;
    }
    const kv = line.match(/^(\w+):\s*(.*)$/);
    if (kv) {
      const key = kv[1];
      const val = parseScalar(kv[2]);
      data[key] = val;
    }
    i++;
  }
  if (!Array.isArray(data.acs)) data.acs = data.acs ? [data.acs] : [];
  if (!Array.isArray(data.blocked_by))
    data.blocked_by = data.blocked_by ? [data.blocked_by] : [];
  if (!Array.isArray(data.ownership))
    data.ownership = data.ownership ? [data.ownership] : [];
  return { data, bodyStart: end + 4, raw: text };
}

function setFrontmatterField(text, key, value) {
  if (!text.startsWith("---")) return text;
  const end = text.indexOf("\n---", 3);
  if (end === -1) return text;
  const head = text.slice(0, end);
  const rest = text.slice(end);
  const re = new RegExp(`^(${key}:\\s*).*$`, "m");
  if (re.test(head)) {
    return head.replace(re, `$1${value}`) + rest;
  }
  // Insert before closing ---
  return head.replace(/\n$/, `\n${key}: ${value}\n`) + rest;
}

function ticketsDir(root) {
  return join(root, "tickets");
}

function boardPath(root) {
  return join(root, "board.md");
}

function loadTickets(root) {
  const dir = ticketsDir(root);
  if (!existsSync(resolve(dir))) return [];
  const files = readdirSync(resolve(dir)).filter(
    (f) => f.endsWith(".md") && !f.endsWith("-report.md"),
  );
  const tickets = [];
  for (const f of files) {
    const path = join(dir, f);
    const text = readFileSync(resolve(path), "utf8");
    const parsed = parseTicketFrontmatter(text);
    if (!parsed || !parsed.data.id) continue;
    tickets.push({
      ...parsed.data,
      path: path.replace(/\\/g, "/"),
      filename: f,
      text,
    });
  }
  return tickets;
}

function byId(tickets) {
  const m = new Map();
  for (const t of tickets) m.set(t.id, t);
  return m;
}

function isTerminal(status) {
  return status === "done" || status === "out-of-scope";
}

function isUnblocked(ticket, map) {
  for (const id of ticket.blocked_by || []) {
    const dep = map.get(id);
    if (!dep || !isTerminal(dep.status)) return false;
  }
  return true;
}

function frontier(tickets) {
  const map = byId(tickets);
  return tickets.filter(
    (t) => t.status === "open" && isUnblocked(t, map),
  );
}

function validateDag(tickets) {
  const errors = [];
  const map = byId(tickets);
  for (const t of tickets) {
    if (!ALL_ROLES.has(t.role)) {
      errors.push(`${t.id}: invalid role "${t.role}"`);
    }
    if (!STATUSES.has(t.status)) {
      errors.push(`${t.id}: invalid status "${t.status}"`);
    }
    for (const dep of t.blocked_by || []) {
      if (!map.has(dep)) errors.push(`${t.id}: blocked_by unknown ${dep}`);
    }
    for (const g of t.ownership || []) {
      // The built-in matcher supports only literal prefixes, "*", and "**".
      // Braces/?/[] would be matched as literals — silently missing real overlaps —
      // so reject them outright rather than misread an ownership expression.
      if (UNSUPPORTED_GLOB.test(g)) {
        errors.push(`${t.id}: unsupported glob syntax "${g}" (supported: literal prefixes, "*", "**")`);
      }
    }
  }
  // Cycle detection
  const visiting = new Set();
  const visited = new Set();
  function visit(id, stack) {
    if (visited.has(id)) return;
    if (visiting.has(id)) {
      errors.push(`cycle: ${[...stack, id].join(" → ")}`);
      return;
    }
    visiting.add(id);
    const t = map.get(id);
    if (t) {
      for (const dep of t.blocked_by || []) visit(dep, [...stack, id]);
    }
    visiting.delete(id);
    visited.add(id);
  }
  for (const t of tickets) visit(t.id, []);
  return errors;
}

function parallelOk(tickets, ids) {
  const map = byId(tickets);
  const selected = ids.map((id) => map.get(id)).filter(Boolean);
  const errors = [];
  if (selected.length !== ids.length) {
    const missing = ids.filter((id) => !map.has(id));
    errors.push(`unknown ids: ${missing.join(", ")}`);
  }
  for (let i = 0; i < selected.length; i++) {
    for (let j = i + 1; j < selected.length; j++) {
      const a = selected[i];
      const b = selected[j];
      if (!BUILD_ROLES.has(a.role) || !BUILD_ROLES.has(b.role)) continue;
      if (a.role !== b.role) continue;
      for (const ga of a.ownership || []) {
        for (const gb of b.ownership || []) {
          if (globsOverlap(ga, gb)) {
            errors.push(
              `overlap: ${a.id} (${ga}) vs ${b.id} (${gb}) [role ${a.role}]`,
            );
          }
        }
      }
    }
  }
  return { ok: errors.length === 0, errors, selected };
}

/** Greedy: take frontier tickets in id order, skip same-role ownership collisions. */
function pickParallel(tickets) {
  const front = frontier(tickets).sort((a, b) => a.id.localeCompare(b.id));
  const picked = [];
  for (const t of front) {
    const trial = [...picked.map((p) => p.id), t.id];
    const check = parallelOk(tickets, trial);
    if (check.ok) picked.push(t);
  }
  return picked;
}

function claimTicket(root, id) {
  const tickets = loadTickets(root);
  const t = tickets.find((x) => x.id === id);
  if (!t) return { ok: false, error: `ticket_not_found: ${id}` };
  if (t.status !== "open") {
    return { ok: false, error: `not_open: ${id} is ${t.status}` };
  }
  const map = byId(tickets);
  if (!isUnblocked(t, map)) {
    return { ok: false, error: `blocked: ${id}` };
  }
  const next = setFrontmatterField(t.text, "status", "claimed");
  writeFileSync(resolve(t.path), next, "utf8");
  return { ok: true, id, path: t.path, title: t.title, status: "claimed" };
}

function appendDoneGist(root, title, pathRel, gist) {
  const bp = boardPath(root);
  if (!existsSync(resolve(bp))) return;
  let text = readFileSync(resolve(bp), "utf8");
  const line = `- [${title}](${pathRel}) — ${gist}`;
  const doneIdx = text.indexOf("## Done");
  if (doneIdx === -1) {
    text += `\n## Done\n\n${line}\n`;
    writeFileSync(resolve(bp), text, "utf8");
    return;
  }
  const afterDone = text.indexOf("\n## ", doneIdx + 1);
  const insertAt = afterDone === -1 ? text.length : afterDone;
  const section = text.slice(doneIdx, insertAt);
  if (section.includes(`](${pathRel})`)) return; // already listed
  const newSection =
    section.trimEnd() + (section.trimEnd().endsWith("Done") ? "\n\n" : "\n") + line + "\n";
  text = text.slice(0, doneIdx) + newSection + text.slice(insertAt);
  writeFileSync(resolve(bp), text, "utf8");
}

function updateBoardTicketStatus(root, id, status) {
  const bp = boardPath(root);
  if (!existsSync(resolve(bp))) return;
  let text = readFileSync(resolve(bp), "utf8");
  const re = new RegExp(
    `(\\|\\s*${id.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*\\|[^|]*\\|[^|]*\\|)\\s*[^|]*\\|`,
    "i",
  );
  if (re.test(text)) {
    text = text.replace(re, `$1 ${status} |`);
    writeFileSync(resolve(bp), text, "utf8");
  }
}

function resolveTicket(root, id, gist) {
  const tickets = loadTickets(root);
  const t = tickets.find((x) => x.id === id);
  if (!t) return { ok: false, error: `ticket_not_found: ${id}` };
  if (t.status !== "claimed" && t.status !== "open") {
    return { ok: false, error: `cannot_resolve: ${id} is ${t.status}` };
  }
  let next = setFrontmatterField(t.text, "status", "done");
  // Ensure ## Resolution has the gist if empty-ish
  if (/## Resolution\s*\n+(?:<!--[\s\S]*?-->\s*)?$/m.test(next) ||
      /## Resolution\s*\n+(?:<!--[\s\S]*?-->\s*)?\n*$/m.test(next)) {
    next = next.replace(
      /## Resolution\s*\n+(?:<!--[\s\S]*?-->\s*)?/,
      `## Resolution\n\n${gist}\n\n`,
    );
  } else if (next.includes("## Resolution")) {
    next = next.replace(
      /(## Resolution\s*\n)/,
      `$1\n${gist}\n`,
    );
  } else {
    next = next.trimEnd() + `\n\n## Resolution\n\n${gist}\n`;
  }
  writeFileSync(resolve(t.path), next, "utf8");
  const pathRel = t.path.replace(/\\/g, "/");
  appendDoneGist(root, t.title || id, pathRel, gist);
  updateBoardTicketStatus(root, id, "done");
  return { ok: true, id, path: pathRel, title: t.title, status: "done", gist };
}

function summary(root) {
  const tickets = loadTickets(root);
  const errors = validateDag(tickets);
  const front = frontier(tickets);
  const parallel = pickParallel(tickets);
  const byStatus = {};
  for (const t of tickets) {
    byStatus[t.status] = (byStatus[t.status] || 0) + 1;
  }
  const complete =
    tickets.length > 0 &&
    tickets.every((t) => isTerminal(t.status)) &&
    errors.length === 0;
  const board = boardPath(root).replace(/\\/g, "/");
  const hasBoard = existsSync(resolve(boardPath(root)));
  return {
    ok: errors.length === 0,
    hasBoard,
    board: hasBoard ? board : null,
    ticketsRoot: ticketsDir(root).replace(/\\/g, "/"),
    count: tickets.length,
    byStatus,
    frontier: front.map(slim),
    parallel: parallel.map(slim),
    partialRoles: incompleteRoles(tickets),
    complete,
    errors,
    tickets: tickets.map(slim),
  };
}

function slim(t) {
  return {
    id: t.id,
    title: t.title,
    role: t.role,
    status: t.status,
    path: t.path,
    acs: t.acs,
    blocked_by: t.blocked_by,
    ownership: t.ownership,
  };
}

function incompleteRoles(tickets) {
  const roles = new Set();
  for (const t of tickets) {
    if (!isTerminal(t.status) && BUILD_ROLES.has(t.role)) roles.add(t.role);
  }
  return [...roles];
}

function parseArgs(argv) {
  const out = {
    cmd: "status",
    root: ".claude/tmp",
    json: false,
    id: null,
    ids: null,
    gist: null,
  };
  const args = argv.slice(2);
  if (
    args[0] &&
    !args[0].startsWith("-") &&
    ["status", "frontier", "claim", "validate", "parallel", "resolve"].includes(
      args[0],
    )
  ) {
    out.cmd = args.shift();
  }
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    if (a === "--root") out.root = args[++i];
    else if (a === "--json") out.json = true;
    else if (a === "--id") out.id = args[++i];
    else if (a === "--ids") out.ids = args[++i].split(",").map((s) => s.trim());
    else if (a === "--gist") out.gist = args[++i];
    else if (a === "--help") {
      console.log(`Usage: ticket-status.mjs [status|frontier|claim|validate|parallel|resolve]
  --root .claude/tmp
  --id T-001
  --ids T-001,T-002
  --gist "one-line resolution"
  --json`);
      process.exit(0);
    }
  }
  return out;
}

function main() {
  const args = parseArgs(process.argv);
  const root = args.root;

  if (args.cmd === "claim") {
    if (!args.id) {
      const payload = { ok: false, error: "missing --id" };
      console.log(args.json ? JSON.stringify(payload) : payload.error);
      process.exit(1);
    }
    const result = claimTicket(root, args.id);
    if (args.json) console.log(JSON.stringify(result, null, 2));
    else
      console.log(
        result.ok
          ? `Claimed ${result.id} (${result.title})`
          : `FAIL: ${result.error}`,
      );
    process.exit(result.ok ? 0 : 1);
  }

  if (args.cmd === "resolve") {
    if (!args.id || !args.gist) {
      const payload = { ok: false, error: "need --id and --gist" };
      console.log(args.json ? JSON.stringify(payload) : payload.error);
      process.exit(1);
    }
    const result = resolveTicket(root, args.id, args.gist);
    if (args.json) console.log(JSON.stringify(result, null, 2));
    else
      console.log(
        result.ok
          ? `Resolved ${result.id}: ${result.gist}`
          : `FAIL: ${result.error}`,
      );
    process.exit(result.ok ? 0 : 1);
  }

  if (args.cmd === "validate") {
    const tickets = loadTickets(root);
    const errors = validateDag(tickets);
    const payload = { ok: errors.length === 0, errors, count: tickets.length };
    if (args.json) console.log(JSON.stringify(payload, null, 2));
    else if (errors.length) {
      console.log("FAIL:");
      for (const e of errors) console.log(`  - ${e}`);
    } else console.log(`OK: ${tickets.length} tickets, DAG valid`);
    process.exit(errors.length ? 1 : 0);
  }

  if (args.cmd === "parallel") {
    const tickets = loadTickets(root);
    const ids = args.ids || pickParallel(tickets).map((t) => t.id);
    const check = parallelOk(tickets, ids);
    const payload = {
      ok: check.ok,
      ids,
      errors: check.errors,
      tickets: check.selected.map(slim),
    };
    if (args.json) console.log(JSON.stringify(payload, null, 2));
    else if (!check.ok) {
      console.log("FAIL parallel:");
      for (const e of check.errors) console.log(`  - ${e}`);
    } else console.log(`OK parallel: ${ids.join(", ") || "(none)"}`);
    process.exit(check.ok ? 0 : 1);
  }

  if (args.cmd === "frontier") {
    const tickets = loadTickets(root);
    const front = frontier(tickets);
    const payload = { ok: true, frontier: front.map(slim) };
    if (args.json) console.log(JSON.stringify(payload, null, 2));
    else {
      if (!front.length) console.log("(no frontier tickets)");
      for (const t of front) {
        console.log(`${t.id}\t${t.role}\t${t.title}\t${t.path}`);
      }
    }
    process.exit(0);
  }

  // status (default)
  if (!existsSync(resolve(boardPath(root))) && !existsSync(resolve(ticketsDir(root)))) {
    const payload = { ok: false, hasBoard: false, error: "no_board" };
    if (args.json) console.log(JSON.stringify(payload, null, 2));
    else console.log("No board/tickets under " + root);
    process.exit(0);
  }
  // Ensure tickets dir exists for mint follow-ups
  if (!existsSync(resolve(ticketsDir(root)))) {
    mkdirSync(resolve(ticketsDir(root)), { recursive: true });
  }
  const payload = summary(root);
  if (args.json) console.log(JSON.stringify(payload, null, 2));
  else {
    console.log(`Board:   ${payload.board || "(missing)"}`);
    console.log(`Tickets: ${payload.count}`);
    console.log(`Complete:${payload.complete ? " yes" : " no"}`);
    console.log(
      `Frontier: ${payload.frontier.map((t) => t.id).join(", ") || "(none)"}`,
    );
    console.log(
      `Parallel: ${payload.parallel.map((t) => t.id).join(", ") || "(none)"}`,
    );
    if (payload.errors.length) {
      console.log("Errors:");
      for (const e of payload.errors) console.log(`  - ${e}`);
    }
  }
  process.exit(payload.ok ? 0 : 1);
}

main();
