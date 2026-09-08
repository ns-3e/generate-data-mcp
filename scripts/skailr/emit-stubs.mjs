#!/usr/bin/env node
/**
 * emit-stubs.mjs — emit stub modules from OpenAPI/JSON Schema sidecars next to contracts.
 * Usage: node scripts/skailr/emit-stubs.mjs [--dir .claude/program/contracts] [--out .claude/program/stubs]
 */
import { readFileSync, existsSync, readdirSync, mkdirSync, writeFileSync } from "node:fs";
import { join, resolve, basename } from "node:path";

function parseArgs(argv) {
  const out = {
    dir: ".claude/program/contracts",
    outDir: ".claude/program/stubs",
  };
  for (let i = 2; i < argv.length; i++) {
    if (argv[i] === "--dir") out.dir = argv[++i];
    else if (argv[i] === "--out") out.outDir = argv[++i];
  }
  return out;
}

function stubFromOpenApi(id, text) {
  const paths = [...text.matchAll(/^\s{2}(\/[^\s:]+):/gm)].map((m) => m[1]);
  const methods = [...text.matchAll(/^\s{4}(get|post|put|patch|delete):/gim)].map((m) =>
    m[1].toUpperCase(),
  );
  const lines = [
    `/** AUTO-GENERATED STUB for contract ${id} — replace at integration */`,
    `export const STUB = true;`,
    `export const contractId = ${JSON.stringify(id)};`,
    `export const routes = ${JSON.stringify(paths)};`,
    `export async function handler(req, res) {`,
    `  res.statusCode = 501;`,
    `  res.end(JSON.stringify({ error: "stub", contractId: ${JSON.stringify(id)}, methods: ${JSON.stringify(methods)} }));`,
    `}`,
    ``,
  ];
  return lines.join("\n");
}

function stubFromJsonSchema(id, text) {
  let schema;
  try {
    schema = JSON.parse(text);
  } catch {
    schema = { title: id };
  }
  return [
    `/** AUTO-GENERATED STUB types for ${id} */`,
    `export const STUB = true;`,
    `export const contractId = ${JSON.stringify(id)};`,
    `export const schemaTitle = ${JSON.stringify(schema.title || id)};`,
    `export function assertShape(_value) {`,
    `  throw new Error("Stub: real producer not wired for " + contractId);`,
    `}`,
    ``,
  ].join("\n");
}

function main() {
  const args = parseArgs(process.argv);
  const dir = resolve(args.dir);
  if (!existsSync(dir)) {
    console.log(`No contracts dir; skip.`);
    process.exit(0);
  }
  mkdirSync(resolve(args.outDir), { recursive: true });
  const files = readdirSync(dir);
  let n = 0;
  const withSidecar = new Set();
  for (const f of files) {
    const id = basename(f).replace(/\.(openapi\.ya?ml|schema\.json|md)$/i, "");
    if (f.endsWith(".openapi.yaml") || f.endsWith(".openapi.yml")) {
      const stub = stubFromOpenApi(id, readFileSync(join(dir, f), "utf8"));
      writeFileSync(join(args.outDir, `${id}.stub.js`), stub);
      withSidecar.add(id);
      n++;
    } else if (f.endsWith(".schema.json")) {
      const stub = stubFromJsonSchema(id, readFileSync(join(dir, f), "utf8"));
      writeFileSync(join(args.outDir, `${id}.stub.js`), stub);
      withSidecar.add(id);
      n++;
    }
  }
  // Name what was skipped — "emitted 0" must be distinguishable from "nothing to do".
  const skipped = files
    .filter((f) => f.endsWith(".md"))
    .map((f) => basename(f).replace(/\.md$/i, ""))
    .filter((id) => !withSidecar.has(id));
  console.log(`OK: emitted ${n} stub(s) → ${args.outDir}`);
  if (skipped.length) {
    console.log(`  no sidecar (no stub emitted): ${skipped.join(", ")}`);
  }
}

main();
