#!/usr/bin/env node
/**
 * rotate-channels.mjs — mechanical channel-board rotation.
 *
 * When a board accumulates more than --max-resolved settled messages, every
 * FULLY-settled thread (root + all `re:` descendants, each resolved/answered) is
 * moved — raw blocks verbatim, whole threads only, never edited — to
 * `archive-<board>.md` in the same directory. Unsettled and partially-settled
 * threads never move. Append-only semantics are preserved: content is relocated
 * intact, nothing is rewritten or deleted.
 *
 * Usage: node scripts/skailr/rotate-channels.mjs
 *          [--dir .claude/program/channels] [--max-resolved 50] [--dry-run]
 * Exit: 0 (including no-op); 2 bad args.
 */
import { readFileSync, writeFileSync, existsSync, readdirSync, appendFileSync } from "node:fs";
import { join, resolve } from "node:path";

function parseArgs(argv) {
  const out = { dir: ".claude/program/channels", maxResolved: 50, dryRun: false };
  for (let i = 2; i < argv.length; i++) {
    if (argv[i] === "--dir") out.dir = argv[++i];
    else if (argv[i] === "--max-resolved") out.maxResolved = Number(argv[++i]);
    else if (argv[i] === "--dry-run") out.dryRun = true;
    else if (argv[i] === "--help") {
      console.log("Usage: rotate-channels.mjs [--dir <channels>] [--max-resolved 50] [--dry-run]");
      process.exit(0);
    } else {
      console.error(`Unknown arg: ${argv[i]}`);
      process.exit(2);
    }
  }
  if (!Number.isFinite(out.maxResolved) || out.maxResolved < 0) {
    console.error("--max-resolved must be a non-negative number");
    process.exit(2);
  }
  return out;
}

/** Split a board into { prefix, blocks: [{id, raw, fields}] } — raw slices, verbatim. */
function splitBoard(text) {
  const starts = [];
  const re = /^### (MSG-\d+)/gm;
  let m;
  while ((m = re.exec(text)) !== null) starts.push({ index: m.index, id: m[1] });
  if (!starts.length) return { prefix: text, blocks: [] };
  const prefix = text.slice(0, starts[0].index);
  const blocks = starts.map((s, i) => {
    const raw = text.slice(s.index, i + 1 < starts.length ? starts[i + 1].index : text.length);
    const header = raw.split("\n---\n")[0];
    const fields = {};
    for (const line of header.split("\n").slice(1)) {
      const j = line.indexOf(":");
      if (j !== -1) fields[line.slice(0, j).trim()] = line.slice(j + 1).trim();
    }
    return { id: s.id, raw, fields };
  });
  return { prefix, blocks };
}

const settled = (b) => b.fields.status === "resolved" || b.fields.status === "answered";

/** Group blocks into threads by following `re:` chains to the ultimate root. */
function threads(blocks) {
  const byId = new Map(blocks.map((b) => [b.id, b]));
  const rootOf = (b) => {
    let cur = b;
    const seen = new Set();
    while (cur.fields.re && byId.has(cur.fields.re) && !seen.has(cur.id)) {
      seen.add(cur.id);
      cur = byId.get(cur.fields.re);
    }
    return cur.id;
  };
  const groups = new Map();
  for (const b of blocks) {
    const r = rootOf(b);
    if (!groups.has(r)) groups.set(r, []);
    groups.get(r).push(b);
  }
  return groups;
}

function main() {
  const args = parseArgs(process.argv);
  const dir = resolve(args.dir);
  if (!existsSync(dir)) {
    console.log(`No channels dir at ${args.dir}; skip.`);
    return;
  }
  const boards = readdirSync(dir).filter(
    (f) => f.endsWith(".md") && f !== "PROTOCOL.md" && !f.startsWith("archive-"),
  );
  let rotatedTotal = 0;
  for (const f of boards) {
    const path = join(dir, f);
    const { prefix, blocks } = splitBoard(readFileSync(path, "utf8"));
    const settledCount = blocks.filter(settled).length;
    if (settledCount <= args.maxResolved) continue;

    const movable = new Set();
    for (const [, members] of threads(blocks)) {
      if (members.every(settled)) for (const b of members) movable.add(b.id);
    }
    if (!movable.size) continue;

    const moved = blocks.filter((b) => movable.has(b.id));
    const kept = blocks.filter((b) => !movable.has(b.id));
    const archivePath = join(dir, `archive-${f}`);

    if (args.dryRun) {
      console.log(`rotate-channels: dry-run ${f} → archive-${f}: would move ${moved.length} message(s) in fully-settled threads, keep ${kept.length}`);
      rotatedTotal += moved.length;
      continue;
    }

    if (!existsSync(archivePath)) {
      writeFileSync(
        archivePath,
        `# Archive: ${f}\n\nFully-settled threads rotated out of \`${f}\` by rotate-channels.mjs.\nBlocks are verbatim; append-only. Do not post new messages here.\n\n`,
      );
    }
    appendFileSync(archivePath, moved.map((b) => b.raw).join(""));
    writeFileSync(path, prefix + kept.map((b) => b.raw).join(""));
    console.log(`rotate-channels: ${f} → archive-${f}: moved ${moved.length} message(s), kept ${kept.length}`);
    rotatedTotal += moved.length;
  }
  if (!rotatedTotal) console.log("rotate-channels: nothing over threshold; no-op");
}

main();
