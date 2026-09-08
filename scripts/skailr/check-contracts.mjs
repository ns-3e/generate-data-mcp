#!/usr/bin/env node
/**
 * check-contracts.mjs — validate contract frontmatter, freeze status, DAG acyclicity hints.
 * Usage: node scripts/skailr/check-contracts.mjs [--dir .claude/program/contracts] [--ledger .claude/program/ledger.md]
 */
import { readFileSync, existsSync, readdirSync, statSync } from "node:fs";
import { join, resolve } from "node:path";

function parseFrontmatter(text) {
  if (!text.startsWith("---")) return {};
  const end = text.indexOf("\n---", 3);
  if (end === -1) return {};
  const data = {};
  for (const line of text.slice(3, end).trim().split("\n")) {
    const i = line.indexOf(":");
    if (i === -1) continue;
    let v = line.slice(i + 1).trim();
    const k = line.slice(0, i).trim();
    if (v.startsWith("[") && v.endsWith("]")) {
      v = v
        .slice(1, -1)
        .split(",")
        .map((s) => s.trim().replace(/^["']|["']$/g, ""))
        .filter(Boolean);
    } else if (/^\d+$/.test(v)) v = Number(v);
    data[k] = v;
  }
  return data;
}

function parseArgs(argv) {
  const out = {
    dir: ".claude/program/contracts",
    ledger: ".claude/program/ledger.md",
    plan: ".claude/program/plan.md",
  };
  for (let i = 2; i < argv.length; i++) {
    if (argv[i] === "--dir") out.dir = argv[++i];
    else if (argv[i] === "--ledger") out.ledger = argv[++i];
    else if (argv[i] === "--plan") out.plan = argv[++i];
    else if (argv[i] === "--consumed") {
      // optional value; defaults to the workstreams tree
      const next = argv[i + 1];
      out.consumed = next && !next.startsWith("--") ? argv[++i] : ".claude/program/workstreams";
    }
  }
  return out;
}

function walkMd(dir) {
  const out = [];
  for (const f of readdirSync(dir)) {
    const p = join(dir, f);
    if (statSync(p).isDirectory()) out.push(...walkMd(p));
    else if (f.endsWith(".md")) out.push(p);
  }
  return out;
}

function main() {
  const args = parseArgs(process.argv);
  const dir = resolve(args.dir);
  if (!existsSync(dir)) {
    console.log(`No contracts dir at ${args.dir}; skip.`);
    process.exit(0);
  }

  const files = readdirSync(dir).filter((f) => f.endsWith(".md"));
  const errors = [];
  const contracts = [];

  for (const f of files) {
    const text = readFileSync(join(dir, f), "utf8");
    const fm = parseFrontmatter(text);
    if (fm.schema && fm.schema !== "skailr.contract/v1") {
      errors.push(`${f}: unexpected schema ${fm.schema}`);
    }
    if (!fm.id) errors.push(`${f}: missing id`);
    if (!fm.version) errors.push(`${f}: missing version`);
    if (!fm.status) errors.push(`${f}: missing status`);
    else if (!["draft", "frozen", "superseded"].includes(String(fm.status).trim())) {
      // Templates live under schemas/, which this script never scans — no carve-out.
      // Any status containing "|" previously bypassed validation entirely.
      errors.push(`${f}: invalid status ${fm.status}`);
    }
    contracts.push({ file: f, ...fm });
  }

  if (existsSync(args.ledger)) {
    const ledger = readFileSync(args.ledger, "utf8");
    const fm = parseFrontmatter(ledger);
    if (fm.status === "building" || fm.status === "approved") {
      const unfrozen = contracts.filter(
        (c) => c.status && !String(c.status).includes("|") && c.status !== "frozen" && c.status !== "superseded",
      );
      if (unfrozen.length) {
        errors.push(
          `Ledger says ${fm.status} but unfrozen contracts: ${unfrozen.map((c) => c.id || c.file).join(", ")}`,
        );
      }
    }
  }

  // Simple DAG: producers/consumers must not form A→B→A on same pair without kernel
  const edges = [];
  for (const c of contracts) {
    const producers = Array.isArray(c.producers) ? c.producers : [];
    const consumers = Array.isArray(c.consumers) ? c.consumers : [];
    for (const p of producers) {
      for (const cons of consumers) {
        if (p && cons) edges.push([p, cons]);
      }
    }
  }
  // detect 2-cycles
  for (const [a, b] of edges) {
    if (edges.some(([x, y]) => x === b && y === a)) {
      errors.push(`Possible cyclic contract dependency between workstreams ${a} <-> ${b}`);
    }
  }

  // --consumed: cross-check `built-against: <id>@<version>` stamps in workstream
  // reports against the current contract versions. A consumer stamped with a stale
  // version was built against an interface that has since been bumped.
  let stamps = 0;
  if (args.consumed) {
    const consumedDir = resolve(args.consumed);
    const byId = new Map(contracts.map((c) => [String(c.id), c]));
    if (existsSync(consumedDir)) {
      for (const f of walkMd(consumedDir)) {
        const text = readFileSync(f, "utf8");
        for (const m of text.matchAll(/^built-against:\s*([a-z0-9-]+)@(\S+)\s*$/gm)) {
          stamps++;
          const [, id, version] = m;
          const c = byId.get(id);
          if (!c) errors.push(`${f}: built-against unknown contract ${id}`);
          else if (String(c.version) !== version) {
            errors.push(`${f}: stale consumer — built against ${id}@${version}, current is ${id}@${c.version}`);
          }
        }
      }
    }
  }

  if (errors.length) {
    console.error(errors.join("\n"));
    process.exit(1);
  }
  console.log(
    `OK: ${contracts.length} contract(s) checked` +
      (args.consumed ? `; ${stamps} built-against stamp(s) verified` : ""),
  );
}

main();
