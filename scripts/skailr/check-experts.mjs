#!/usr/bin/env node
/**
 * check-experts.mjs — validate the expert roster and regenerate its routing registry.
 *
 * DOC: Public CLI. Contract: .claude/program/contracts/expert-validation-gate.md v1.
 *   node scripts/skailr/check-experts.mjs [--dir <path>] [--regen-registry] [--slug <slug>] [--strict] [--help]
 *   Exit 0 = valid or nothing to check. Exit 1 = validation errors. Exit 2 = bad usage.
 *   A MISSING roster directory exits 0 with a skip message: a fresh install that has never
 *   minted must never fail a gate for not having experts.
 *
 * DOC: Operational. Safe to wire as a `.claude/settings.skailr.json` Stop hook, but only
 *   WITHOUT --strict, so a soft roster_cap warning never fails an ordinary turn.
 *
 * DOC: Explicitly NOT checked: route_when band overlap between two experts, or between an
 *   expert and a team. Textual overlap is not mechanically decidable (brief A11). Nothing
 *   here protects a caller from misrouting or double dispatch.
 *
 * Zero npm dependencies, matching every other script in scripts/skailr/. The YAML
 * frontmatter parser and the JSON Schema walker below are deliberately small subsets,
 * sized to the two kernel sidecars in .claude/program/schemas/ and nothing more.
 */
import { readFileSync, writeFileSync, existsSync, readdirSync, statSync } from "node:fs";
import { execSync } from "node:child_process";
import { join, resolve, dirname, basename } from "node:path";

const PROFILE_SCHEMA = "expert.schema.json";
const CONFIG_SCHEMA = "expert-config.schema.json";
const RESERVED_ROLE_NAMES = ["expert", "expert-scout"];
const REQUIRED_SECTIONS = [
  "Band",
  "Industry depth",
  "Repo depth",
  "Sources",
  "How I advise",
  "How I co-author",
  "How I gate",
  "Known limits",
];
const DEFAULT_ROSTER_CAP = 7;
const PROVISIONAL_STALE_DAYS = 90;
const SHORT_ROUTE_WHEN = 30;
const MINT_LOG_ANCHOR = "<!-- mint-log:append-below -->";

/* -------------------------------------------------------------------------- */
/* YAML frontmatter — indentation-based subset                                 */
/* -------------------------------------------------------------------------- */

class YamlError extends Error {}

/** Drop a trailing ` # comment`, respecting quotes so a URL fragment survives. */
function stripComment(line) {
  let quote = null;
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (quote) {
      if (c === quote) quote = null;
    } else if (c === '"' || c === "'") {
      quote = c;
    } else if (c === "#" && (i === 0 || line[i - 1] === " " || line[i - 1] === "\t")) {
      return line.slice(0, i);
    }
  }
  return line;
}

/**
 * Scalars are never coerced to numbers. The expert schema has no numeric field, and
 * YAML int-coercion would silently turn an all-digit `against_sha` into a number and
 * fail its `type: string` check for the wrong reason.
 */
function parseScalar(raw) {
  const v = raw.trim();
  if (v === "" || v === "null" || v === "~") return null;
  if (v === "true") return true;
  if (v === "false") return false;
  if (v.length >= 2 && ((v[0] === '"' && v.endsWith('"')) || (v[0] === "'" && v.endsWith("'")))) {
    return v.slice(1, -1);
  }
  if (v.startsWith("[") && v.endsWith("]")) {
    const inner = v.slice(1, -1).trim();
    if (!inner) return [];
    return splitInline(inner).map((s) => parseScalar(s));
  }
  return v;
}

/** Split `a, b, "c, d"` on top-level commas only. */
function splitInline(s) {
  const out = [];
  let cur = "";
  let quote = null;
  for (const c of s) {
    if (quote) {
      cur += c;
      if (c === quote) quote = null;
    } else if (c === '"' || c === "'") {
      quote = c;
      cur += c;
    } else if (c === ",") {
      out.push(cur);
      cur = "";
    } else {
      cur += c;
    }
  }
  if (cur.trim()) out.push(cur);
  return out.map((x) => x.trim()).filter((x) => x !== "");
}

