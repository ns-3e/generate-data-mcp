#!/usr/bin/env node
/**
 * feature-status.mjs — print feature pipeline status and next incomplete phase.
 * Usage:
 *   node scripts/skailr/feature-status.mjs [--progress .claude/tmp/progress.md] [--root .claude/tmp] [--json]
 *
 * When `<root>/board.md` exists, merges ticket-board state (via ticket-status)
 * so resume can continue the frontier instead of only BE/FE slices.
 * Default root is the directory containing progress.md (or `.claude/tmp`).
 */
import { readFileSync, existsSync, readdirSync } from "node:fs";
import { join, resolve, dirname } from "node:path";
import { spawnSync } from "node:child_process";

function parseFrontmatter(text) {
  if (!text.startsWith("---")) return {};
  const end = text.indexOf("\n---", 3);
  if (end === -1) return {};
  const data = {};
  for (const line of text.slice(3, end).trim().split("\n")) {
    const i = line.indexOf(":");
    if (i === -1) continue;
    data[line.slice(0, i).trim()] = line.slice(i + 1).trim();
  }
  return data;
}

function extractTable(body, heading) {
  const rows = [];
  const lines = body.split("\n");
  let inSection = false;
  for (const line of lines) {
    if (line.startsWith(`## ${heading}`)) {
      inSection = true;
      continue;
    }
    if (inSection && line.startsWith("## ")) break;
    if (!inSection || !line.startsWith("|")) continue;
    if (line.includes("---") || /Phase|Slice|ID \|/i.test(line)) continue;
    const raw = line.split("|").map((c) => c.trim());
    if (raw.length && raw[0] === "") raw.shift();
    if (raw.length && raw[raw.length - 1] === "") raw.pop();
    if (raw.length < 2) continue;
    rows.push({
      name: raw[0],
      status: raw[1],
      completed: raw[2] || "",
      extra: raw.length >= 4 ? raw[3] : raw[2] || "",
    });
  }
  return rows;
}

const ORDER = [
  "research",
  "story",
  "spec",
  "build",
  "verify",
  "validate",
  "docs",
];

function loadTicketBoard(root) {
  const board = join(root, "board.md");
  if (!existsSync(resolve(board))) return null;
  const script = resolve("scripts/skailr/ticket-status.mjs");
  if (!existsSync(script)) return { hasBoard: true, error: "no_ticket_status_script" };
  const r = spawnSync(
    process.execPath,
    [script, "--root", root, "--json"],
    { encoding: "utf8" },
  );
  try {
    return JSON.parse(r.stdout || "{}");
  } catch {
    return { hasBoard: true, error: "ticket_status_parse_failed", stderr: r.stderr };
  }
}

function main() {
  let progressPath = ".claude/tmp/progress.md";
  let rootArg = null;
  let json = false;
  for (let i = 2; i < process.argv.length; i++) {
    if (process.argv[i] === "--progress") progressPath = process.argv[++i];
    else if (process.argv[i] === "--root") rootArg = process.argv[++i];
    else if (process.argv[i] === "--json") json = true;
  }
  if (!existsSync(resolve(progressPath))) {
    const payload = { ok: false, error: "no_progress", next: null };
    if (json) console.log(JSON.stringify(payload));
    else
      console.log(
        "No progress file found. Run /ship-feature or /yolo first (or seed progress.md).",
      );
    process.exit(0);
  }
  const text = readFileSync(progressPath, "utf8");
  const fm = parseFrontmatter(text);
  const body = text.startsWith("---")
    ? text.slice(text.indexOf("\n---", 3) + 4)
    : text;
  const phases = extractTable(body, "Phases").map((r) => ({
    phase: r.name,
    status: r.status,
    completed: r.completed,
    notes: r.extra,
  }));
  const buildSlices = extractTable(body, "Build slice (when build is in_progress)").map(
    (r) => ({
      slice: r.name,
      status: r.status,
      report: r.extra || r.completed,
    }),
  );
  const progressTickets = extractTable(body, "Tickets (when board exists)").map((r) => ({
    id: r.name,
    title: r.status,
    status: r.completed,
    report: r.extra,
  }));

  let next = null;
  for (const name of ORDER) {
    const row = phases.find((p) => p.phase === name);
    if (!row || row.status !== "complete") {
      next = name;
      break;
    }
  }

  const buildRow = phases.find((p) => p.phase === "build");
  const partialBuild =
    next === "build" &&
    buildRow &&
    (buildRow.status === "in_progress" ||
      buildSlices.some((s) => s.status === "complete"))
      ? buildSlices.filter((s) => s.status !== "complete").map((s) => s.slice)
      : [];

  const artifactRoot = rootArg || dirname(progressPath).replace(/\\/g, "/");
  const board = loadTicketBoard(artifactRoot);

  const handoffs = [];
  if (next === "build") {
    const handoffDir = join(artifactRoot, "handoff");
    if (existsSync(resolve(handoffDir))) {
      for (const f of readdirSync(resolve(handoffDir))) {
        if (!f.endsWith(".md")) continue;
        const key = f.replace(/\.md$/, "");
        handoffs.push({
          slice: key,
          ticket: /^T-\d+/i.test(key) ? key : null,
          path: join(handoffDir, f).replace(/\\/g, "/"),
        });
      }
    }
  }

  const defaultRequest = join(artifactRoot, "request.md").replace(/\\/g, "/");
  const payload = {
    ok: true,
    feature: fm.feature || null,
    mode: fm.mode || null,
    status: fm.status || null,
    updated: fm.updated || null,
    request: fm.request || defaultRequest,
    artifactRoot,
    phases,
    buildSlices,
    progressTickets: progressTickets.length ? progressTickets : null,
    board: board && board.hasBoard !== false ? board : null,
    next,
    partialBuild: partialBuild.length ? partialBuild : null,
    partialRoles:
      board && board.partialRoles && board.partialRoles.length
        ? board.partialRoles
        : partialBuild.length
          ? partialBuild
          : null,
    frontier: board && board.frontier ? board.frontier : null,
    parallel: board && board.parallel ? board.parallel : null,
    handoffs: handoffs.length ? handoffs : null,
    complete: next === null,
  };
  if (json) console.log(JSON.stringify(payload, null, 2));
  else {
    console.log(`Feature: ${payload.feature || "(unknown)"}`);
    console.log(`Mode:    ${payload.mode || "(unknown)"}`);
    console.log(`Status:  ${payload.status || "(unknown)"}`);
    console.log(`Root:    ${payload.artifactRoot}`);
    console.log(`Next:    ${payload.next || "(all phases complete)"}`);
    if (payload.board && payload.board.count != null) {
      console.log(
        `Board:   ${payload.board.count} tickets; frontier=${(payload.frontier || []).map((t) => t.id).join(",") || "none"}`,
      );
    }
    if (payload.partialBuild) {
      console.log(`Build pending slices: ${payload.partialBuild.join(", ")}`);
    }
    if (payload.handoffs) {
      console.log(
        `Handoffs: ${payload.handoffs.map((h) => `${h.slice}=${h.path}`).join(", ")}`,
      );
    }
    for (const p of phases) console.log(`  - ${p.phase}: ${p.status}`);
  }
}

main();
