// Drive the shipped DSH session store and report what it actually accepts.
//
// This exists because the tool's own premise was wrong in three places that a
// self-consistent fixture could not catch: DSH enforces that an artifact sits
// where its header's cwd says, it resolves that cwd before comparing it to the
// stored workspace path, and a workspace shows a session only when its stored
// `sessionIds` list names that session. All three were found by asking the real
// backend, so the real backend is what this checks against.
//
// Usage: node dsh_contract.mjs <dshInstallRoot> <sessionsRoot>
// Prints one JSON object on stdout.

import { pathToFileURL } from "node:url";
import { dirname, join } from "node:path";
import { readFile, stat } from "node:fs/promises";

const [dshRoot, sessionsRoot] = process.argv.slice(2);
if (!dshRoot || !sessionsRoot) {
  console.error("usage: dsh_contract.mjs <dshInstallRoot> <sessionsRoot>");
  process.exit(2);
}

const modules = join(dshRoot, "node_modules", "@deepseek-ai");
const load = (relative) => import(pathToFileURL(join(modules, relative)).href);

const report = {
  dshRoot,
  sessionsRoot,
  listed: [],
  loaded: {},
  membership: {},
  invisible: [],
  notes: [],
  errors: [],
};

let backend;
try {
  const { default: JsonlSessionPersistence } = await load(
    "dsh-session-persistence-jsonl/lib/index.js",
  );
  const { Context } = await load("cordis/lib/index.js");
  const ctx = new Context();
  ctx.logger = Object.assign(() => {}, {
    debug: () => {},
    info: () => {},
    warn: () => {},
    error: () => {},
    extend: () => ctx.logger,
  });
  backend = new JsonlSessionPersistence(ctx, { root: sessionsRoot });
} catch (error) {
  report.errors.push(`could not construct the backend: ${error.message}`);
  console.log(JSON.stringify(report));
  process.exit(0);
}

// list() is the operation that a header-only rewrite breaks, and it breaks it
// for the whole store rather than for the one session.
try {
  const snapshots = await backend.list();
  report.listed = snapshots.map((snapshot) => ({
    id: snapshot.header.id,
    cwd: snapshot.header.cwd ?? null,
    version: snapshot.header.version,
    origin: snapshot.header.origin ?? null,
  }));
} catch (error) {
  report.errors.push(`list() threw: ${error.message}`);
  console.log(JSON.stringify(report));
  process.exit(0);
}

// open() is the other half: a session can be listed and still refuse to load.
for (const entry of report.listed) {
  try {
    const stored = await backend.requireStoredLog(entry.id);
    report.loaded[entry.id] = { ok: true, events: stored.events?.length ?? 0 };
  } catch (error) {
    report.loaded[entry.id] = { ok: false, error: error.message };
  }
}

// Workspace membership. DSH shows a session under a workspace only when both
// halves hold: the workspace's stored `sessionIds` names the session, and the
// session's resolved cwd equals that workspace's stored path
// (dsh-workspace/lib/index.js:102-104). The two-line rule is transcribed here
// because standing up the whole storage domain would mean composing a Cordis
// app; `realpathNormalize` below is DSH's own function, not a reimplementation.
try {
  const { realpathNormalize } = await load("dsh-workspace/lib/index.js");
  const registryPath = join(dirname(sessionsRoot), "storages", "workspace.json");
  const document = JSON.parse(await readFile(registryPath, "utf8"));
  const rows = document?.tables?.workspaces ?? {};

  const sessionPath = new Map();
  for (const entry of report.listed) {
    if (typeof entry.cwd !== "string") continue;
    try {
      const resolved = await realpathNormalize(entry.cwd);
      if ((await stat(resolved)).isDirectory()) sessionPath.set(entry.id, resolved);
    } catch {
      // An unresolvable cwd is recorded as invalid by DSH, so it matches nothing.
    }
  }

  // A subagent session is nested under its parent instead of being a workspace
  // member, and DSH never accounts one in a workspace's sessionIds: measured on
  // this machine, none of its subagent sessions appear in any record. Counting
  // them as invisible would turn a normal state into a false alarm.
  let subagents = 0;
  for (const entry of report.listed) {
    if (entry.origin === "subagent") {
      subagents += 1;
      continue;
    }
    const path = sessionPath.get(entry.id);
    const holders = [];
    for (const record of Object.values(rows)) {
      if (!Array.isArray(record?.sessionIds) || !record.sessionIds.includes(entry.id)) continue;
      if (path !== undefined && path === record.path) holders.push(record.path);
    }
    report.membership[entry.id] = holders;
    if (holders.length === 0) report.invisible.push(entry.id);
  }
  if (subagents > 0) {
    report.notes.push(
      `${subagents} subagent session(s) are nested under their parent rather than listed in a workspace, so they are not counted as invisible`,
    );
  }
} catch (error) {
  report.notes.push(`workspace membership was not checked: ${error.message}`);
}

console.log(JSON.stringify(report));
