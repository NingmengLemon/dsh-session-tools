# dsh-session-tools

Typed tooling for DeepSeek Harness session artifacts: inspect them, verify them,
and **move a session between workspaces**.

The move is one operation: rewrite the `cwd` field inside the stored session
header. Nothing is deleted, no directory is created, no log body is re-encoded,
and `workspace.json` is never touched.

## Why rewriting one header field is the whole job

Two facts about DSH 0.2.0-rc.2, both read from the shipping source rather than
assumed, make the registry irrelevant to a move.

**A session's workspace is derived from its own header, not from a registry list.**
`dsh-workspace/lib/index.js:102-104`:

```js
get sessionIds() {
    return this.record.sessionIds.filter(
        (id) => this.host.sessionPath(id) === this.record.path);
}
```

The per-workspace `sessionIds` array is a cache DSH re-filters on every read.
Editing it would be redundant, and trusting it would be wrong. Attaching a
session validates only the header's `cwd`: it must resolve, be a directory, and
equal the workspace path (`dsh-workspace/lib/index.js:114-122`).

**The artifact's location is itself derived from `cwd` + `id`.**
`dsh-session-persistence-jsonl/lib/index.js:914-927`:

```js
function sessionDir(root, cwd, id) { return join(projectDir(root, cwd), encodeSegment(id)); }
function generationLogPath(root, cwd, id, version, compression) {
    return join(sessionDir(root, cwd, id), generationLogFilename(version, compression));
}
```

So the header *is* the membership. Change the header and the session reports as
belonging to the new workspace, leaving the artifact where it is. Old entries in
the previous workspace's cached list become inert, exactly as a deleted
session's leftovers do.

## Why only the first frame is touched

A session artifact is a **concatenation of independently decodable zstd frames**,
and DSH's reader depends on that structure:

* the first frame must contain exactly the header line — `assertZstdHeaderFrame`,
  `dsh-session-persistence-jsonl/lib/index.js:2293-2295`
* each later frame carries one durable event batch
* every frame is written with the checksum flag (`CHECKSUM_OPTIONS`, `:1291`)

That is why this tool replaces the first frame and copies every remaining frame
as raw bytes. It is also why re-encoding the whole log into one frame is a defect
rather than a shortcut: a single frame holding header plus events fails the
first-frame contract, and DSH then refuses to read the log.

A related trap this tool avoids on purpose: **`version` is never re-stamped.**
Writing the current format version into an older log's header makes DSH classify
it as malformed (`released v2 physical header lacks isSeeded`) and drop it from
`persistence.list()`, so the session silently disappears.

## Install

```powershell
uv sync
```

## Use

```powershell
# 1. Verify every artifact decodes frame-by-frame and satisfies DSH's contract.
uv run dsh-sessions verify

# 2. List the registry and every cwd group.
uv run dsh-sessions list
uv run dsh-sessions list --from "E:\Projects\Python\UniversalWorkspace"

# 3. See what a move would do. Nothing is written.
uv run dsh-sessions move --session <id> --to "<target workspace path>" --dry-run

# 4. Do it. Refuses while a DSH process is running unless --force.
uv run dsh-sessions move --session <id> --to "<target workspace path>"

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
* Each artifact is copied to a `.bak-<epoch-ms>` sibling before it is replaced.
* Replacement goes through a temporary file and an atomic rename, so an
  interrupted run cannot leave a half-written log.
* After writing, the session is re-read and the stored `cwd` is asserted to be
  the target. Success is never reported on faith.
* Nothing is ever deleted, and no directory is ever created.

### Backups accumulate

Every move leaves one `.bak-<epoch-ms>` file per artifact, and nothing removes
them. Moving a hundred sessions leaves a hundred backups, each the size of the
log it came from. DSH ignores these files — they are not canonical artifact
names, so they are never read or counted as sessions — but they do occupy disk.

Reclaim the space once you are satisfied the moves are correct:

```powershell
# List them first.
Get-ChildItem "$env:USERPROFILE\.dsh\sessions" -Recurse -File -Filter "*.bak-*" |
    Measure-Object -Property Length -Sum

# Then remove them. This is the only destructive command in this project's docs.
Get-ChildItem "$env:USERPROFILE\.dsh\sessions" -Recurse -File -Filter "*.bak-*" |
    Remove-Item -WhatIf
```

Drop `-WhatIf` to actually delete. An interrupted write can also leave a
`*.move-tmp` file; those are safe to remove the same way.

## Layout

```
src/dsh_session_tools/
  frames.py       the concatenated-zstd container: scan, decode, rebuild
  paths.py        DSH's two on-disk encodings (projectKey, encodeSegment)
  sessions.py     discovery under <dshHome>/sessions
  workspaces.py   the workspace registry document, and path canonicalisation
  operations.py   plan a move, apply it, verify it
  cli.py          the command line
```

## Development

```powershell
uv run ruff check .
uv run ruff format --check .
uvx ty check
uv run pytest
```

## Scope and limits

Read this before trusting it with a library you care about.

* **This implementation was validated by byte-comparison against an
  independently written Node implementation** of the same operation, on a real
  artifact: identical SHA-256, and the restore path reproduced the original bytes
  exactly. The test suite covers the container contract, the encodings, and the
  refusal paths.
* **It has not been exercised against a live DSH session.** Close DSH before
  moving anything. The liveness check is a heuristic and is deliberately biased
  toward false positives, so a "DSH looks alive" warning may appear when it is.
* **Sessions whose generations disagree are reported, not repaired.** Run
  `doctor` to find them.
* **Unregistered cwds are reported, not reconciled.** Sessions under a path that
  is not a registered workspace show as ungrouped in DSH; this tool will not move
  them *into* such a path, and it reports them so you can decide.
* The frame-boundary scan locates frames by the zstd magic number. A compressed
  payload could in principle contain those four bytes and split one frame into
  two bogus ranges; that is not silent corruption, because every range is
  decompressed and a bogus split fails JSON validation. The approach was checked
  against all 1000 artifacts (782,720 frames) in the development library.
