#!/usr/bin/env node
/**
 * status.mjs — one view of the in-flight run: program ledger, active feature,
 * ticket frontier, channel inbox with age, blockers.
 *
 * Read-only composition of the existing readers (ledger-status, feature-status,
 * ticket-status) plus a channel-inbox scan. Works standalone (.claude/tmp) and
 * nested (workstreams/<ws>/features/<slug>).
 *
 * Usage: node scripts/skailr/status.mjs [--root .] [--json]
 * Exit: always 0 (this is a viewer, not a gate).
 */
import { readFileSync, existsSync, readdirSync } from "node:fs";
import { join, resolve, dirname } from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const SCRIPTS = dirname(fileURLToPath(import.meta.url));

function parseArgs(argv) {
  const out = { root: ".", json: false };
  for (let i = 2; i < argv.length; i++) {
    if (argv[i] === "--root") out.root = argv[++i];
    else if (argv[i] === "--json") out.json = true;
    else if (argv[i] === "--help") {
      console.log("Usage: status.mjs [--root .] [--json]");
      process.exit(0);
    } else {
      console.error(`Unknown arg: ${argv[i]}`);
      process.exit(2);
    }
  }
  return out;
}

function runJson(root, script, args) {
  const r = spawnSync(process.execPath, [join(SCRIPTS, script), ...args], {
    cwd: root,
    encoding: "utf8",
  });
  try {
    return JSON.parse(r.stdout);
  } catch {
    return null;
  }
}

const MSG_RE = /### (MSG-\d+)\n([\s\S]*?)\n---\n/g;

function scanChannels(dir) {
  const messages = [];
  if (!existsSync(dir)) return messages;
  for (const f of readdirSync(dir).filter((x) => x.endsWith(".md") && x !== "PROTOCOL.md")) {
    const text = readFileSync(join(dir, f), "utf8");
    for (const m of text.matchAll(MSG_RE)) {
      const fields = {};
      for (const line of m[2].split("\n")) {
        const i = line.indexOf(":");
        if (i !== -1) fields[line.slice(0, i).trim()] = line.slice(i + 1).trim();
      }
      messages.push({ id: m[1], seq: Number(m[1].slice(4)), file: f, ...fields });
    }
  }
  return messages;
}

function inboxOf(messages) {
  const settled = (m) => m.status === "resolved" || m.status === "answered";
  const maxSeq = Math.max(0, ...messages.map((m) => m.seq));
  return messages
    .filter(
      (m) =>
        !settled(m) &&
        (m.status === "open" ||
          m.status === "blocked-on-human" ||
          m.type === "contract-change" ||
          (m.to && m.to.includes("@human"))),
    )
    .map((m) => ({ ...m, age: maxSeq - m.seq })); // messages posted since this one
}

function section(body, heading) {
  const re = new RegExp(`^## ${heading}\\s*\\n([\\s\\S]*?)(?=\\n## |$)`, "m");
  const m = body.match(re);
  return m ? m[1].trim() : null;
}

function main() {
  const args = parseArgs(process.argv);
  const root = resolve(args.root);
  const out = { program: null, feature: null, board: null, inbox: [], blockers: [] };

  // ---- Program tier ----
  const ledgerPath = join(root, ".claude/program/ledger.md");
  if (existsSync(ledgerPath)) {
    out.program = runJson(root, "ledger-status.mjs", ["--ledger", ledgerPath, "--json"]);
    const body = readFileSync(ledgerPath, "utf8");
    const blockers = section(body, "Blockers");
    if (blockers && !/^none\.?$/i.test(blockers)) {
      out.blockers = blockers.split("\n").map((l) => l.trim()).filter(Boolean);
    }
  }

  // ---- Active feature root: nested cursor first, else standalone ----
  let featureRoot = null;
  if (out.program?.artifactRoot && existsSync(join(root, out.program.artifactRoot))) {
    featureRoot = out.program.artifactRoot;
  } else if (existsSync(join(root, ".claude/tmp/progress.md"))) {
    featureRoot = ".claude/tmp";
  }
  if (featureRoot) {
    out.feature = runJson(root, "feature-status.mjs", [
      "--progress",
      join(root, featureRoot, "progress.md"),
      "--root",
      join(root, featureRoot),
      "--json",
    ]);
    if (existsSync(join(root, featureRoot, "board.md"))) {
      out.board = runJson(root, "ticket-status.mjs", ["status", "--root", join(root, featureRoot), "--json"]);
    }
  }

  // ---- Channel inboxes (program + active feature + standalone tmp) ----
  const channelDirs = new Set([join(root, ".claude/program/channels")]);
  if (featureRoot) channelDirs.add(join(root, featureRoot, "channels"));
  channelDirs.add(join(root, ".claude/tmp/channels"));
  for (const dir of channelDirs) {
    for (const m of inboxOf(scanChannels(dir))) {
      out.inbox.push({ ...m, dir: dir.slice(root.length + 1) });
    }
  }

  if (args.json) {
    console.log(JSON.stringify(out, null, 2));
    return;
  }

  // ---- Human view ----
  if (out.program?.ok) {
    console.log(`PROGRAM  ${out.program.program} — ${out.program.status}${out.program.next ? ` (next: ${out.program.next})` : ""}`);
    for (const c of out.program.featureCursors || []) {
      console.log(`  ${c.workstream}${c.feature ? `/${c.feature}` : ""}: ${c.phase ?? c.status}`);
    }
  } else {
    console.log("PROGRAM  none (no ledger)");
  }
  if (out.feature?.ok) {
    console.log(`FEATURE  ${out.feature.feature} [${out.feature.mode}] — ${out.feature.status}${out.feature.next ? ` (next: ${out.feature.next})` : ""}  root=${featureRoot}`);
  } else {
    console.log("FEATURE  none in flight");
  }
  if (out.board?.ok !== undefined) {
    const t = out.board.tickets || [];
    const open = t.filter((x) => x.status === "open").length;
    const claimed = t.filter((x) => x.status === "claimed").length;
    const done = t.filter((x) => x.status === "done").length;
    console.log(`BOARD    ${t.length} tickets — ${done} done, ${claimed} claimed, ${open} open${out.board.frontier?.length ? `; frontier: ${out.board.frontier.map((f) => f.id ?? f).join(", ")}` : ""}`);
  }
  if (out.inbox.length) {
    console.log(`INBOX    ${out.inbox.length} item(s)`);
    for (const m of out.inbox) {
      console.log(`  ${m.id} [${m.type}] to=${m.to} status=${m.status} age=${m.age} (${m.dir}/${m.file})`);
    }
  } else {
    console.log("INBOX    empty");
  }
  if (out.blockers.length) {
    console.log("BLOCKERS");
    for (const b of out.blockers) console.log(`  ${b}`);
  }
}

main();
