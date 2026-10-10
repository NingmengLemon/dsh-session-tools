// Drive the shipped DSH session store and report what it actually accepts.
//
// This exists because the tool's own premise was wrong in two places that a
// self-consistent fixture could not catch: DSH enforces that an artifact sits
// where its header's cwd says, and it resolves that cwd before comparing it to
// the stored workspace path. Both were found by asking the real backend, so the
// real backend is what this checks against.
//
// Usage: node dsh_contract.mjs <dshInstallRoot> <sessionsRoot>
// Prints one JSON object on stdout.

import { pathToFileURL } from "node:url";
import { join } from "node:path";

const [dshRoot, sessionsRoot] = process.argv.slice(2);
if (!dshRoot || !sessionsRoot) {
  console.error("usage: dsh_contract.mjs <dshInstallRoot> <sessionsRoot>");
  process.exit(2);
}

const modules = join(dshRoot, "node_modules", "@deepseek-ai");
const load = (relative) => import(pathToFileURL(join(modules, relative)).href);

const report = { dshRoot, sessionsRoot, listed: [], loaded: {}, errors: [] };

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

console.log(JSON.stringify(report));
