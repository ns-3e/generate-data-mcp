#!/usr/bin/env node
/**
 * validate-channels.mjs — parse channel boards; fail on malformed MSG or report open human/contract-change.
 * Usage: node scripts/skailr/validate-channels.mjs [--dir .claude/program/channels] [--strict-inbox]
 */
import { readFileSync, existsSync, readdirSync, statSync } from "node:fs";
import { join, resolve } from "node:path";

const MSG_RE =
  /### (MSG-\d+)\n([\s\S]*?)\n---\n([\s\S]*?)(?=\n### MSG-|\n*$)/g;

const TYPES = new Set([
  "question",
  "answer",
  "decision",
  "heads-up",
  "blocker",
  "contract-change",
  "ack",
]);
const STATUSES = new Set(["open", "answered", "resolved", "blocked-on-human"]);

function parseArgs(argv) {
  const out = { dir: ".claude/program/channels", strictInbox: false, roster: null };
  for (let i = 2; i < argv.length; i++) {
    if (argv[i] === "--dir") out.dir = argv[++i];
    else if (argv[i] === "--strict-inbox") out.strictInbox = true;
    else if (argv[i] === "--tmp") out.dir = ".claude/tmp/channels";
    else if (argv[i] === "--roster") out.roster = argv[++i];
    else if (argv[i] === "--no-roster") out.roster = false;
  }
  return out;
}

/** Known addressees: @human/@all/@architect + registry team names + agent role stems.
 *  Warn-only — a message parked on a nonexistent addressee never gets drained. */
function knownAddressees(rosterPath) {
  const known = new Set(["human", "all", "architect"]);
  if (rosterPath && existsSync(rosterPath)) {
    for (const m of readFileSync(rosterPath, "utf8").matchAll(/^### ([a-z][a-z0-9-]*)$/gm)) {
      known.add(m[1]);
    }
  }
  const agentsDir = ".claude/agents";
  if (existsSync(agentsDir)) {
    for (const team of readdirSync(agentsDir)) {
      const teamDir = join(agentsDir, team);
      if (!statSync(teamDir).isDirectory()) continue;
      for (const f of readdirSync(teamDir)) {
        if (f.endsWith(".md")) known.add(f.replace(/\.md$/, ""));
      }
    }
  }
  return known;
}

function parseFile(path, text) {
  const errors = [];
  const messages = [];
  const re = new RegExp(MSG_RE.source, "g");
  let m;
  while ((m = re.exec(text)) !== null) {
    const id = m[1];
    const header = m[2];
    const body = m[3].trim();
    const fields = {};
    for (const line of header.split("\n")) {
      const i = line.indexOf(":");
      if (i === -1) continue;
      fields[line.slice(0, i).trim()] = line.slice(i + 1).trim();
    }
    if (!fields.from) errors.push(`${path} ${id}: missing from`);
    if (!fields.to) errors.push(`${path} ${id}: missing to`);
    if (!TYPES.has(fields.type)) errors.push(`${path} ${id}: bad type ${fields.type}`);
    if (!STATUSES.has(fields.status)) errors.push(`${path} ${id}: bad status ${fields.status}`);
    if (!body) errors.push(`${path} ${id}: empty body`);
    messages.push({ id, ...fields, body, file: path });
  }
  // A heading that exists in the raw text but did not parse (e.g. missing the `---`
  // separator) would otherwise vanish silently — invisible to routing AND validation.
  const rawHeadings = (text.match(/^### MSG-\d+/gm) || []).length;
  if (rawHeadings !== messages.length) {
    errors.push(
      `${path}: ${rawHeadings} "### MSG-" heading(s) but only ${messages.length} parsed — malformed message block (check the \`---\` separator and header lines)`,
    );
  }
  return { messages, errors };
}

function main() {
  const args = parseArgs(process.argv);
  const dir = resolve(args.dir);
  if (!existsSync(dir)) {
    console.log(`No channels dir at ${args.dir}; skip.`);
    process.exit(0);
  }

  const files = readdirSync(dir).filter((f) => f.endsWith(".md") && f !== "PROTOCOL.md");
  let all = [];
  let errors = [];
  for (const f of files) {
    const text = readFileSync(join(dir, f), "utf8");
    const r = parseFile(f, text);
    all = all.concat(r.messages);
    errors = errors.concat(r.errors);
  }

  // Resolved/answered messages leave the inbox regardless of type or addressee —
  // otherwise every contract-change (including the seeded worked example) is a
  // phantom inbox item forever and --strict-inbox can never pass again.
  const settled = (m) => m.status === "resolved" || m.status === "answered";
  const inbox = all.filter(
    (m) =>
      !settled(m) &&
      (m.status === "open" ||
        m.status === "blocked-on-human" ||
        m.type === "contract-change" ||
        (m.to && m.to.includes("@human"))),
  );

  if (errors.length) {
    console.error(errors.join("\n"));
    process.exit(1);
  }

  const maxSeq = Math.max(0, ...all.map((m) => Number(m.id.slice(4)) || 0));
  console.log(`OK: ${all.length} message(s); inbox=${inbox.length}`);
  for (const m of inbox) {
    const age = maxSeq - (Number(m.id.slice(4)) || 0); // messages posted since
    console.log(`  INBOX ${m.id} type=${m.type} to=${m.to} status=${m.status} age=${age} (${m.file})`);
  }

  // Addressee sanity (warn-only): default roster is the teams registry when present.
  if (args.roster !== false) {
    const rosterPath = args.roster || ".claude/teams/registry.md";
    const known = knownAddressees(rosterPath);
    for (const m of all) {
      if (m.status === "resolved" || m.status === "answered") continue;
      for (const raw of (m.to || "").split(/[,\s]+/).filter(Boolean)) {
        const name = raw.replace(/^@/, "");
        if (!name || known.has(name) || name.startsWith("ws-")) continue;
        console.log(`  WARN ${m.id}: addressee @${name} matches no team, agent role, ws-*, @human/@all/@architect — may never be drained (${m.file})`);
      }
    }
  }

  if (args.strictInbox && inbox.length) {
    console.error("Strict inbox: open human/contract-change messages remain");
    process.exit(2);
  }
}

main();
