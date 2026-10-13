"""Read and maintain the durable workspace registry state.

``<dshHome>/storages/workspace.json`` is a JSON domain document::

    { "unit":   { "name": "workspace", "version": N },
      "global": { "workspaceIds": [...], "archivedSessionIds": [...], ... },
      "tables": { "workspaces": { "<id>": { "path", "title", "sessionIds", ... } } } }

Two fields decide whether a session appears under a workspace:

* the workspace's ``path``, which the session's ``cwd`` must resolve to, and
* the workspace's ``sessionIds``, the persisted ownership list.

**Membership needs both, and the second one is easy to miss.** DSH derives the
visible list from the stored one on every read
(``dsh-workspace/lib/index.js:102-104``)::

    get sessionIds() {
        return this.record.sessionIds.filter(
            (id) => this.host.sessionPath(id) === this.record.path);
    }

That getter only *filters* the stored list; it never adds to it. A session whose
``cwd`` resolves to a workspace is still hidden unless that workspace's
``sessionIds`` already lists it. Every write path in DSH maintains the list,
``attachSession`` prepending the id (``:111-129``) and ``detachSession``
removing it (``:148-153``), and startup adopts only ids that no workspace
accounts for at all (``bootstrap``, ``:784`` and ``:801``). A session left out
of every list is therefore visible in no workspace, and DSH reports it only as a
warning (``reportFilteredCandidates``, ``:900-910``)::

    workspace '<id>' filtered session '<id>' from membership:
    canonical cwd '<x>' differs from workspace path '<y>'

Relocating a session's artifacts without maintaining this document is what makes
a moved session disappear, so a move maintains it: the id is removed from every
record that still owns it and prepended to the target's, which is the state
``attachSession`` produces.

The document is written the way DSH writes it: ``JSON.stringify(document, null,
2)`` plus a newline (``dsh-storage-json/lib/index.js:72-84``), replaced
atomically through a temporary file in the same directory.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import time_ns
from typing import Any

from .errors import ToolError, quote

#: Suffix for the copy taken before the registry document is replaced. Matches
#: the marker the artifact backups use, so ``--keep-backups`` reasoning applies.
REGISTRY_BACKUP_MARKER = ".bak-"


def canonical(path: str | Path) -> str:
    """Return a comparison key for a filesystem path.

    DSH compares a *normalized* session ``cwd`` against the workspace record's
    path verbatim (``dsh-workspace/lib/index.js:114-122``). On Windows that
    makes the comparison sensitive to separator style, symlinks and letter case
    in ways that surprise people, so this project compares with :func:`os.path.realpath`
    plus a case fold on case-insensitive platforms.

    A path that does not exist is returned normalized-but-unresolved rather than
    being treated as an error; callers decide what a missing path means.
    """
    expanded = os.path.expanduser(str(path))
    try:
        resolved = os.path.realpath(expanded, strict=True)
    except OSError:
        resolved = os.path.normpath(os.path.abspath(expanded))
    if os.name == "nt":
        resolved = os.path.normcase(resolved)
    return resolved.rstrip("\\/") or resolved


@dataclass(frozen=True, slots=True)
class Workspace:
    """One registered workspace."""

    workspace_id: str
    path: str
    title: str

    @property
    def key(self) -> str:
        return canonical(self.path)


@dataclass(frozen=True, slots=True)
class OwnershipEdit:
    """The registry edit that files a session under a workspace.

    ``detach_from`` and ``attach`` mirror DSH's ``detachSession`` and
    ``attachSession``: the id leaves every record that still owns it and is
    prepended to the target's list.
    """

    session_id: str
    target: Workspace
    detach_from: tuple[Workspace, ...]
    attach: bool

    @property
    def needed(self) -> bool:
        """Whether the document has to be written at all."""
        return self.attach or bool(self.detach_from)

    def describe(self) -> str:
        """One line naming what changes, for a dry run or a log."""
        parts = [f"detach from {quote(workspace.title)}" for workspace in self.detach_from]
        if self.attach:
            parts.append(f"attach to {quote(self.target.title)}")
        return ", ".join(parts) if parts else "already accounted for"


@dataclass(frozen=True, slots=True)
class OwnershipResult:
    """What writing the registry changed."""

    changed: tuple[str, ...]
    backup: Path | None


@dataclass(frozen=True, slots=True)
class Registry:
    """The parts of the workspace registry this tool needs."""

    workspaces: tuple[Workspace, ...]
    archived_session_ids: frozenset[str]
    owners: dict[str, tuple[str, ...]]
    source: Path

    def by_key(self, key: str) -> Workspace | None:
        for workspace in self.workspaces:
            if workspace.key == key:
                return workspace
        return None

    def by_id(self, workspace_id: str) -> Workspace | None:
        for workspace in self.workspaces:
            if workspace.workspace_id == workspace_id:
                return workspace
        return None

    @property
    def home(self) -> Path:
        """The DSH home this document belongs to, derived from its own location."""
        return self.source.parent.parent

    def find(self, path: str) -> Workspace | None:
        """Look a workspace up by a user-supplied path."""
        return self.by_key(canonical(path))

    def resolve_cwd(self, cwd: str) -> Workspace | None:
        """Map a session's stored ``cwd`` to a workspace, or ``None``.

        Falls back to a case-insensitive comparison so a session that DSH itself
        cannot re-attach is still attributed to the right workspace in reports,
        rather than showing up as belonging to nothing.
        """
        workspace = self.by_key(canonical(cwd))
        if workspace is not None:
            return workspace
        folded = canonical(cwd)
        for candidate in self.workspaces:
            if candidate.key.casefold() == folded.casefold():
                return candidate
        return None

    def holders(self, session_id: str) -> tuple[Workspace, ...]:
        """Every workspace whose stored ``sessionIds`` lists ``session_id``.

        This is the raw ownership list, before DSH's membership filter, so two
        holders mean DSH refuses to start (``validateStoredState``, ``:849-853``)
        and no holders means the session shows up nowhere.
        """
        found: list[Workspace] = []
        for workspace_id, session_ids in self.owners.items():
            if session_id in session_ids:
                workspace = self.by_id(workspace_id)
                if workspace is not None:
                    found.append(workspace)
        return tuple(found)

    def is_archived(self, session_id: str) -> bool:
        return session_id in self.archived_session_ids


def workspace_state_path(dsh_home: Path) -> Path:
    return dsh_home / "storages" / "workspace.json"


def load_document(source: Path) -> dict[str, Any]:
    """Read the registry document as it is, keeping every field.

    Raises:
        ToolError: when the document is missing or not the shape expected.
    """
    if not source.is_file():
        raise ToolError(f"workspace state not found: {source}")
    try:
        document: Any = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ToolError(f"cannot read {source}: {error}") from error
    if not isinstance(document, dict):
        raise ToolError(f"{source}: top level is not an object")
    return document


def rows_of(document: dict[str, Any], source: Path) -> dict[str, Any]:
    """Return the ``tables.workspaces`` map, or raise."""
    table = document.get("tables")
    if not isinstance(table, dict):
        raise ToolError(f"{source}: 'tables' is missing or not an object")
    rows = table.get("workspaces")
    if not isinstance(rows, dict):
        raise ToolError(f"{source}: 'tables.workspaces' is missing or not an object")
    return rows


def load_registry(dsh_home: Path) -> Registry:
    """Load the workspace registry document.

    Raises:
        ToolError: when the document is missing or does not have the shape this
            tool expects. A registry we cannot read is never guessed at, because
            every path decision below depends on it.
    """
    source = workspace_state_path(dsh_home)
    document = load_document(source)
    rows = rows_of(document, source)

    workspaces: list[Workspace] = []
    owners: dict[str, tuple[str, ...]] = {}
    for workspace_id, entry in rows.items():
        if not isinstance(entry, dict):
            continue
        raw_ids = entry.get("sessionIds")
        owners[workspace_id] = (
            tuple(item for item in raw_ids if isinstance(item, str)) if isinstance(raw_ids, list) else ()
        )
        path = entry.get("path")
        if not isinstance(path, str) or not path:
            continue
        title = entry.get("title")
        workspaces.append(
            Workspace(
                workspace_id=workspace_id,
                path=path,
                title=title if isinstance(title, str) and title else Path(path).name,
            )
        )

    global_state = document.get("global")
    archived: set[str] = set()
    if isinstance(global_state, dict):
        raw_archived = global_state.get("archivedSessionIds")
        if isinstance(raw_archived, list):
            archived = {item for item in raw_archived if isinstance(item, str)}

    workspaces.sort(key=lambda workspace: workspace.title.casefold())
    return Registry(
        workspaces=tuple(workspaces),
        archived_session_ids=frozenset(archived),
        owners=owners,
        source=source,
    )


def plan_ownership(registry: Registry, session_id: str, target: Workspace) -> OwnershipEdit:
    """Work out what the registry needs so DSH files ``session_id`` under ``target``.

    Pure: it reads the already-loaded registry and reports the edit, so a dry run
    can show the change without touching the document.
    """
    holders = registry.holders(session_id)
    target_ids = registry.owners.get(target.workspace_id, ())
    return OwnershipEdit(
        session_id=session_id,
        target=target,
        detach_from=tuple(
            workspace for workspace in holders if workspace.workspace_id != target.workspace_id
        ),
        attach=session_id not in target_ids,
    )


def _timestamp() -> str:
    """An ISO stamp shaped like JavaScript's ``Date.toISOString()``."""
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def apply_ownership(dsh_home: Path, edit: OwnershipEdit) -> OwnershipResult:
    """Write the registry so ``edit`` holds, keeping the rest of the document.

    The document is re-read here rather than reused from a plan, and the target
    record's path is re-checked, so a registry that changed in between is
    detected instead of overwritten with stale assumptions.

    Raises:
        ToolError: when the edit cannot be applied or the result does not verify.
    """
    if not edit.needed:
        return OwnershipResult(changed=(), backup=None)

    source = workspace_state_path(dsh_home)
    document = load_document(source)
    rows = rows_of(document, source)

    entry = rows.get(edit.target.workspace_id)
    if not isinstance(entry, dict):
        raise ToolError(
            f"{source}: workspace {edit.target.workspace_id} disappeared; re-run after checking DSH"
        )
    if entry.get("path") != edit.target.path:
        raise ToolError(
            f"{source}: workspace {quote(edit.target.title)} now has path {quote(entry.get('path'))}, "
            f"not {quote(edit.target.path)}; refusing to file the session by a stale path"
        )

    changed: list[str] = []
    for workspace_id, record in rows.items():
        if not isinstance(record, dict):
            continue
        raw_ids = record.get("sessionIds")
        existing = [item for item in raw_ids if isinstance(item, str)] if isinstance(raw_ids, list) else []
        if workspace_id == edit.target.workspace_id:
            if not edit.attach:
                continue
            updated = [edit.session_id, *(item for item in existing if item != edit.session_id)]
        else:
            if edit.session_id not in existing:
                continue
            updated = [item for item in existing if item != edit.session_id]
        record["sessionIds"] = updated
        record["updatedAt"] = _timestamp()
        changed.append(workspace_id)

    if not changed:
        return OwnershipResult(changed=(), backup=None)

    serialized = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    backup = source.with_name(f"{source.name}{REGISTRY_BACKUP_MARKER}{time_ns() // 1_000_000}")
    temporary = source.with_name(f"{source.name}.move-tmp")
    try:
        shutil.copy2(source, backup)
        temporary.write_text(serialized, encoding="utf-8")
        os.replace(temporary, source)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise ToolError(f"could not update {source}: {error}") from error

    _verify_ownership(source, edit)
    return OwnershipResult(changed=tuple(changed), backup=backup)


def _verify_ownership(source: Path, edit: OwnershipEdit) -> None:
    """Re-read the document and assert the edit took effect.

    Raises:
        ToolError: when the written document does not say what was intended, so a
            caller can never report a repaired session on faith.
    """
    try:
        rows = rows_of(load_document(source), source)
    except ToolError as error:
        raise ToolError(f"{source}: written registry cannot be read back: {error}") from error

    target_ids = rows.get(edit.target.workspace_id, {}).get("sessionIds")
    if not isinstance(target_ids, list) or edit.session_id not in target_ids:
        raise ToolError(f"{source}: {edit.session_id} is still not accounted by {quote(edit.target.title)}")
    for workspace in edit.detach_from:
        other = rows.get(workspace.workspace_id, {}).get("sessionIds")
        if isinstance(other, list) and edit.session_id in other:
            raise ToolError(f"{source}: {edit.session_id} is still accounted by {quote(workspace.title)}")


def reclaim_registry_backup(backup: Path | None) -> None:
    """Remove a registry backup once the change it covers has been verified."""
    if backup is None:
        return
    with contextlib.suppress(OSError):
        backup.unlink(missing_ok=True)
