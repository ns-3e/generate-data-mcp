#!/usr/bin/env node
/**
 * archive-program.mjs — move live `.claude/program/` runtime state into
 * `.claude/program/archive/<timestamp>-<slug>/` so the next initiative starts clean.
 *
 * Usage:
 *   node scripts/skailr/archive-program.mjs [--dry-run] [--force]
 *   node scripts/skailr/archive-program.mjs dry-run
 *
 * Exit 0 with `noop` when nothing live to archive.
 * Refuses incomplete ledgers unless `--force` (start-over / new initiative).
 */
import {
  existsSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  renameSync,
  copyFileSync,
  writeFileSync,
} from "node:fs";
import { join, resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(__dirname, "../..");

const PROGRAM_DIR = ".claude/program";
const ARCHIVE_DIR = join(PROGRAM_DIR, "archive");
const LEDGER_PATH = join(PROGRAM_DIR, "ledger.md");

/** Top-level files/dirs under .claude/program/ that are live runtime. */
const ROOT_RUNTIME = [
  "request.md",
  "brief.md",
  "plan.md",
  "mode.md",
  "ledger.md",
  "ownership.json",
  "contracts",
  "workstreams",
  "field-guide.md",
  "model-usage.md",
  "status-digest.md",
  "program-validation-report.md",
];

const USAGE = `Usage:
  node scripts/skailr/archive-program.mjs [--dry-run] [--force]
  node scripts/skailr/archive-program.mjs dry-run

Moves live program runtime into .claude/program/archive/<ts>-<slug>/.
Refuses incomplete ledgers unless --force.`;

function die(msg, code = 1) {
  console.error(`archive-program: ${msg}`);
  process.exit(code);
}

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

function sanitizeSlug(raw) {
  const s = String(raw || "program")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64);
  return s || "program";
}

function timestampUtc() {
  return new Date()
    .toISOString()
    .replace(/\.\d{3}Z$/, "Z")
    .replace(/[-:]/g, "");
}

function ledgerComplete(fm, ledgerText) {
  if ((fm.status || "").toLowerCase() === "complete") return true;
  // Fallback: all phases in Phases table are complete (same idea as ledger-status)
  const body = ledgerText.startsWith("---")
    ? ledgerText.slice(ledgerText.indexOf("\n---", 3) + 4)
    : ledgerText;
  const lines = body.split("\n");
  let inPhases = false;
  const statuses = [];
  for (const line of lines) {
    if (line.startsWith("## Phases")) {
      inPhases = true;
      continue;
    }
    if (inPhases && line.startsWith("## ")) break;
    if (!inPhases || !line.startsWith("|")) continue;
    if (line.includes("---")) continue;
    const cols = line
      .split("|")
      .map((c) => c.trim())
      .filter(Boolean);
    if (cols.length < 2) continue;
    if (/^Phase$/i.test(cols[0])) continue;
    statuses.push(cols[1]);
  }
  if (!statuses.length) return false;
  return statuses.every((s) => s === "complete" || s === "done");
}

function collectLiveItems(programAbs) {
  const items = [];

  for (const name of ROOT_RUNTIME) {
    const full = join(programAbs, name);
    if (existsSync(full)) items.push({ rel: name, abs: full });
  }

  const channelsAbs = join(programAbs, "channels");
  if (existsSync(channelsAbs)) {
    for (const ent of readdirSync(channelsAbs, { withFileTypes: true })) {
      const name = ent.name;
      if (name === "PROTOCOL.md" || name === "feature.md") continue;
      // Archive program.md, ws-*.md boards, and rotate-channels output
      if (name === "program.md" || /^ws-.+\.md$/i.test(name) || /^archive-.+\.md$/i.test(name)) {
        items.push({
          rel: join("channels", name),
          abs: join(channelsAbs, name),
        });
      }
    }
  }

  return items;
}

