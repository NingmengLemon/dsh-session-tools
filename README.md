# dsh-session-tools

Typed tooling for DeepSeek Harness session artifacts: inspect them, verify them,
and **move a session between workspaces**.

A move is two coupled changes, and DSH rejects the result if only one is made:
the artifact is relocated into the project directory the new workspace names,
**and** the `cwd` in its stored header is rewritten to that workspace. Neither
half is sufficient on its own.

## Why both halves are required

**The artifact's location is derived from `cwd` + `id`, and that derivation is
enforced on every read.** `dsh-session-persistence-jsonl/lib/index.js:914-927`
derives the path:

```js
function sessionDir(root, cwd, id) { return join(projectDir(root, cwd), encodeSegment(id)); }
function generationLogPath(root, cwd, id, version, compression) {
    return join(sessionDir(root, cwd, id), generationLogFilename(version, compression));
}
```

and `:3383-3395` asserts it:

```js
expectedPath = generationLogPath(this.root, meta.cwd, meta.id, storedVersion, this.compression);
if (path !== expectedPath && !await this.sameFile(path, expectedPath, signal))
    throw new Error(`corrupt session log "${path}": header id "${meta.id}" and cwd identify "${expectedPath}"`);
```

That assertion runs from `readGenerationHeader` (`:3091`), so it fires on every
`list()` and `stat()`, and from `decodeStoredLog` (`:2820`), so it fires on every
`open()`. It raises a plain `Error`, and `listArtifacts` swallows only the two
typed errors (`:3043`), so it escapes `list()` into the workspace registry, the
ACP session list and the session query.

The consequence is out of proportion to the change: rewriting a header without
relocating the artifact does not move one session, it makes the whole store
unreadable, because `list()` throws rather than skipping the offending entry.

**A session's workspace comes from its header, not from a registry list.** So
`workspace.json` still never needs editing. `dsh-workspace/lib/index.js:102-104`:

```js
get sessionIds() {
    return this.record.sessionIds.filter(
        (id) => this.host.sessionPath(id) === this.record.path);
}
```

`indexHeader` (`:872-890`) resolves the header's `cwd` and compares the result
against the stored workspace path verbatim. That is why the `cwd` written here is
the registry's path exactly as it is spelled, never a canonicalised version of
it, and why a workspace whose path resolves elsewhere is refused: writing its
spelling would file the session under no workspace at all, with only a warning
logged.

## Why only the first frame is touched

A session artifact is a **concatenation of independently decodable zstd frames**,
and DSH's reader depends on that structure:

* the first frame must contain exactly the header line — `assertZstdHeaderFrame`,
  `dsh-session-persistence-jsonl/lib/index.js:2293-2295`
* each later frame carries one durable event batch
* every frame is written with the checksum flag (`CHECKSUM_OPTIONS`, `:1291`)

So a rewrite replaces the first frame and copies every remaining frame as raw
bytes. Re-encoding the whole log into one frame is a defect rather than a
shortcut: a single frame holding header plus events fails the first-frame
contract and DSH refuses the log.

Frame boundaries are found by walking each frame's own structure, the way DSH
does (`scanZstdFrames`, `:1300-1363`). Searching for the 4-byte magic number and
trusting every hit is unsound, because those bytes occur inside compressed
payloads and split one frame in two; that was observed on real artifacts here,
not merely in theory.

A related trap this tool avoids on purpose: **`version` is never re-stamped.**
Writing the current format version into an older log's header makes DSH classify
it as malformed and drop it from `persistence.list()`, so the session silently
disappears.

## Install

```powershell
uv sync
```

## Use

```powershell
# 1. Ask the installed DSH itself whether it accepts the store, and whether this
#    tool sees the same sessions. This is the check worth trusting; it drives the
#    real backend rather than reimplementing its rules.
uv run dsh-sessions contract

# 2. The same check against a throwaway store this tool builds and moves, using a
#    real artifact as the subject. Nothing in your library is touched.
uv run dsh-sessions contract --self-test

# 3. Verify every artifact decodes frame-by-frame and every line is JSON.
uv run dsh-sessions verify

# 4. List the registry and every cwd group. `--from` shows each session's title,
#    read from its log, its creation time, and its id, newest first.
uv run dsh-sessions list
uv run dsh-sessions list --from "E:\Projects\Python\UniversalWorkspace"

# 5. See what a move would do. Nothing is written. --session takes a full id, a
#    unique id prefix, or a fragment of the title; an ambiguous one is refused and
#    lists the candidates.
uv run dsh-sessions move --session <id-or-title> --to "<target workspace path>" --dry-run

# 6. Do it. Refuses while a DSH process is running unless --force.
uv run dsh-sessions move --session <id-or-title> --to "<target workspace path>"

# Undo one artifact from its backup.
uv run dsh-sessions restore "<...>/session.v4.jsonl.zstd.bak-1791596004428"

# Diagnose: directory/header mismatches, unregistered cwds, archive accounting.
uv run dsh-sessions doctor
```

