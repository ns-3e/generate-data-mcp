#!/usr/bin/env node
/**
 * ledger-status.mjs — print program status, next incomplete phase, and feature cursors.
 * Usage: node scripts/skailr/ledger-status.mjs [--ledger .claude/program/ledger.md] [--json]
 */
import { readFileSync, existsSync } from "node:fs";
import { resolve } from "node:path";

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
    if (line.includes("---")) continue;
    const cols = line
      .split("|")
      .map((c) => c.trim())
      .filter(Boolean);
    if (cols.length < 2) continue;
    // skip header rows
    if (/^(Phase|Workstream)$/i.test(cols[0])) continue;
    rows.push(cols);
  }
  return rows;
}

const ORDER = [
  "A_kernel",
  "B_workstreams",
  "C_integration",
  "D_validation",
  "E_documentation",
];

function parseFeatureCursors(body) {
  const raw = extractTable(body, "Workstream cursors");
  return raw.map((cols) => {
    // Legacy: Workstream | Team | Phase | Status
    // New: Workstream | Team | Feature | Feature phase | Status
    if (cols.length >= 5) {
      return {
        workstream: cols[0],
        team: cols[1],
        feature: cols[2],
        featurePhase: cols[3],
        status: cols[4],
      };
    }
    return {
      workstream: cols[0],
      team: cols[1] || null,
      feature: null,
      featurePhase: cols[2] || null,
      status: cols[3] || cols[2] || "pending",
    };
  });
}

function pickNextFeature(cursors) {
  const incomplete = cursors.filter(
    (c) => c.feature && c.status !== "complete" && c.status !== "done",
  );
  if (!incomplete.length) return null;
  // Prefer in_progress, else first pending whose depends are not tracked here (orchestrator / plan.md)
  const active = incomplete.find(
    (c) =>
      c.status === "in_progress" ||
      (c.featurePhase &&
        c.featurePhase !== "pending" &&
        c.featurePhase !== "complete"),
  );
  return active || incomplete[0];
}

function main() {
  let ledgerPath = ".claude/program/ledger.md";
  let json = false;
  for (let i = 2; i < process.argv.length; i++) {
    if (process.argv[i] === "--ledger") ledgerPath = process.argv[++i];
    if (process.argv[i] === "--json") json = true;
  }
  if (!existsSync(resolve(ledgerPath))) {
    const payload = { ok: false, error: "no_ledger", next: null };
    if (json) console.log(JSON.stringify(payload));
    else console.log("No ledger found. Run /discover → /plan-program first.");
    process.exit(0);
  }
  const text = readFileSync(ledgerPath, "utf8");
  const fm = parseFrontmatter(text);
  const body = text.startsWith("---")
    ? text.slice(text.indexOf("\n---", 3) + 4)
    : text;
  const phaseRows = extractTable(body, "Phases");
  const phases = phaseRows.map((cols) => ({
    phase: cols[0],
    status: cols[1],
  }));
  let next = null;
  for (const name of ORDER) {
    const row = phases.find((p) => p.phase === name);
    // "done" and "complete" are both terminal — archive-program.mjs and the shipped
    // example ledger use "done"; treating it as incomplete made `next` point at
    // A_kernel on a finished program, which /continue-program reads as a resume target.
    if (!row || (row.status !== "complete" && row.status !== "done")) {
      next = name;
      break;
    }
  }

  const featureCursors = parseFeatureCursors(body);
  const nextFeature = pickNextFeature(featureCursors);
  const artifactRoot = nextFeature
    ? `.claude/program/workstreams/${nextFeature.workstream}/features/${nextFeature.feature}`
    : null;

  const payload = {
    ok: true,
    program: fm.program || null,
    status: fm.status || null,
    updated: fm.updated || null,
    phases,
    next,
    featureCursors,
    nextFeature,
    artifactRoot,
    complete: next === null,
  };
  if (json) console.log(JSON.stringify(payload, null, 2));
  else {
    console.log(`Program: ${payload.program || "(unknown)"}`);
    console.log(`Status:  ${payload.status || "(unknown)"}`);
    console.log(`Next:    ${payload.next || "(all phases complete)"}`);
    for (const p of phases) console.log(`  - ${p.phase}: ${p.status}`);
    if (featureCursors.length) {
      console.log("Features:");
      for (const f of featureCursors) {
        console.log(
          `  - ${f.workstream}/${f.feature || "(ws)"}: ${f.featurePhase || "-"} / ${f.status}`,
        );
      }
      if (nextFeature) {
        console.log(
          `Next feature: ${nextFeature.workstream}/${nextFeature.feature} (${nextFeature.featurePhase || "pending"})`,
        );
        console.log(`Artifact root: ${artifactRoot}`);
      }
    }
  }
}

main();