function emptyProgramBoard() {
  return `# Channel: program

Program-wide messages: announcements, contract-change requests, escalations, anything
crossing two or more workstreams. Append-only. See \`PROTOCOL.md\` for the format and the
posting discipline.

---
`;
}

function reseedProgramBoard(programAbs, dryRun) {
  const dest = join(programAbs, "channels", "program.md");
  const packTemplate = join(
    REPO_ROOT,
    ".claude/program/channels/program.md",
  );
  // Prefer a minimal empty board so the next run does not inherit the example thread.
  // Fall back to copying the pack template if write fails somehow.
  if (dryRun) {
    console.log(`archive-program: would re-seed ${dest}`);
    return;
  }
  mkdirSync(dirname(dest), { recursive: true });
  try {
    writeFileSync(dest, emptyProgramBoard(), "utf8");
  } catch {
    if (existsSync(packTemplate)) {
      copyFileSync(packTemplate, dest);
    } else {
      writeFileSync(dest, emptyProgramBoard(), "utf8");
    }
  }
  console.log(`archive-program: re-seeded ${dest}`);
}

function main() {
  const args = process.argv.slice(2);
  let dryRun = false;
  let force = false;
  for (const a of args) {
    if (a === "--dry-run" || a === "dry-run") dryRun = true;
    else if (a === "--force") force = true;
    else if (a === "-h" || a === "--help") {
      console.log(USAGE);
      process.exit(0);
    } else {
      die(`unknown arg: ${a}\n${USAGE}`, 2);
    }
  }

  const cwd = process.cwd();
  const programAbs = resolve(cwd, PROGRAM_DIR);

  if (!existsSync(programAbs)) {
    console.log("archive-program: noop (no .claude/program/)");
    process.exit(0);
  }

  const items = collectLiveItems(programAbs);
  if (!items.length) {
    console.log("archive-program: noop (no live runtime)");
    process.exit(0);
  }

  const ledgerAbs = resolve(cwd, LEDGER_PATH);
  let fm = {};
  let ledgerText = "";
  let complete = false;
  let ledgerState = "incomplete";
  if (existsSync(ledgerAbs)) {
    ledgerText = readFileSync(ledgerAbs, "utf8");
    fm = parseFrontmatter(ledgerText);
    complete = ledgerComplete(fm, ledgerText);
    ledgerState = complete ? "complete" : "incomplete";
  } else {
    // No ledger but live files: pre-freeze leftovers (the ledger is only seeded at
    // contract freeze) or an abandoned run. Archiving preserves everything, so allow
    // it without --force — but report it honestly, never as "complete".
    complete = true;
    ledgerState = "no-ledger (archiving as leftovers)";
  }

  if (!complete && !force) {
    die(
      `ledger is incomplete (status=${fm.status || "unknown"}); pass --force to archive anyway`,
    );
  }

  const slug = sanitizeSlug(fm.program);
  const destRel = join(ARCHIVE_DIR, `${timestampUtc()}-${slug}`);
  const destAbs = resolve(cwd, destRel);

  if (dryRun) {
    console.log(
      `archive-program: dry-run → ${destRel} (force=${force} ledger=${ledgerState})`,
    );
    for (const it of items) {
      console.log(`archive-program: would move ${it.rel}`);
    }
    reseedProgramBoard(programAbs, true);
    console.log(`archive-program: dry-run done (${items.length} path(s))`);
    process.exit(0);
  }

  mkdirSync(destAbs, { recursive: true });
  mkdirSync(join(destAbs, "channels"), { recursive: true });

  for (const it of items) {
    const target = join(destAbs, it.rel);
    mkdirSync(dirname(target), { recursive: true });
    if (existsSync(target)) {
      die(`archive destination already exists: ${target}`);
    }
    renameSync(it.abs, target);
    console.log(`archive-program: moved ${it.rel}`);
  }

  reseedProgramBoard(programAbs, false);

  console.log(
    `archive-program: archived ${items.length} path(s) → ${destRel}${force && !complete ? " (forced)" : ""}`,
  );
}

main();