`--dsh-home PATH` overrides `$DSH_HOME`, which overrides `~/.dsh`.

## What it refuses

A refusal prints a reason and exits 1; it is never reported as success.

| Condition | Why |
|---|---|
| Header and location already disagree | DSH calls such a log corrupt and `list()` throws for the whole store |
| Target workspace path resolves elsewhere | DSH resolves the cwd and compares it to the unresolved stored path, so the session would land in no workspace |
| Session is a subagent | moving one is almost always a mistake; `--allow-subagent` overrides |
| Session is archived | unarchive first; `--allow-archived` overrides |
| Target is not a registered workspace | the path must also be an existing directory |
| Generations disagree about `cwd` | we cannot tell which one DSH considers current, so we do not guess |
| A DSH process looks alive | a live DSH appends to logs; `--force` overrides |

A **damaged** log is a different outcome from a refusal: it raises, because "your
log is corrupt" is not a policy decision.

## Safety model

* Nothing is written until every artifact of the session has been read, validated
  and rebuilt in memory.
* Every payload is staged beside its destination before anything is replaced, so
  a failure while staging leaves the store untouched.
* Each replacement is preceded by a `.bak-<epoch-ms>` sibling copy and performed
  by renaming the staged file over the target, which is atomic on one volume.
* After writing, the session is re-read and both halves of the move are asserted:
  the stored `cwd` is the target, and the artifact sits in the project directory
  that `cwd` names. Success is never reported on faith.
* `--keep-backups` keeps the byte copies; without it they are removed once the
  destination has been verified. Files the tool did not create are reported and
  left alone.

### Reversing a move

`restore` places the backup's bytes where that file's own header says they
belong, and parks the copy that was current under a backup name. Writing the
bytes back where the backup sits would rebuild the corrupt state the refusal
above exists to prevent, and leaving the current copy in place would give DSH an
id in two project directories, which it also refuses
(`findLog`, `dsh-session-persistence-jsonl/lib/index.js:3370`).

## Layout

```
src/dsh_session_tools/
  frames.py          the concatenated-zstd container: walk, decode, rebuild
  paths.py           DSH's two on-disk encodings (projectKey, encodeSegment)
  sessions.py        discovery under <dshHome>/sessions
  workspaces.py      the workspace registry document, and path canonicalisation
  titles.py          session titles, read from the log's session/title events
  operations.py      plan a move, apply it, verify it
  contract.py        check this tool's view against the installed DSH
  dsh_contract.mjs   the node side of that check, driving the real backend
  cli.py             the command line
```

## Development

```powershell
uv run ruff check .
uv run ruff format --check .
uvx ty check
uv run pytest
uv run dsh-sessions contract --self-test
```

## Scope and limits

Read this before trusting it with a library you care about.

* **The move is verified against the shipped DSH backend.** `contract
  --self-test` copies a real artifact into a throwaway home, checks DSH accepts
  it, moves it with the real planner and applier, and checks DSH accepts it again
  with the same event count. Reintroducing the header-only rewrite makes that
  check report zero sessions after the move, which is the failure mode it exists
  to catch.
* **It has not been exercised against a live DSH session.** Close DSH before
  moving anything. The liveness check is a heuristic and is deliberately biased
  toward false positives, so a "DSH looks alive" warning may appear when it is.
* **Sessions whose generations disagree are reported, not repaired.** Run
  `doctor` to find them.
* **Unregistered cwds are reported, not reconciled.** Sessions under a path that
  is not a registered workspace show as ungrouped in DSH; this tool will not move
  them *into* such a path, and it reports them so you can decide.
* **`verify` parses every line as JSON, and that dominates its running time.** On
  this machine's library, 349 artifacts and 318,632 frames take about 76 seconds.
  A move does not pay that cost: it validates container integrity, which is what
  it depends on, and leaves line parsing to `verify`.
* The reports below are from the development library, and the numbers are
  measured rather than estimated: 233 session directories, 108 of which hold
  several retained generations, 349 artifacts, 601 MiB compressed, 318,632
  frames.
* **Titles cost a full read of the listed session.** DSH keeps a title as a
  `session/title` event in the log, not in the header, so `list --from` decodes
  each listed session's current artifact to find the last one. Listing 54
  sessions this way took about 2.3 seconds on this machine. A plain `list`
  reads headers only and pays nothing for titles. Selecting a session by title
  reads logs too, but only after the id stages find nothing.
* **Creation time comes from the header** (`createdAt`, epoch milliseconds). A
  value that is missing or out of range shows as `(no time)` and sorts last.
* **Six artifacts in the development library are listed by DSH but will not
  open**, as v0 logs whose descriptor version it no longer supports. They predate
  this tool; `contract` reports them separately so a real divergence is not
  buried among them.
