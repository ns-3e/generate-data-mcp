#!/usr/bin/env node
/**
 * cleanup-scoped.mjs — purge allowlisted build caches inside the current agent
 * worktree, or retire that worktree after a successful complete run.
 *
 * Usage:
 *   node scripts/skailr/cleanup-scoped.mjs dry-run
 *   node scripts/skailr/cleanup-scoped.mjs purge
 *   node scripts/skailr/cleanup-scoped.mjs retire
 *
 * Optional env:
 *   CLEANUP_ROOT=<dir>  subdirectory inside the agent worktree (purge/dry-run only)
 *
 * Shared main checkouts: purge/retire are no-op success (exit 0).
 * Paths outside own agent worktree: refuse (exit 1).
 */
import {
  existsSync,
  readdirSync,
  rmSync,
  statSync,
  realpathSync,
} from "node:fs";
import { join, resolve, relative } from "node:path";
import { execFileSync } from "node:child_process";

const ALLOWLIST = new Set([
  "target",
  "node_modules",
  ".venv",
  "venv",
  ".next",
  "dist",
  "build",
  "coverage",
  "__pycache__",
  ".pytest_cache",
  ".turbo",
  ".vite",
]);

/** Do not descend when looking for nested caches (env trees are deleted wholesale). */
const SKIP_DESCEND = new Set([
  ".git",
  ".venv",
  "venv",
  "node_modules",
  "target",
]);

const DENY_NAMES = new Set([
  ".git",
  ".env",
  "data",
  "tmp",
  "program",
  "repo",
  "experts",
  "channels",
  "contracts",
  "tickets",
  "handoff",
]);

const USAGE = `Usage:
  node scripts/skailr/cleanup-scoped.mjs dry-run
  node scripts/skailr/cleanup-scoped.mjs purge
  node scripts/skailr/cleanup-scoped.mjs retire

Optional: CLEANUP_ROOT=<dir> under the current agent worktree (purge/dry-run).`;

function die(msg, code = 1) {
  console.error(`cleanup-scoped: ${msg}`);
  process.exit(code);
}

function git(args, cwd) {
  return execFileSync("git", args, {
    cwd,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  }).trim();
}

function normalize(p) {
  return resolve(p).replace(/\\/g, "/");
}

function isInside(child, parent) {
  const c = normalize(child);
  const p = normalize(parent);
  if (c === p) return true;
  const prefix = p.endsWith("/") ? p : p + "/";
  return c.startsWith(prefix);
}

/** Match toplevel exactly equal to .../.claude/worktrees/<leaf>. */
function parseAgentWorktree(toplevel) {
  const n = normalize(toplevel);
  const marker = "/.claude/worktrees/";
  const idx = n.lastIndexOf(marker);
  if (idx === -1) return null;
  const after = n.slice(idx + marker.length);
  if (!after || after.includes("/")) return null;
  return { root: n, id: after };
}

function resolveOwnRoot() {
  let toplevel;
  try {
    toplevel = git(["rev-parse", "--show-toplevel"], process.cwd());
  } catch {
    die("not inside a git work tree");
  }
  const agent = parseAgentWorktree(toplevel);
  return { toplevel: normalize(toplevel), agent };
}

function assertCleanupRoot(agentRoot, cleanupRoot) {
  const abs = normalize(cleanupRoot);
  if (!isInside(abs, agentRoot)) {
    die(`CLEANUP_ROOT escapes agent worktree: ${abs} (root ${agentRoot})`);
  }
  if (!existsSync(abs) || !statSync(abs).isDirectory()) {
    die(`CLEANUP_ROOT is not a directory: ${abs}`);
  }
  // Refuse if CLEANUP_ROOT is a denied skailr / data path under the worktree
  const rel = relative(agentRoot, abs).replace(/\\/g, "/");
  const parts = rel.split("/").filter(Boolean);
  for (const part of parts) {
    if (part === ".claude") continue;
    if (DENY_NAMES.has(part) || part.startsWith(".env")) {
      die(`CLEANUP_ROOT hits deny path segment '${part}': ${abs}`);
    }
  }
  return abs;
}

function pathDenied(absPath, agentRoot) {
  const rel = relative(agentRoot, absPath).replace(/\\/g, "/");
  if (rel.startsWith("..")) return "escapes worktree";
  const parts = rel.split("/").filter(Boolean);
  for (let i = 0; i < parts.length; i++) {
    const part = parts[i];
    if (part === ".git") return ".git";
    if (part.startsWith(".env")) return ".env*";
    // Deny product data at worktree root only (allowlist never includes data/)
    if (i === 0 && part === "data") return "data/";
    // Deny skailr artifact trees
    if (part === ".claude" && parts[i + 1]) {
      const next = parts[i + 1];
      if (
        next === "tmp" ||
        next === "program" ||
        next === "repo" ||
        next === "experts"
      ) {
        return `.claude/${next}`;
      }
    }
  }
  return null;
}

