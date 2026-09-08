#!/usr/bin/env node
/**
 * check-ownership.mjs — fail if diff paths violate ownership map or owners collide.
 * Usage:
 *   node scripts/skailr/check-ownership.mjs [--map path] [--base ref] [--paths-file -]
 *   node scripts/skailr/check-ownership.mjs --from-spec .claude/tmp/spec.md
 */
import { readFileSync, existsSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { resolve } from "node:path";

function minimatch(path, pattern) {
  const p = path.replace(/\\/g, "/");
  const g = pattern.replace(/\\/g, "/");
  if (g.endsWith("/**")) {
    const prefix = g.slice(0, -3);
    return p === prefix.replace(/\/$/, "") || p.startsWith(prefix.endsWith("/") ? prefix : prefix + "/");
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

function parseArgs(argv) {
  const out = { map: ".claude/program/ownership.json", base: null, fromSpec: null, pathsFile: null, mapOnly: false };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--map") out.map = argv[++i];
    else if (a === "--base") out.base = argv[++i];
    else if (a === "--from-spec") out.fromSpec = argv[++i];
    else if (a === "--paths-file") out.pathsFile = argv[++i];
    else if (a === "--map-only") out.mapOnly = true;
    else if (a === "--help") {
      console.log("Usage: check-ownership.mjs [--map ownership.json] [--base ref] [--from-spec spec.md] [--map-only]");
      process.exit(0);
    }
  }
  return out;
}

function ownershipFromSpec(specPath) {
  const text = readFileSync(specPath, "utf8");
  const owners = [];
  const backend = text.match(/BACKEND may write only:\s*(.+)/i);
  const frontend = text.match(/FRONTEND may write only:\s*(.+)/i);
  const data = text.match(/DATA may write only:\s*(.+)/i);
  if (backend) {
    owners.push({
      id: "backend",
      team: "engineering",
      units: backend[1].split(/,\s*/).map((s) => s.trim()).filter(Boolean),
    });
  }
  if (frontend) {
    owners.push({
      id: "frontend",
      team: "engineering",
      units: frontend[1].split(/,\s*/).map((s) => s.trim()).filter(Boolean),
    });
  }
  if (data) {
    owners.push({
      id: "data",
      team: "engineering",
      units: data[1].split(/,\s*/).map((s) => s.trim()).filter(Boolean),
    });
  }
  return { schema: "skailr.ownership/v1", owners };
}

function findCollisions(owners) {
  const errors = [];
  for (let i = 0; i < owners.length; i++) {
    for (let j = i + 1; j < owners.length; j++) {
      for (const ga of owners[i].units) {
        for (const gb of owners[j].units) {
          if (ga === gb || minimatch(ga, gb) || minimatch(gb, ga)) {
            errors.push(`Ownership overlap: ${owners[i].id} (${ga}) vs ${owners[j].id} (${gb})`);
          }
        }
      }
    }
  }
  return errors;
}

function main() {
  const args = parseArgs(process.argv);
  let map;
  if (args.fromSpec) {
    if (!existsSync(args.fromSpec)) {
      console.error(`Spec not found: ${args.fromSpec}`);
      process.exit(2);
    }
    map = ownershipFromSpec(args.fromSpec);
  } else {
    const mapPath = resolve(args.map);
    if (!existsSync(mapPath)) {
      // Soft-ok when no program ownership map yet
      console.log(`No ownership map at ${args.map}; skip.`);
      process.exit(0);
    }
    map = JSON.parse(readFileSync(mapPath, "utf8"));
  }

  if (!map.owners?.length) {
    console.error("Ownership map has no owners");
    process.exit(1);
  }

  // The built-in matcher supports only literal prefixes, "*", and "**". Braces/?/[]
  // would be treated as literals — overlaps missed, real paths reported unowned — so
  // reject unsupported syntax loudly instead of silently misreading the map.
  const UNSUPPORTED_GLOB = /[{}?[\]]/;
  const badGlobs = [];
  for (const o of map.owners) {
    for (const g of o.units || []) if (UNSUPPORTED_GLOB.test(g)) badGlobs.push(`${o.id}: ${g}`);
  }
  for (const g of map.kernel?.globs || []) if (UNSUPPORTED_GLOB.test(g)) badGlobs.push(`kernel: ${g}`);
  if (badGlobs.length) {
    console.error(
      "Unsupported glob syntax (supported: literal prefixes, \"*\", \"**\"):\n" +
        badGlobs.map((b) => `  ${b}`).join("\n"),
    );
    process.exit(1);
  }

  const collisions = findCollisions(map.owners);
  if (collisions.length) {
    console.error(collisions.join("\n"));
    process.exit(1);
  }

  if (args.mapOnly) {
    console.log(`OK: ownership map valid (${map.owners.length} owners); map-only`);
    process.exit(0);
  }

  let paths = [];
  if (args.pathsFile) {
    const raw = args.pathsFile === "-" ? readFileSync(0, "utf8") : readFileSync(args.pathsFile, "utf8");
    paths = raw.split("\n").map((l) => l.trim()).filter(Boolean);
  } else {
    const base = args.base || map.baseRef || "HEAD";
    try {
      const out = execFileSync("git", ["diff", "--name-only", base], { encoding: "utf8" });
      paths = out.split("\n").map((l) => l.trim()).filter(Boolean);
    } catch {
      try {
        const out = execFileSync("git", ["diff", "--name-only"], { encoding: "utf8" });
        paths = out.split("\n").map((l) => l.trim()).filter(Boolean);
      } catch {
        // Fail closed: a gate that cannot observe the diff must not report success.
        console.error(
          `ERROR: could not obtain changed paths from git (base=${base}); ` +
            "refusing to pass with 0 paths. Supply paths via --paths-file if git is unavailable.",
        );
        process.exit(1);
      }
    }
  }

  const kernelGlobs = map.kernel?.globs || [];
  const kernelFrozen = !!map.kernel?.frozen;
  const violations = [];

  for (const path of paths) {
    if (path.startsWith(".claude/")) continue;
    const matching = map.owners.filter((o) => o.units.some((g) => minimatch(path, g)));
    if (kernelFrozen && kernelGlobs.some((g) => minimatch(path, g))) {
      violations.push(`${path}: kernel_violation`);
      continue;
    }
    if (matching.length === 0) {
      if (kernelGlobs.some((g) => minimatch(path, g))) continue;
      violations.push(`${path}: unowned`);
    } else if (matching.length > 1) {
      violations.push(`${path}: collision [${matching.map((m) => m.id).join(",")}]`);
    }
  }

  if (violations.length) {
    console.error("Ownership violations:\n" + violations.join("\n"));
    process.exit(1);
  }

  console.log(`OK: ownership map valid; ${paths.length} path(s) checked`);
}

main();
