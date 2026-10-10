"""The operations: inspect a session, and move it to another workspace.

Moving means one thing only — rewriting the ``cwd`` field inside the first frame
of each of the session's artifacts, then copying every remaining frame as raw
bytes. Nothing is deleted, no directory is created, and ``workspace.json`` is
never touched.

Why no registry edit is needed
-----------------------------
DSH derives a workspace's membership from each session's own location every time
it reads it (``dsh-workspace/lib/index.js:102-104``), and the artifact path is
itself derived from ``cwd`` + ``id``
(``dsh-session-persistence-jsonl/lib/index.js:914-927``). So the header *is* the
membership. The stored ``sessionIds`` arrays are a cache DSH re-filters on read;
a stale entry in the old workspace is inert, exactly as a deleted session's
leftovers are.

Deliberate conservatism
-----------------------
* A session whose generations disagree about ``cwd`` is refused rather than
  guessed at, because we cannot tell which one DSH considers current.
* ``version`` is carried through untouched. Re-stamping it to match a new
  filename is what makes DSH reject an older log as malformed, so this tool
  never does it.
* Subagent sessions and archived sessions need an explicit override, because
  both are cases where a move is usually a mistake.
* Nothing is written until every artifact of the session has been read,
  validated and rebuilt in memory.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from time import time_ns
from typing import TYPE_CHECKING

from .errors import ToolError
from .frames import ArtifactError, decode_header, replace_header_frame, scan_frames
from .paths import project_key
from .sessions import Artifact, Session

if TYPE_CHECKING:
    from .workspaces import Registry, Workspace


@dataclass(frozen=True, slots=True)
class PlannedChange:
    """One generation file, rebuilt in memory and ready to be written."""

    artifact: Artifact
    old_cwd: str | None
    new_cwd: str
    payload: bytes
    frames: int
    unchanged: bool


@dataclass(frozen=True, slots=True)
class MovePlan:
    """A validated, fully rebuilt move that has not touched the disk yet."""

    session: Session
    target: Workspace
    source: Workspace | None
    changes: tuple[PlannedChange, ...]

    @property
    def session_id(self) -> str:
        return self.session.session_id

    @property
    def is_noop(self) -> bool:
        return all(change.unchanged for change in self.changes)


@dataclass(frozen=True, slots=True)
class AppliedChange:
    """What one artifact looks like after the write."""

    artifact: Artifact
    old_cwd: str | None
    new_cwd: str
    bytes_before: int
    bytes_after: int
    frames: int
    backup: Path | None
    unchanged: bool


@dataclass(frozen=True, slots=True)
class MoveFailure:
    """A refusal, with the reason stated rather than a bare false."""

    session_id: str
    reason: str


PlanOutcome = MovePlan | MoveFailure


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise ArtifactError(f"cannot read {path}: {error}") from error


def plan_move(
    session: Session,
    target: Workspace,
    registry: Registry,
    *,
    allow_subagent: bool = False,
    allow_archived: bool = False,
) -> PlanOutcome:
    """Validate the move and rebuild every artifact in memory.

    Returns:
        A :class:`MovePlan`, or a :class:`MoveFailure` explaining the refusal.

    Raises:
        ArtifactError: when an artifact is malformed. A damaged log is a
            different problem from a refused move and is surfaced loudly.
    """
    target_path = target.path

    if session.is_subagent and not allow_subagent:
        return MoveFailure(
            session_id=session.session_id,
            reason="subagent session (pass --allow-subagent to move it anyway)",
        )
    if registry.is_archived(session.session_id) and not allow_archived:
        return MoveFailure(
            session_id=session.session_id,
            reason="session is archived (unarchive it first, or pass --allow-archived)",
        )

    stored_cwd = session.cwd
    if stored_cwd is None:
        return MoveFailure(
            session_id=session.session_id,
            reason="header carries no cwd, so DSH could not attach it to any workspace",
        )
    if not Path(target_path).is_dir():
        return MoveFailure(
            session_id=session.session_id,
            reason=f"target workspace path is not an existing directory: {target_path}",
        )

    artifacts = session.artifacts()
    if not artifacts:
        return MoveFailure(session_id=session.session_id, reason="no canonical artifact found")

    changes: list[PlannedChange] = []
    agreed_cwd: str | None = None

    for artifact in artifacts:
        raw = _read(artifact.path)
        header = decode_header(raw, label=artifact.filename)

        if header.get("id") != session.session_id:
            return MoveFailure(
                session_id=session.session_id,
                reason=f"{artifact.filename}: header id does not match the session",
            )

        artifact_cwd = header.get("cwd")
        if not isinstance(artifact_cwd, str):
            return MoveFailure(
                session_id=session.session_id,
                reason=f"{artifact.filename}: header carries no usable cwd",
            )

        # Each generation is read here rather than trusted from the scan, because
        # the scan only sees the authoritative one. A generation pointing somewhere
        # else means the logs disagree and DSH would open a different workspace
        # depending on which generation it selects, so we refuse instead of picking.
        if agreed_cwd is None:
            agreed_cwd = artifact_cwd
            if artifact_cwd not in (target_path, stored_cwd):
                return MoveFailure(
                    session_id=session.session_id,
                    reason=(
                        f"{artifact.filename} stores cwd {artifact_cwd!r}, matching neither the "
                        f"session's cwd {stored_cwd!r} nor the target; refusing to guess"
                    ),
                )
        elif artifact_cwd != agreed_cwd:
            return MoveFailure(
                session_id=session.session_id,
                reason=(
                    f"generations disagree: {artifacts[0].filename} says {agreed_cwd!r} while "
                    f"{artifact.filename} says {artifact_cwd!r}; refusing to guess"
                ),
            )

        frames = len(scan_frames(raw))
        if artifact_cwd == target_path:
            changes.append(
                PlannedChange(
                    artifact=artifact,
                    old_cwd=artifact_cwd,
                    new_cwd=target_path,
                    payload=raw,
                    frames=frames,
                    unchanged=True,
                )
            )
            continue

        # Rebuild with cwd replaced in place so the remaining keys keep their order.
        new_header: dict[str, object] = {
            key: (target_path if key == "cwd" else value) for key, value in header.items()
        }
        rebuilt, body = replace_header_frame(raw, new_header, label=artifact.filename)
        changes.append(
            PlannedChange(
                artifact=artifact,
                old_cwd=artifact_cwd if isinstance(artifact_cwd, str) else None,
                new_cwd=target_path,
                payload=rebuilt,
                frames=1 + len(body),
                unchanged=False,
            )
        )

    return MovePlan(
        session=session,
        target=target,
        source=registry.resolve_cwd(stored_cwd),
        changes=tuple(changes),
    )


def apply_plan(plan: MovePlan) -> tuple[AppliedChange, ...]:
    """Write a planned move to disk.

    Each artifact is copied to a ``.bak-<epoch-ms>`` sibling first, then replaced
    through a temporary file and an atomic rename, so an interrupted run cannot
    leave a half-written log.

    Raises:
        ToolError: when a write fails. Already-applied artifacts keep their
            backups, so the state is recoverable with ``restore``.
    """
    applied: list[AppliedChange] = []
    for change in plan.changes:
        path = change.artifact.path
        frames = change.frames
        if change.unchanged:
            applied.append(
                AppliedChange(
                    artifact=change.artifact,
                    old_cwd=change.old_cwd,
                    new_cwd=change.new_cwd,
                    bytes_before=len(change.payload),
                    bytes_after=len(change.payload),
                    frames=frames,
                    backup=None,
                    unchanged=True,
                )
            )
            continue

        backup = path.with_name(f"{path.name}.bak-{time_ns() // 1_000_000}")
        try:
            backup.write_bytes(path.read_bytes())
            temporary = path.with_name(f"{path.name}.move-tmp")
            temporary.write_bytes(change.payload)
            temporary.replace(path)
        except OSError as error:
            raise ToolError(f"write failed for {path}: {error}") from error

        applied.append(
            AppliedChange(
                artifact=change.artifact,
                old_cwd=change.old_cwd,
                new_cwd=change.new_cwd,
                bytes_before=len(_read(backup)),
                bytes_after=len(change.payload),
                frames=frames,
                backup=backup,
                unchanged=False,
            )
        )
    return tuple(applied)


def verify_move(session: Session, target: Workspace) -> None:
    """Re-read the session from disk and confirm the move took effect.

    Raises:
        ToolError: when the stored cwd is not the target, so a caller can never
            report success on faith.
    """
    artifacts = session.artifacts()
    if not artifacts:
        raise ToolError(f"{session.session_id}: no artifact remains after the move")
    header = decode_header(_read(artifacts[0].path), label=artifacts[0].filename)
    if header.get("cwd") != target.path:
        raise ToolError(
            f"{session.session_id}: post-check failed, stored cwd is {header.get('cwd')!r} "
            f"but {target.path!r} was expected"
        )


def describe_mismatch(session: Session) -> str | None:
    """Explain a directory/header disagreement, or return ``None`` when they agree.

    The project directory name is derived from ``cwd``, so a disagreement means
    the header changed without the artifact moving. DSH reads the header, so the
    session still works, but this tool reports it rather than hiding it.
    """
    cwd = session.cwd
    if cwd is None:
        return "header has no cwd"
    try:
        expected = project_key(cwd)
    except ValueError as error:
        return str(error)
    if expected != session.project_dir_name:
        return f"lives in {session.project_dir_name!r} but its cwd implies {expected!r}"
    return None