function tokenizeYaml(text) {
  const lines = [];
  for (const raw of text.split("\n")) {
    if (raw.includes("\t") && /^\s*\t/.test(raw)) throw new YamlError("tab indentation is not valid YAML");
    const noComment = stripComment(raw);
    if (!noComment.trim()) continue;
    lines.push({
      indent: noComment.match(/^ */)[0].length,
      text: noComment.trimEnd().trim(),
      raw: noComment,
    });
  }
  return lines;
}

const KEY_RE = /^([^:\s][^:]*):(?:\s+(.*))?$/;

function parseNode(lines, i, indent) {
  if (i < lines.length && (lines[i].text === "-" || lines[i].text.startsWith("- "))) {
    return parseSeq(lines, i, indent);
  }
  return parseMap(lines, i, indent);
}

function parseMap(lines, i, indent) {
  const obj = {};
  while (i < lines.length) {
    const line = lines[i];
    if (line.indent < indent) break;
    if (line.indent > indent) throw new YamlError(`unexpected indentation at "${line.text}"`);
    if (line.text === "-" || line.text.startsWith("- ")) break;

    const m = line.text.match(KEY_RE);
    if (!m) throw new YamlError(`cannot parse line "${line.text}"`);
    const key = m[1].trim();
    const rest = (m[2] ?? "").trim();

    if (rest === "|" || rest === ">" || rest === "|-" || rest === ">-") {
      const fold = rest[0] === ">";
      const block = [];
      let j = i + 1;
      while (j < lines.length && lines[j].indent > indent) {
        block.push(lines[j].text);
        j++;
      }
      obj[key] = block.join(fold ? " " : "\n");
      i = j;
    } else if (rest === "") {
      const next = lines[i + 1];
      if (next && next.indent > indent) {
        const [child, ni] = parseNode(lines, i + 1, next.indent);
        obj[key] = child;
        i = ni;
      } else if (next && next.indent === indent && (next.text === "-" || next.text.startsWith("- "))) {
        const [child, ni] = parseSeq(lines, i + 1, indent);
        obj[key] = child;
        i = ni;
      } else {
        obj[key] = null;
        i++;
      }
    } else {
      obj[key] = parseScalar(rest);
      i++;
    }
  }
  return [obj, i];
}

function parseSeq(lines, i, indent) {
  const arr = [];
  while (i < lines.length && lines[i].indent === indent && (lines[i].text === "-" || lines[i].text.startsWith("- "))) {
    const line = lines[i];
    const content = line.text === "-" ? "" : line.text.slice(2).trim();

    if (content === "") {
      const next = lines[i + 1];
      if (next && next.indent > indent) {
        const [v, ni] = parseNode(lines, i + 1, next.indent);
        arr.push(v);
        i = ni;
      } else {
        arr.push(null);
        i++;
      }
    } else if (KEY_RE.test(content)) {
      // `- key: value` opens a mapping whose sibling keys align with the content column.
      const col = line.raw.indexOf("-") + 2;
      const sub = [{ indent: col, text: content, raw: line.raw }];
      let j = i + 1;
      while (j < lines.length && lines[j].indent >= col) {
        sub.push(lines[j]);
        j++;
      }
      const [v] = parseMap(sub, 0, col);
      arr.push(v);
      i = j;
    } else {
      arr.push(parseScalar(content));
      i++;
    }
  }
  return [arr, i];
}

/** Returns { frontmatter, body }. Throws YamlError when the block is absent or malformed. */
function splitFrontmatter(text) {
  const normalized = text.replace(/^\uFEFF/, "").replace(/\r\n/g, "\n");
  if (!normalized.startsWith("---\n") && normalized.trimStart().indexOf("---") !== 0) {
    throw new YamlError("no YAML frontmatter block");
  }
  const start = normalized.indexOf("---");
  const end = normalized.indexOf("\n---", start + 3);
  if (end === -1) throw new YamlError("unterminated YAML frontmatter block");
  const raw = normalized.slice(start + 3, end);
  const afterFence = normalized.indexOf("\n", end + 1);
  const body = afterFence === -1 ? "" : normalized.slice(afterFence + 1);
  const lines = tokenizeYaml(raw);
  if (!lines.length) throw new YamlError("empty YAML frontmatter block");
  const [fm] = parseMap(lines, 0, lines[0].indent);
  return { frontmatter: fm, body };
}

