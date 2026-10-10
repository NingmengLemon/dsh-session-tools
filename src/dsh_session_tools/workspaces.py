"""Read the durable workspace registry state.

``<dshHome>/storages/workspace.json`` is a JSON domain document::

    { "global":  { "workspaceIds": [...], "archivedSessionIds": [...], ... },
      "tables":  { "workspaces": { "<id>": { "path", "title", "sessionIds", ... } } } }

Only two things in it matter to us:

* each workspace's ``path``, which is what a session's ``cwd`` must equal to
  belong to it, and
* ``global.archivedSessionIds``, so we can report whether a session is archived.

The per-workspace ``sessionIds`` array is deliberately ignored. DSH re-derives
it on every read::

    dsh-workspace/lib/index.js:102-104
        get sessionIds() {
            return this.record.sessionIds.filter(
                (id) => this.host.sessionPath(id) === this.record.path);
        }

so writing it would be redundant, and trusting it would be wrong.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ToolError


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
class Registry:
    """The parts of the workspace registry this tool needs."""

    workspaces: tuple[Workspace, ...]
    archived_session_ids: frozenset[str]
    source: Path

    def by_key(self, key: str) -> Workspace | None:
        for workspace in self.workspaces:
            if workspace.key == key:
                return workspace
        return None

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

    def is_archived(self, session_id: str) -> bool:
        return session_id in self.archived_session_ids


def workspace_state_path(dsh_home: Path) -> Path:
    return dsh_home / "storages" / "workspace.json"


def load_registry(dsh_home: Path) -> Registry:
    """Load the workspace registry document.

    Raises:
        ToolError: when the document is missing or does not have the shape this
            tool expects. A registry we cannot read is never guessed at, because
            every path decision below depends on it.
    """
    source = workspace_state_path(dsh_home)
    if not source.is_file():
        raise ToolError(f"workspace state not found: {source}")

    try:
        document: Any = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ToolError(f"cannot read {source}: {error}") from error

    if not isinstance(document, dict):
        raise ToolError(f"{source}: top level is not an object")

    table = document.get("tables")
    if not isinstance(table, dict):
        raise ToolError(f"{source}: 'tables' is missing or not an object")
    rows = table.get("workspaces")
    if not isinstance(rows, dict):
        raise ToolError(f"{source}: 'tables.workspaces' is missing or not an object")

    workspaces: list[Workspace] = []
    for workspace_id, entry in rows.items():
        if not isinstance(entry, dict):
            continue
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
        source=source,
    )
