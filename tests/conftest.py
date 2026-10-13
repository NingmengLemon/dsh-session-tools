"""Shared fixtures and helpers: a throwaway DSH home that looks structurally real."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
import zstandard

from dsh_session_tools.paths import encode_segment, project_key
from dsh_session_tools.workspaces import Registry, Workspace


def write_artifact(
    directory: Path,
    *,
    filename: str,
    header: dict[str, object],
    lines: list[str] | None = None,
    compressed: bool = True,
) -> Path:
    """Write one session artifact the way DSH lays it out.

    Compressed artifacts get one zstd frame per line, which is the container
    shape DSH requires: the header alone in the first frame.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    body = [json.dumps(header, separators=(",", ":"))] + (lines or [])

    if not compressed:
        path.write_text("\n".join(body) + "\n", encoding="utf-8")
        return path

    compressor = zstandard.ZstdCompressor(write_checksum=True)
    path.write_bytes(b"".join(compressor.compress((line + "\n").encode()) for line in body))
    return path


def title_event(title: str, *, seq: int) -> str:
    """One `session/title` event line, shaped the way DSH appends it."""
    event = {
        "seq": seq,
        "type": "session/title",
        "data": {"title": title, "messageSeqs": [], "source": {"kind": "user"}},
    }
    return json.dumps(event, separators=(",", ":"))


class FakeHome:
    """A temporary DSH home plus helpers for populating it."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def sessions_root(self) -> Path:
        return self.root / "sessions"

    @property
    def workspace_state(self) -> Path:
        return self.root / "storages" / "workspace.json"

    def workspace_dir(self, name: str) -> Path:
        """Create a real directory usable as a workspace path."""
        target = self.root / "workspaces" / name
        target.mkdir(parents=True, exist_ok=True)
        return target

    def session(
        self,
        *,
        session_id: str,
        cwd: str,
        version: int = 4,
        title: str | None = None,
        origin: str | None = None,
        lines: list[str] | None = None,
        filename: str | None = None,
        extra_headers: dict[str, object] | None = None,
    ) -> Path:
        header: dict[str, object] = {
            "type": "session",
            "version": version,
            "id": session_id,
            "createdAt": 1791115958273,
            "isSeeded": False,
            "delegationDepth": 0,
            "agentPreset": "standard-no-web",
            "cwd": cwd,
        }
        if origin is not None:
            header["origin"] = origin
        if extra_headers:
            header.update(extra_headers)

        body = lines if lines is not None else ['{"seq":1,"type":"turn/start"}']
        if title is not None:
            # DSH keeps a title as a session/title event in the log, never in the header.
            body = [*body, title_event(title, seq=len(body) + 2)]

        directory = self.sessions_root / project_key(cwd) / encode_segment(session_id)
        return write_artifact(
            directory,
            filename=filename or f"session.v{version}.jsonl.zstd",
            header=header,
            lines=body,
        )

    def write_registry(
        self,
        workspaces: list[tuple[str, str]],
        *,
        archived: list[str] | None = None,
    ) -> None:
        """Write a workspace domain document from ``(id, path)`` pairs."""
        table = {
            workspace_id: {
                "path": path,
                "title": Path(path).name,
                "sessionIds": [],
                "createdAt": "2026-01-01T00:00:00.000Z",
                "updatedAt": "2026-01-01T00:00:00.000Z",
            }
            for workspace_id, path in workspaces
        }
        document = {
            "unit": {"name": "workspace", "version": 1},
            "global": {
                "initialized": True,
                "workspaceIds": [workspace_id for workspace_id, _ in workspaces],
                "archivedSessionIds": archived or [],
                "pinnedSessionIds": [],
            },
            "tables": {"workspaces": table},
        }
        self.workspace_state.parent.mkdir(parents=True, exist_ok=True)
        self.workspace_state.write_text(json.dumps(document, indent=2), encoding="utf-8")


@pytest.fixture
def home(tmp_path: Path) -> Iterator[FakeHome]:
    fake = FakeHome(root=tmp_path / "dsh-home")
    fake.sessions_root.mkdir(parents=True, exist_ok=True)
    yield fake


def workspace_at(registry: Registry, path: Path) -> Workspace:
    """Return the registered workspace for ``path`` or fail the test.

    Tests read better without a ``None`` check on every lookup, and a missing
    workspace is a broken fixture rather than a behaviour under test.
    """
    found = registry.find(str(path))
    assert found is not None, f"fixture did not register a workspace at {path}"
    return found