/* -------------------------------------------------------------------------- */
/* JSON Schema walker — hand-rolled subset of draft 2020-12                    */
/* -------------------------------------------------------------------------- */

function typeMatches(value, type) {
  switch (type) {
    case "object":
      return value !== null && typeof value === "object" && !Array.isArray(value);
    case "array":
      return Array.isArray(value);
    case "string":
      return typeof value === "string";
    case "integer":
      return Number.isInteger(value);
    case "number":
      return typeof value === "number" && Number.isFinite(value);
    case "boolean":
      return typeof value === "boolean";
    case "null":
      return value === null;
    default:
      return true;
  }
}

/**
 * Supports the keywords the kernel sidecars actually use: type, const, enum, required,
 * properties, items, pattern, minLength, minItems, minimum. Everything else is ignored
 * by design, matching the `$comment` in expert.schema.json.
 */
function walkSchema(value, schema, path, out) {
  if (!schema || typeof schema !== "object") return;

  if (schema.type !== undefined) {
    const types = Array.isArray(schema.type) ? schema.type : [schema.type];
    if (!types.some((t) => typeMatches(value, t))) {
      out.push({ path, keyword: "type", message: `expected ${types.join(" or ")}, got ${describe(value)}` });
      return;
    }
  }
  if (schema.const !== undefined && value !== schema.const) {
    out.push({ path, keyword: "const", message: `must be ${JSON.stringify(schema.const)}, got ${describe(value)}` });
    return;
  }
  if (schema.enum !== undefined && !schema.enum.includes(value)) {
    out.push({
      path,
      keyword: "enum",
      message: `must be one of ${schema.enum.map((e) => JSON.stringify(e)).join(", ")}, got ${describe(value)}`,
    });
    return;
  }
  if (typeof value === "string") {
    if (schema.minLength !== undefined && value.length < schema.minLength) {
      out.push({ path, keyword: "minLength", message: schema.minLength === 1 ? "must not be empty" : `must be at least ${schema.minLength} characters` });
    }
    if (schema.pattern !== undefined && !new RegExp(schema.pattern).test(value)) {
      out.push({ path, keyword: "pattern", message: `${JSON.stringify(value)} does not match ${schema.pattern}` });
    }
  }
  if (typeof value === "number") {
    if (schema.minimum !== undefined && value < schema.minimum) {
      out.push({ path, keyword: "minimum", message: `must be >= ${schema.minimum}, got ${value}` });
    }
  }
  if (Array.isArray(value)) {
    if (schema.minItems !== undefined && value.length < schema.minItems) {
      out.push({ path, keyword: "minItems", message: schema.minItems === 1 ? "must not be empty" : `must have at least ${schema.minItems} entries` });
    }
    if (schema.items) {
      value.forEach((v, idx) => walkSchema(v, schema.items, `${path}/${idx}`, out));
    }
  }
  if (value !== null && typeof value === "object" && !Array.isArray(value)) {
    for (const key of schema.required || []) {
      if (!(key in value) || value[key] === undefined) {
        out.push({ path: `${path}/${key}`, keyword: "required", message: `missing required field '${key}'` });
      }
    }
    for (const [key, sub] of Object.entries(schema.properties || {})) {
      if (key in value && value[key] !== undefined) walkSchema(value[key], sub, `${path}/${key}`, out);
    }
    if (schema.additionalProperties === false) {
      for (const key of Object.keys(value)) {
        if (!(schema.properties || {})[key]) {
          out.push({ path: `${path}/${key}`, keyword: "additionalProperties", message: `unknown key '${key}'` });
        }
      }
    }
  }
}