/** Collect allowlisted cache dirs under scanRoot (recursive), skipping deny subtrees. */
function collectTargets(scanRoot, agentRoot) {
  const found = [];
  const stack = [scanRoot];
  while (stack.length) {
    const dir = stack.pop();
    let entries;
    try {
      entries = readdirSync(dir, { withFileTypes: true });
    } catch {
      continue;
    }
    for (const ent of entries) {
      if (!ent.isDirectory()) continue;
      const name = ent.name;
      const full = join(dir, name);
      const abs = normalize(full);

      if (!isInside(abs, agentRoot)) continue;

      const denied = pathDenied(abs, agentRoot);
      if (denied) {
        // Do not descend into .git / artifact roots / data
        if (
          name === ".git" ||
          name.startsWith(".env") ||
          (name === "data" && normalize(dir) === normalize(agentRoot)) ||
          (name === "tmp" &&
            normalize(dir).endsWith("/.claude")) ||
          (name === "program" &&
            normalize(dir).endsWith("/.claude")) ||
          (name === "repo" && normalize(dir).endsWith("/.claude")) ||
          (name === "experts" && normalize(dir).endsWith("/.claude"))
        ) {
          continue;
        }
      }

      if (ALLOWLIST.has(name)) {
        const why = pathDenied(abs, agentRoot);
        if (why) {
          console.error(
            `cleanup-scoped: skip ${abs} (deny: ${why})`,
          );
          continue;
        }
        found.push(abs);
        // Do not descend into a cache dir we will delete
        continue;
      }

      // Descend; skip heavy/irrelevant trees by name
      if (SKIP_DESCEND.has(name)) continue;
      stack.push(full);
    }
  }
  return found.sort();
}

function humanSize(dir) {
  // Best-effort; skip on error
  try {
    const out = execFileSync("du", ["-sh", dir], {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
    }).trim();
    return out.split(/\s+/)[0] || "?";
  } catch {
    return "?";
  }
}

function doPurge(dryRun) {
  const { toplevel, agent } = resolveOwnRoot();
  if (!agent) {
    console.log(
      `cleanup-scoped: not an agent worktree (${toplevel}); purge no-op`,
    );
    return;
  }

  let scanRoot = agent.root;
  if (process.env.CLEANUP_ROOT) {
    scanRoot = assertCleanupRoot(agent.root, process.env.CLEANUP_ROOT);
  }

  const targets = collectTargets(scanRoot, agent.root);
  if (!targets.length) {
    console.log(
      `cleanup-scoped: no allowlisted caches under ${scanRoot}`,
    );
    return;
  }

  for (const t of targets) {
    const size = humanSize(t);
    if (dryRun) {
      console.log(`cleanup-scoped: would delete [${size}] ${t}`);
    } else {
      console.log(`cleanup-scoped: deleting [${size}] ${t}`);
      rmSync(t, { recursive: true, force: true });
    }
  }
  console.log(
    `cleanup-scoped: ${dryRun ? "dry-run " : ""}purge done (${targets.length} path(s)) worktree=${agent.id}`,
  );
}

function doRetire(dryRun) {
  if (process.env.CLEANUP_ROOT) {
    die("CLEANUP_ROOT is not valid with retire (whole worktree only)");
  }
  const { toplevel, agent } = resolveOwnRoot();
  if (!agent) {
    console.log(
      `cleanup-scoped: not an agent worktree (${toplevel}); retire no-op`,
    );
    return;
  }

  // Safety: cwd must be inside this worktree
  const cwd = normalize(process.cwd());
  if (!isInside(cwd, agent.root)) {
    die(`cwd ${cwd} is outside own worktree ${agent.root}`);
  }

  // Refuse if this somehow looks like the main repo (no worktrees marker)
  // Already gated by parseAgentWorktree.

  const removePath = agent.root;

  if (dryRun) {
    console.log(
      `cleanup-scoped: would retire worktree ${agent.id} at ${removePath}`,
    );
    return;
  }

  // Prefer running from main repo so remove does not fail on cwd-inside-tree.
  let commonDir;
  try {
    commonDir = git(["rev-parse", "--git-common-dir"], agent.root);
  } catch (e) {
    die(`cannot resolve git-common-dir: ${e.message || e}`);
  }
  const commonAbs = normalize(
    commonDir.startsWith("/") ? commonDir : join(agent.root, commonDir),
  );
  let mainCheckout = agent.root;
  if (commonAbs.includes("/.git")) {
    mainCheckout = commonAbs.slice(0, commonAbs.indexOf("/.git"));
  }

  try {
    git(["worktree", "remove", "--force", removePath], mainCheckout);
  } catch (e) {
    try {
      git(["worktree", "remove", "--force", removePath], agent.root);
    } catch (e2) {
      die(`git worktree remove failed: ${e2.stderr || e2.message || e2}`);
    }
  }
  console.log(`cleanup-scoped: retired worktree ${agent.id} (${removePath})`);
}

function main() {
  const mode = process.argv[2];
  if (!mode || !["dry-run", "purge", "retire"].includes(mode)) {
    console.error(USAGE);
    process.exit(2);
  }

  // Resolve realpaths early for clearer errors
  try {
    process.chdir(realpathSync(process.cwd()));
  } catch {
    /* keep cwd */
  }

  if (mode === "dry-run") {
    // dry-run shows purge candidates; also note retire eligibility
    doPurge(true);
    const { agent } = resolveOwnRoot();
    if (agent) {
      console.log(
        `cleanup-scoped: retire eligible for worktree ${agent.id}`,
      );
    }
    return;
  }
  if (mode === "purge") {
    doPurge(false);
    return;
  }
  if (mode === "retire") {
    doRetire(false);
  }
}

main();
