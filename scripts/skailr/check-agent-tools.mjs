#!/usr/bin/env node
/**
 * check-agent-tools.mjs — Claude Code tool allowlist hygiene.
 *
 * 1. Write without Edit forces full-file rewrites
 *    ("No Edit tool available; rewriting the full file…").
 * 2. Commands that can Read should also have Grep and Glob so orchestrators
 *    do not fall back to Bash find/rg for routine repo search.
 *
 * Usage: node scripts/skailr/check-agent-tools.mjs
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, resolve } from "node:path";

const ROOT = resolve(process.cwd());

function walkMd(dir, out = []) {
  if (!statSync(dir, { throwIfNoEntry: false })?.isDirectory()) return out;
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    const st = statSync(p);
    if (st.isDirectory()) walkMd(p, out);
    else if (name.endsWith(".md")) out.push(p);
  }
  return out;
}

function toolsFromFrontmatter(text, key) {
  if (!text.startsWith("---")) return null;
  const end = text.indexOf("\n---", 3);
  if (end === -1) return null;
  const prefix = `${key}:`;
  for (const line of text.slice(3, end).split("\n")) {
    const trimmed = line.trim();
    if (!trimmed.startsWith(prefix)) continue;
    const rest = trimmed.slice(prefix.length).trim();
    return rest
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
  }
  return null;
}

function checkWriteImpliesEdit(path, key, tools) {
  if (tools.includes("Write") && !tools.includes("Edit")) {
    return `${path}: ${key} lists Write without Edit (${tools.join(", ")})`;
  }
  return null;
}

function checkCommandSearchTools(path, tools) {
  if (!tools.includes("Read")) return null;
  const missing = ["Grep", "Glob"].filter((t) => !tools.includes(t));
  if (missing.length) {
    return `${path}: allowed-tools has Read but missing ${missing.join(", ")} (${tools.join(", ")})`;
  }
  return null;
}

const failures = [];

for (const path of walkMd(join(ROOT, ".claude", "agents"))) {
  const tools = toolsFromFrontmatter(readFileSync(path, "utf8"), "tools");
  if (!tools) continue;
  const err = checkWriteImpliesEdit(path, "tools", tools);
  if (err) failures.push(err);
}

for (const path of walkMd(join(ROOT, ".claude", "commands"))) {
  const tools = toolsFromFrontmatter(readFileSync(path, "utf8"), "allowed-tools");
  if (!tools) continue;
  const errEdit = checkWriteImpliesEdit(path, "allowed-tools", tools);
  if (errEdit) failures.push(errEdit);
  const errSearch = checkCommandSearchTools(path, tools);
  if (errSearch) failures.push(errSearch);
}

if (failures.length) {
  console.error("check-agent-tools: allowlist gaps:\n");
  for (const f of failures) console.error(`  ${f}`);
  process.exit(1);
}

const agentCount = walkMd(join(ROOT, ".claude", "agents")).length;
const cmdCount = walkMd(join(ROOT, ".claude", "commands")).length;
console.log(
  `OK: Write⇒Edit on agents/commands; Read⇒Grep+Glob on commands (${agentCount} agent(s), ${cmdCount} command(s))`,
);
