#!/usr/bin/env node
/**
 * check-blocks.mjs — canonical-boilerplate byte-identity lint.
 *
 * The pack deliberately repeats a handful of canonical blocks across agent files
 * (no include mechanism exists, and generating .claude/ would invert the
 * source-of-truth invariant). The failure mode is edit-one-forget-39 drift.
 *
 * For each block: the canonical text is extracted from a reference file (anchor
 * line → next blank line). Every agent file whose text contains the anchor line is
 * a carrier and must contain the canonical text verbatim (as a substring — files
 * may extend a block with role-specific lines after it, e.g. content-writer's
 * brand-voice rider). Files listed in `exclude` carry a deliberate variant and are
 * skipped. `minCarriers` guards against a block silently disappearing pack-wide.
 *
 * Usage: node scripts/skailr/check-blocks.mjs [--root .]
 * Exit: 0 all blocks identical; 1 drift or missing block.
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, resolve, relative } from "node:path";

const BLOCKS = [
  {
    name: "tone",
    ref: ".claude/agents/engineering/backend-engineer.md",
    anchor: "Be extremely concise. Sacrifice grammar for the sake of concision.",
    exclude: [],
    minCarriers: 40,
  },
  {
    name: "task-return",
    ref: ".claude/agents/engineering/backend-engineer.md",
    anchor: null, // prefix-anchored
    prefix: "Task return: ",
    // architect deliberately returns board.md/tickets alongside the spec
    exclude: [".claude/agents/engineering/architect.md"],
    minCarriers: 39,
  },
  {
    name: "artifact-root",
    ref: ".claude/agents/engineering/backend-engineer.md",
    anchor: "Task prompts may set `ARTIFACT_ROOT=<path>`. Default when unset: `.claude/tmp`.",
    exclude: [],
    minCarriers: 8,
  },
  {
    name: "domain-ws-root",
    ref: ".claude/agents/content/content-lead.md",
    anchor: "Task prompts may set `WS_ROOT=<path>`. Default when unset: `.claude/program/workstreams/<ws>`. A standalone single-workstream run passes `WS_ROOT=.claude/tmp`. Read and write workstream artifacts only under `$WS_ROOT`; leads pass `WS_ROOT=<path>` in every worker Task prompt.",
    exclude: [],
    minCarriers: 21,
  },
  {
    name: "context-handoff",
    ref: ".claude/agents/engineering/backend-engineer.md",
    anchor: null,
    prefix: "Long builds can exhaust the context window",
    exclude: [],
    minCarriers: 3,
  },
  {
    name: "cleanup-before-done",
    ref: ".claude/agents/engineering/backend-engineer.md",
    anchor: null,
    prefix: "Before `DONE:`",
    exclude: [],
    minCarriers: 3,
  },
];

function parseArgs(argv) {
  const out = { root: "." };
  for (let i = 2; i < argv.length; i++) {
    if (argv[i] === "--root") out.root = argv[++i];
    else if (argv[i] === "--help") {
      console.log("Usage: check-blocks.mjs [--root .]");
      process.exit(0);
    } else {
      console.error(`Unknown arg: ${argv[i]}`);
      process.exit(2);
    }
  }
  return out;
}

function walk(dir) {
  const out = [];
  for (const f of readdirSync(dir)) {
    const p = join(dir, f);
    if (statSync(p).isDirectory()) out.push(...walk(p));
    else if (f.endsWith(".md")) out.push(p);
  }
  return out;
}

function extractParagraph(text, { anchor, prefix }) {
  const lines = text.split("\n");
  const i = anchor
    ? lines.findIndex((l) => l === anchor)
    : lines.findIndex((l) => l.startsWith(prefix));
  if (i === -1) return null;
  let j = i;
  while (j < lines.length && lines[j].trim() !== "") j++;
  return lines.slice(i, j).join("\n");
}

function main() {
  const args = parseArgs(process.argv);
  const root = resolve(args.root);
  const agentsDir = join(root, ".claude/agents");
  const files = walk(agentsDir);
  const errors = [];

  for (const block of BLOCKS) {
    const refPath = join(root, block.ref);
    const canonical = extractParagraph(readFileSync(refPath, "utf8"), block);
    if (!canonical) {
      errors.push(`${block.name}: anchor not found in reference ${block.ref}`);
      continue;
    }
    const excluded = new Set(block.exclude.map((e) => resolve(root, e)));
    let carriers = 0;
    for (const f of files) {
      if (excluded.has(resolve(f))) continue;
      const text = readFileSync(f, "utf8");
      const anchorLine = block.anchor || canonical.split("\n")[0];
      const hasAnchor = block.anchor
        ? text.split("\n").includes(block.anchor)
        : text.split("\n").some((l) => l.startsWith(block.prefix));
      if (!hasAnchor) continue;
      carriers++;
      if (!text.includes(canonical)) {
        errors.push(
          `${block.name}: ${relative(root, f)} diverges from canonical block (ref ${block.ref}, anchor "${anchorLine.slice(0, 60)}…")`,
        );
      }
    }
    if (carriers < block.minCarriers) {
      errors.push(
        `${block.name}: only ${carriers} carrier(s), expected ≥ ${block.minCarriers} — block disappearing or anchor reworded pack-wide?`,
      );
    }
  }

  if (errors.length) {
    console.error("Canonical block drift:\n" + errors.map((e) => `  - ${e}`).join("\n"));
    process.exit(1);
  }
  console.log(`OK: ${BLOCKS.length} canonical block(s) identical across carriers`);
}

main();
