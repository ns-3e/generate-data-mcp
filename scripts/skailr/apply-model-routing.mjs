#!/usr/bin/env node
/**
 * apply-model-routing.mjs — apply or check agent model: frontmatter from profiles.
 * Usage:
 *   node scripts/skailr/apply-model-routing.mjs [--profile economy|balanced|quality] [--check] [--root .]
 */
import { readFileSync, writeFileSync, readdirSync, existsSync, statSync } from "node:fs";
import { join, resolve, basename } from "node:path";

const VALID_PROFILES = new Set(["economy", "balanced", "quality"]);

function parseArgs(argv) {
  let root = ".";
  let profile = null;
  let check = false;
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--check") check = true;
    else if (a === "--profile") {
      profile = argv[++i];
      if (!profile || !VALID_PROFILES.has(profile)) {
        console.error(
          `Invalid --profile. Expected one of: ${[...VALID_PROFILES].join(", ")}`
        );
        process.exit(2);
      }
    } else if (a === "--root") {
      root = argv[++i];
      if (!root) {
        console.error("--root requires a path");
        process.exit(2);
      }
    } else if (a === "--help" || a === "-h") {
      console.log(
        "Usage: node scripts/skailr/apply-model-routing.mjs [--profile economy|balanced|quality] [--check] [--root .]"
      );
      process.exit(0);
    } else {
      console.error(`Unknown argument: ${a}`);
      process.exit(2);
    }
  }
  return { root: resolve(root), profile, check };
}

function listAgentFiles(agentsRoot) {
  const files = [];
  if (!existsSync(agentsRoot)) return files;
  for (const name of readdirSync(agentsRoot)) {
    const p = join(agentsRoot, name);
    let st;
    try {
      st = statSync(p);
    } catch {
      continue;
    }
    if (st.isFile() && name.endsWith(".md")) {
      throw new Error(
        `flat agent files are not allowed under .claude/agents/; move ${name} into a subdirectory`
      );
    }
    if (!st.isDirectory()) continue;
    for (const file of readdirSync(p)) {
      if (file.endsWith(".md")) files.push(join(p, file));
    }
  }
  return files.sort();
}

function parseFrontmatter(text) {
  if (!text.startsWith("---")) return { fm: {}, body: text, end: -1 };
  const end = text.indexOf("\n---", 3);
  if (end === -1) return { fm: {}, body: text, end: -1 };
  const fm = {};
  for (const line of text.slice(3, end).trim().split("\n")) {
    const i = line.indexOf(":");
    if (i === -1) continue;
    fm[line.slice(0, i).trim()] = line.slice(i + 1).trim();
  }
  const body = text.slice(end + 4).replace(/^\n/, "");
  return { fm, body, end };
}

function resolveModel(profile, roleName) {
  return profile.roles[roleName] ?? profile.default ?? "sonnet";
}

function setModelInFrontmatter(text, model) {
  const { end } = parseFrontmatter(text);
  if (end === -1) {
    throw new Error("missing YAML frontmatter");
  }
  const fmBlock = text.slice(0, end + 4); // through closing ---
  const body = text.slice(end + 4);
  let nextFm;
  if (/^model:\s*.+$/m.test(fmBlock)) {
    nextFm = fmBlock.replace(/^model:\s*.+$/m, `model: ${model}`);
  } else {
    // Insert before closing ---
    nextFm = fmBlock.replace(/\n---\s*$/, `\nmodel: ${model}\n---`);
  }
  return nextFm + body;
}

function main() {
  const { root, profile: profileArg, check } = parseArgs(process.argv.slice(2));
  const configPath = join(root, ".claude", "model-routing.json");
  if (!existsSync(configPath)) {
    console.error(`Missing config: ${configPath}`);
    process.exit(1);
  }

  const config = JSON.parse(readFileSync(configPath, "utf8"));
  if (profileArg) {
    if (!config.profiles?.[profileArg]) {
      console.error(`Profile not found in config: ${profileArg}`);
      process.exit(1);
    }
    config.active = profileArg;
    if (!check) {
      writeFileSync(configPath, JSON.stringify(config, null, 2) + "\n");
      console.log(`Active profile set to: ${profileArg}`);
    }
  }

  const active = config.active;
  const profile = config.profiles?.[active];
  if (!profile) {
    console.error(`Active profile "${active}" missing from profiles`);
    process.exit(1);
  }

  const agentsRoot = join(root, ".claude", "agents");
  const files = listAgentFiles(agentsRoot);
  if (files.length === 0) {
    console.error(`No agent files under ${agentsRoot}`);
    process.exit(1);
  }

  const rows = [];
  let mismatches = 0;
  let updated = 0;

  for (const file of files) {
    const text = readFileSync(file, "utf8");
    const { fm } = parseFrontmatter(text);
    const role = fm.name || basename(file, ".md");
    const expected = resolveModel(profile, role);
    const current = fm.model || "";
    const ok = current === expected;
    rows.push({ role, current: current || "(none)", expected, ok });
    if (!ok) mismatches++;

    if (!check && !ok) {
      const next = setModelInFrontmatter(text, expected);
      writeFileSync(file, next);
      updated++;
    }
  }

  // Ensure every role in the profile exists as an agent, and every agent is in the profile
  const roleSet = new Set(rows.map((r) => r.role));
  const missingInFs = Object.keys(profile.roles || {}).filter((r) => !roleSet.has(r));
  const missingInProfile = rows
    .map((r) => r.role)
    .filter((r) => profile.roles && !(r in profile.roles));

  console.log(`Profile: ${active}${check ? " (check)" : ""}`);
  console.log("Role".padEnd(28) + "Current".padEnd(12) + "Expected".padEnd(12) + "Status");
  for (const r of rows) {
    console.log(
      r.role.padEnd(28) +
        r.current.padEnd(12) +
        r.expected.padEnd(12) +
        (r.ok ? "ok" : "MISMATCH")
    );
  }

  if (missingInFs.length) {
    console.error(`\nRoles in profile but no agent file: ${missingInFs.join(", ")}`);
    mismatches += missingInFs.length;
  }
  if (missingInProfile.length) {
    console.error(
      `\nAgents missing from profile.roles (will use default "${profile.default}"): ${missingInProfile.join(", ")}`
    );
  }

  if (check) {
    if (mismatches > 0) {
      console.error(`\nFAIL: ${mismatches} mismatch(es). Run without --check to apply.`);
      process.exit(1);
    }
    console.log(`\nOK: ${rows.length} agents match profile "${active}".`);
    return;
  }

  console.log(
    `\nApplied profile "${active}": ${updated} file(s) updated, ${rows.length - updated} already matched.`
  );
  if (profileArg) {
    console.log(
      "If you maintain this pack, run ./scripts/remirror.sh so Cursor mirrors stay in sync."
    );
  }
}

main();