function describe(value) {
  if (value === null) return "null";
  if (Array.isArray(value)) return `an array of ${value.length}`;
  if (typeof value === "object") return "an object";
  return JSON.stringify(value);
}

/* -------------------------------------------------------------------------- */
/* Markdown body                                                               */
/* -------------------------------------------------------------------------- */

function splitSections(body) {
  const sections = new Map();
  let h1 = null;
  let cur = null;
  let fenced = false;
  for (const line of body.split("\n")) {
    if (/^\s*(```|~~~)/.test(line)) fenced = !fenced;
    if (!fenced) {
      const h2 = line.match(/^##\s+(.+?)\s*$/);
      if (h2) {
        cur = [];
        if (!sections.has(h2[1])) sections.set(h2[1], cur);
        else cur = sections.get(h2[1]);
        continue;
      }
      const top = line.match(/^#\s+(.+?)\s*$/);
      if (top) {
        if (h1 === null) h1 = top[1];
        cur = null;
        continue;
      }
      if (/^#{3,}\s/.test(line) && cur) {
        cur.push(line);
        continue;
      }
    }
    if (cur) cur.push(line);
  }
  return { h1, sections };
}

/** An HTML comment is generator scaffolding, not content. */
function sectionIsEmpty(lines) {
  return (lines || []).join("\n").replace(/<!--[\s\S]*?-->/g, "").trim() === "";
}

/* -------------------------------------------------------------------------- */
/* Helpers                                                                     */
/* -------------------------------------------------------------------------- */

function oneLine(value) {
  return String(value ?? "").replace(/\s+/g, " ").trim().replace(/\|/g, "\\|");
}

function projectRootFor(dir) {
  return basename(dirname(dir)) === ".claude" ? dirname(dirname(dir)) : dirname(dir);
}

function daysSince(iso) {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  return (Date.now() - t) / 86400000;
}

function packRoleNames(root) {
  const names = new Set(RESERVED_ROLE_NAMES);
  const agentsDir = join(root, ".claude", "agents");
  if (!existsSync(agentsDir)) return names;
  const walk = (d) => {
    for (const entry of readdirSync(d, { withFileTypes: true })) {
      const p = join(d, entry.name);
      if (entry.isDirectory()) walk(p);
      else if (entry.name.endsWith(".md")) names.add(entry.name.slice(0, -3));
    }
  };
  try {
    walk(agentsDir);
  } catch {
    /* an unreadable agents tree only shrinks the reserved list; slugs still need -expert */
  }
  return names;
}

function pathChangedSince(root, sha, ref) {
  try {
    const out = execSync(`git -C ${JSON.stringify(root)} diff --name-only ${JSON.stringify(sha)} -- ${JSON.stringify(ref)}`, {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
    });
    return out.trim() !== "";
  } catch {
    return false;
  }
}

/* -------------------------------------------------------------------------- */
/* Registry generation                                                         */
/* -------------------------------------------------------------------------- */

const REGISTRY_HEADER = `<!-- Generated by scripts/skailr/check-experts.mjs --regen-registry from
     .claude/experts/profiles/*.md — do not hand-edit. -->`;

// Copied verbatim from .claude/program/schemas/expert-registry.template.md. Embedded
// rather than read at runtime so generation is byte-deterministic in any working tree.
const REGISTRY_PREAMBLE = `This is the Tier-1 routing view of the expert roster: the thin file a caller reads to decide
whether any expert covers an ask, without loading a single profile.

- **Read the roster table only.** Do not open a profile to decide routing. Open a profile
  only after a band is selected.
- **A band matches** when the ask falls within exactly one row's \`route-when\`.
- **Zero matches and two or more matches both mean "no expert route."** The caller falls
  through to its normal behavior. Ambiguity is never resolved by guessing.
- **\`route-when\` overlap between experts, or between an expert and a team, is not
  mechanically checked.** No validator protects you here; textual band overlap is not
  decidable. Treat a suspicious near-match as no match.
- **A missing registry file means an empty roster.** This is the normal state of a fresh
  project and is never an error.
- **A \`provisional\` expert may advise and co-author but never blocks.** A \`deprecated\` expert
  never appears in this table at all.`;

function renderRegistry(profiles, existingText) {
  const listed = profiles
    .filter((p) => p.fm && p.fm.maturity !== "deprecated" && typeof p.fm.slug === "string")
    .sort((a, b) => (a.fm.slug < b.fm.slug ? -1 : a.fm.slug > b.fm.slug ? 1 : 0));

  const out = [];
  out.push(REGISTRY_HEADER, "", "# Expert Registry", "", "## How routing uses this", "", REGISTRY_PREAMBLE, "");
  out.push("## Roster", "");
  out.push("| slug | name | classification | maturity | gate | route-when |");
  out.push("|---|---|---|---|---|---|");
  for (const p of listed) {
    const f = p.fm;
    out.push(
      `| ${oneLine(f.slug)} | ${oneLine(f.name)} | ${oneLine(f.classification)} | ${oneLine(f.maturity)} | ${oneLine(f.gate)} | ${oneLine(f.route_when)} |`,
    );
  }
  if (!listed.length) out.push("", "_No experts minted yet._");
  out.push("", "## Depth index", "");
  if (!listed.length) {
    out.push("_No experts minted yet._", "");
  } else {
    for (const p of listed) {
      const f = p.fm;
      out.push(`### ${f.slug}`);
      out.push(`- industry: ${(f.depth?.industry || []).map((s) => oneLine(s)).join(", ")}`);
      out.push(`- repo: ${(f.depth?.repo || []).map((s) => oneLine(s)).join(", ")}`);
      out.push(`- profile: \`.claude/experts/profiles/${f.slug}.md\``);
      out.push("");
    }
  }
  out.push("## Mint and revision log", "");

  // The log is append-only and never regenerated: everything past the anchor is preserved
  // byte-for-byte, which is also what makes a repeat run byte-identical.
  let tail = "\n";
  if (existingText) {
    const anchor = existingText.indexOf(MINT_LOG_ANCHOR);
    if (anchor !== -1) {
      tail = existingText.slice(anchor + MINT_LOG_ANCHOR.length);
    } else {
      const heading = existingText.indexOf("## Mint and revision log");
      if (heading !== -1) tail = existingText.slice(heading + "## Mint and revision log".length).replace(/^\n+/, "\n");
    }
  }
  return out.join("\n") + MINT_LOG_ANCHOR + tail;
}

/* -------------------------------------------------------------------------- */
/* CLI                                                                         */
/* -------------------------------------------------------------------------- */

const USAGE = `Usage: node scripts/skailr/check-experts.mjs [options]

  --dir <path>          roster directory (default .claude/experts)
  --regen-registry      rewrite registry.md from profiles, then validate
  --slug <slug>         validate only this profile
  --strict              treat warnings as errors
  --help

Exit codes: 0 valid or nothing to check, 1 validation errors, 2 bad usage.`;

function parseArgs(argv) {
  const out = { dir: ".claude/experts", regen: false, slug: null, strict: false };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--dir" || a === "--slug") {
      const v = argv[++i];
      if (v === undefined || v.startsWith("--")) return { usageError: `${a} requires a value` };
      if (a === "--dir") out.dir = v;
      else out.slug = v;
    } else if (a === "--regen-registry") out.regen = true;
    else if (a === "--strict") out.strict = true;
    else if (a === "--help" || a === "-h") return { help: true };
    else return { usageError: `unknown argument: ${a}` };
  }
  return out;
}

/* -------------------------------------------------------------------------- */
/* Validation                                                                  */
/* -------------------------------------------------------------------------- */

/** Map a schema violation onto the numbered rule it demonstrates. */
function ruleForSchemaIssue(issue) {
  const p = issue.path;
  if (p === "/schema") return [1, "schema tag"];
  if (p === "/slug" && issue.keyword === "pattern") return [4, "slug pattern"];
  if (p === "/classification" || p === "/maturity" || p === "/gate") return [3, "enum"];
  if (p.startsWith("/depth")) return [8, "dual depth"];
  if (p.startsWith("/sources")) return [9, "sources"];
  return [2, "required field"];
}

function validateProfile(p, ctx, errors, warnings) {
  const file = p.rel;
  const push = (rule, name, detail) => errors.push({ file, slug: p.slugKey, text: `${file}: rule ${rule} (${name}): ${detail}` });

  if (p.parseError) {
    push(1, "frontmatter", p.parseError);
    return;
  }
  const fm = p.fm;

  const issues = [];
  walkSchema(fm, ctx.profileSchema, "", issues);
  for (const issue of issues) {
    const [rule, name] = ruleForSchemaIssue(issue);
    push(rule, name, `${issue.path || "/"}: ${issue.message}`);
  }

  const slug = typeof fm.slug === "string" ? fm.slug : null;

  // 5: slug equals the filename stem.
  if (slug && slug !== p.stem) push(5, "slug/filename", `slug '${slug}' does not match filename stem '${p.stem}'`);

  // 6: slug unique across profiles.
  if (slug && ctx.slugCounts.get(slug) > 1) push(6, "duplicate slug", `slug '${slug}' is declared by ${ctx.slugCounts.get(slug)} profiles`);

  // 7: slug does not collide with a reserved pack role name.
  if (slug && ctx.reserved.has(slug)) push(7, "reserved name", `slug '${slug}' collides with a reserved pack role name`);

  const sources = Array.isArray(fm.sources) ? fm.sources.filter((s) => s && typeof s === "object") : [];
  const kinds = sources.map((s) => s.kind);
  const hasInternal = kinds.includes("repo-path");
  const hasExternal = kinds.includes("url") || kinds.includes("doc");

  // 10: repo-path refs exist on disk.
  for (const s of sources) {
    if (s.kind === "repo-path" && typeof s.ref === "string" && s.ref) {
      if (!existsSync(resolve(ctx.root, s.ref))) push(10, "repo-path source", `'${s.ref}' does not exist on disk`);
    }
  }

  // 11 / 12 / 13 / 14: classification implications.
  if (fm.classification === "internal" && !hasInternal) {
    push(11, "internal sources", "classification 'internal' requires at least one repo-path source");
  }
  if ((fm.classification === "external" || fm.classification === "hybrid") && !hasExternal) {
    push(12, "external sources", `classification '${fm.classification}' requires at least one url or doc source`);
  }
  if ((fm.classification === "external" || fm.classification === "hybrid") && slug) {
    const research = join(ctx.dir, "research", `${slug}.md`);
    if (!existsSync(research)) {
      push(13, "research artifact", `classification '${fm.classification}' requires ${ctx.dirLabel}/research/${slug}.md`);
    }
  }
  if (fm.classification === "hybrid" && !hasInternal) {
    push(14, "hybrid sources", "classification 'hybrid' requires an internal source set (repo-path) as well as an external one");
  }

  // 15: an expert groundable only through Intair cannot function offline.
  if (sources.length && kinds.every((k) => k === "intair-node")) {
    push(15, "intair-only", "every source is kind 'intair-node'; an expert must be groundable without Intair");
  }

  // 16: hard gates are only for established experts.
  if (fm.gate === "hard" && fm.maturity !== "established") {
    push(16, "gate maturity", `gate 'hard' requires maturity 'established', got '${fm.maturity}'`);
  }

  // 17 / 18: body sections.
  const { h1, sections } = splitSections(p.body || "");
  if (!h1) push(18, "body section", "missing the body H1 heading");
  for (const title of REQUIRED_SECTIONS) {
    if (!sections.has(title)) push(18, "body section", `missing required section '## ${title}'`);
  }
  if (sections.has("Known limits") && sectionIsEmpty(sections.get("Known limits"))) {
    push(17, "known limits", "'## Known limits' is empty; an expert that claims no limits is a correctness failure");
  }

  // 19: supersedes names a real expert.
  if (typeof fm.supersedes === "string" && fm.supersedes && !ctx.slugCounts.has(fm.supersedes)) {
    push(19, "supersedes", `supersedes '${fm.supersedes}' names a slug that does not exist in the roster`);
  }

  /* Warnings */
  const warn = (name, detail) => warnings.push({ file, slug: p.slugKey, text: `${file}: warning (${name}): ${detail}` });

  if (typeof fm.route_when === "string" && fm.route_when.trim().length > 0 && fm.route_when.trim().length < SHORT_ROUTE_WHEN) {
    warn("vague band", `route_when is only ${fm.route_when.trim().length} characters; a vague band causes misrouting`);
  }
  const sha = fm.last_reviewed?.against_sha;
  if (typeof sha === "string" && sha && sha !== "unknown") {
    for (const s of sources) {
      if (s.kind === "repo-path" && typeof s.ref === "string" && s.ref && pathChangedSince(ctx.root, sha, s.ref)) {
        warn("stale source", `'${s.ref}' changed since last_reviewed.against_sha ${sha}`);
      }
    }
  }
  if (fm.maturity === "provisional" && typeof fm.minted?.at === "string") {
    const age = daysSince(fm.minted.at);
    if (age !== null && age > PROVISIONAL_STALE_DAYS) {
      warn("stale provisional", `provisional for ${Math.floor(age)} days, never promoted or retired`);
    }
  }
}

/* -------------------------------------------------------------------------- */
/* Main                                                                        */
/* -------------------------------------------------------------------------- */

function main() {
  const args = parseArgs(process.argv);
  if (args.help) {
    console.log(USAGE);
    return 0;
  }
  if (args.usageError) {
    console.error(`${args.usageError}\n\n${USAGE}`);
    return 2;
  }

  const dir = resolve(args.dir);
  if (!existsSync(dir)) {
    console.log(`No expert roster at ${args.dir}; skip.`);
    return 0;
  }
  let st;
  try {
    st = statSync(dir);
  } catch (e) {
    console.error(`Cannot read roster dir ${args.dir}: ${e.message}`);
    return 2;
  }
  if (!st.isDirectory()) {
    console.error(`Not a directory: ${args.dir}`);
    return 2;
  }

  const root = projectRootFor(dir);
  const schemaDir = join(root, ".claude", "program", "schemas");
  let profileSchema;
  let configSchema;
  try {
    profileSchema = JSON.parse(readFileSync(join(schemaDir, PROFILE_SCHEMA), "utf8"));
    configSchema = JSON.parse(readFileSync(join(schemaDir, CONFIG_SCHEMA), "utf8"));
  } catch (e) {
    console.error(`Cannot load kernel schemas from ${join(".claude", "program", "schemas")}: ${e.message}`);
    return 2;
  }

  const errors = [];
  const warnings = [];

  // 20: config.json against expert-config.schema.json. A missing config means all defaults.
  let config = {};
  const configPath = join(dir, "config.json");
  const configLabel = `${args.dir}/config.json`;
  if (existsSync(configPath)) {
    let raw;
    try {
      raw = readFileSync(configPath, "utf8");
    } catch (e) {
      console.error(`${configLabel}: unreadable: ${e.message}`);
      return 2;
    }
    try {
      config = JSON.parse(raw);
    } catch (e) {
      console.error(`${configLabel}: unparseable JSON: ${e.message}`);
      return 2;
    }
    const issues = [];
    walkSchema(config, configSchema, "", issues);
    for (const issue of issues) {
      errors.push({ file: configLabel, slug: null, text: `${configLabel}: rule 20 (config): ${issue.path || "/"}: ${issue.message}` });
    }
  }
  const rosterCap = Number.isInteger(config.roster_cap) && config.roster_cap >= 1 ? config.roster_cap : DEFAULT_ROSTER_CAP;

  const profilesDir = join(dir, "profiles");
  let files = [];
  if (existsSync(profilesDir) && statSync(profilesDir).isDirectory()) {
    files = readdirSync(profilesDir)
      .filter((f) => f.endsWith(".md"))
      .sort();
  }

  const profiles = files.map((f) => {
    const abs = join(profilesDir, f);
    const stem = f.slice(0, -3);
    const rec = { file: f, abs, stem, rel: `${args.dir}/profiles/${f}`, fm: null, body: "", parseError: null, slugKey: stem };
    let text;
    try {
      text = readFileSync(abs, "utf8");
    } catch (e) {
      rec.parseError = `unreadable: ${e.message}`;
      return rec;
    }
    try {
      const { frontmatter, body } = splitFrontmatter(text);
      rec.fm = frontmatter;
      rec.body = body;
      if (typeof frontmatter.slug === "string") rec.slugKey = frontmatter.slug;
    } catch (e) {
      rec.parseError = e instanceof YamlError ? e.message : `unparseable frontmatter: ${e.message}`;
    }
    return rec;
  });

  const slugCounts = new Map();
  for (const p of profiles) {
    if (p.fm && typeof p.fm.slug === "string") slugCounts.set(p.fm.slug, (slugCounts.get(p.fm.slug) || 0) + 1);
  }

  const ctx = {
    root,
    dir,
    dirLabel: args.dir,
    profileSchema,
    slugCounts,
    reserved: packRoleNames(root),
  };

  for (const p of profiles) validateProfile(p, ctx, errors, warnings);

  // 21: roster cap. Deprecated experts are retained for history and are not part of the
  // roster (expert-registry-format rule 2), so they do not count against the cap.
  const rosterSize = profiles.filter((p) => p.fm && p.fm.maturity !== "deprecated").length;
  if (rosterSize > rosterCap) {
    const detail = `roster has ${rosterSize} expert(s), above roster_cap ${rosterCap}; consider consolidating`;
    if (args.strict) errors.push({ file: configLabel, slug: null, text: `${configLabel}: rule 21 (roster cap): ${detail}` });
    else warnings.push({ file: configLabel, slug: null, text: `${configLabel}: warning (roster cap): ${detail}` });
  }

  let selected = profiles;
  if (args.slug) {
    selected = profiles.filter((p) => p.slugKey === args.slug || p.stem === args.slug);
    if (!selected.length) {
      console.error(`No profile for --slug ${args.slug} in ${args.dir}/profiles`);
      return 2;
    }
  }
  const inScope = (item) => !args.slug || item.slug === null || selected.some((p) => p.slugKey === item.slug);
  const reportedErrors = errors.filter(inScope);
  const reportedWarnings = warnings.filter(inScope);

  if (args.strict) {
    for (const w of reportedWarnings) reportedErrors.push({ ...w, text: w.text.replace(": warning (", ": strict warning (") });
    reportedWarnings.length = 0;
  }

  // Regeneration never runs on a failed roster, so a broken profile cannot corrupt the
  // routing view. The gate is the WHOLE roster even under --slug, because the registry is
  // a whole-roster projection.
  if (args.regen) {
    const blocking = args.strict ? errors.concat(warnings) : errors;
    if (blocking.length) {
      const lines = reportedErrors.map((e) => e.text);
      const outOfScope = blocking.length - reportedErrors.length;
      if (outOfScope > 0) lines.push(`${args.dir}/registry.md: not regenerated: ${outOfScope} error(s) elsewhere in the roster`);
      console.error(lines.join("\n"));
      for (const w of reportedWarnings) console.log(w.text);
      return 1;
    }
    const registryPath = join(dir, "registry.md");
    const existing = existsSync(registryPath) ? readFileSync(registryPath, "utf8") : null;
    const next = renderRegistry(profiles, existing);
    if (next !== existing) writeFileSync(registryPath, next, "utf8");
    console.log(`Regenerated ${args.dir}/registry.md`);
  }

  for (const w of reportedWarnings) console.log(w.text);
  if (reportedErrors.length) {
    console.error(reportedErrors.map((e) => e.text).join("\n"));
    return 1;
  }
  console.log(`OK: ${selected.length} expert(s) checked`);
  return 0;
}

process.exit(main());
