"""The operations: inspect a session, and move it to another workspace.

Moving a session is two coupled changes
---------------------------------------
DSH derives the artifact's path from the header's ``cwd`` and ``id``
(``dsh-session-persistence-jsonl/lib/index.js:914-927``) and then *enforces* that
derivation on every read. ``assertStoredIdentity``, same file ``:3383-3395``::

    expectedPath = generationLogPath(this.root, meta.cwd, meta.id, ...)
    if (path !== expectedPath && !await this.sameFile(path, expectedPath, signal))
        throw new Error(`corrupt session log "${path}": header id ... and cwd identify ...`)

That check runs from ``readGenerationHeader`` (``:3091``) on every ``list()`` and
``stat()``, and from ``decodeStoredLog`` (``:2820``) on every ``open()``. It
raises a plain ``Error``, which ``listArtifacts`` does not swallow (``:3043``
catches only the two typed errors), so it escapes ``list()`` and reaches the
workspace registry, the ACP session list and the session query. Rewriting only
the header therefore does not move a session; it makes one artifact that DSH
calls corrupt, and on this host that takes the whole session store down with it.

So a move is: relocate the artifact into the project directory the new ``cwd``
names, *and* rewrite the header to that ``cwd``. Neither half is sufficient. The
tool used to do only the second, which is why this module now refuses a session
whose header and location already disagree, instead of quietly adding one more.

Why the registry is still not edited
------------------------------------
A workspace's membership comes from each session's own header, re-derived on
every read::

    dsh-workspace/lib/index.js:102-104
        get sessionIds() {
            return this.record.sessionIds.filter(
                (id) => this.host.sessionPath(id) === this.record.path);
        }

``indexHeader`` (``:872-890``) resolves the header's ``cwd`` with
``realpathNormalize`` and compares it against the stored workspace path. Since
that resolution must land exactly on ``record.path``, the ``cwd`` written here is
the registry's path verbatim, never a canonicalised spelling of it; writing a
resolved path would leave the session in no workspace at all.

Deliberate conservatism
-----------------------
* A session whose header and location already disagree is refused, because DSH
  reports it as corrupt and moving it would carry the damage forward.
* A session whose generations disagree about ``cwd`` is refused rather than
  guessed at, because we cannot tell which one DSH considers current.
* ``version`` is carried through untouched. Re-stamping it to match a new
  filename is what makes DSH reject an older log as malformed.
* Subagent sessions and archived sessions need an explicit override.
* Nothing is written until every artifact has been read, validated and rebuilt
  in memory.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from time import time_ns

from .errors import ToolError
from .frames import ArtifactError, decode_header, replace_header_frame, scan_frames
from .paths import encode_segment, project_key
from .sessions import Artifact, Session, parse_generation
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
    source_directory: Path
    target_directory: Path

    @property
    def session_id(self) -> str:
        return self.session.session_id

    @property
    def is_noop(self) -> bool:
        return all(change.unchanged for change in self.changes)

    @property
    def relocates(self) -> bool:
        """Whether the artifacts have to change directory."""
        return self.source_directory != self.target_directory


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
    destination: Path


@dataclass(frozen=True, slots=True)
class AppliedMove:
    """The result of applying a plan."""

    session_id: str
    changes: tuple[AppliedChange, ...]
    source_directory: Path
    target_directory: Path
    moved: bool
    leftovers: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class MoveFailure:
    """A refusal, with the reason stated rather than a bare false."""

    session_id: str
    reason: str


PlanOutcome = MovePlan | MoveFailure

#: Suffix for the sibling copy taken before an artifact is replaced.
BACKUP_MARKER = ".bak-"

#: Suffix for the staged file written beside its destination before the swap.
_STAGING_MARKER = ".move-tmp"


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise ArtifactError(f"cannot read {path}: {error}") from error


def target_directory_for(session_directory: Path, target_cwd: str, session_id: str) -> Path:
    """Where the artifacts must live for a header carrying ``target_cwd``.

    The project directory is one level up from the session directory, so this
    keeps the move inside the same sessions root without needing the root itself.
    """
    return session_directory.parent.parent / project_key(target_cwd) / encode_segment(session_id)


def resolve_link_spelling(path: str) -> str | None:
    """Return the resolved spelling of ``path`` when it differs, else ``None``.

    A registry path that reaches its directory through a link is the one case
    where writing it verbatim cannot work, because DSH resolves the cwd and then
    compares the result against the unresolved stored path.

    The comparison is textual rather than on identity, and deliberately so: two
    spellings of one directory still fail DSH's check, and that is the problem.
    Windows short and long names are the common instance.
    """
    try:
        resolved = str(Path(path).resolve(strict=True))
    except OSError:
        return None
    return resolved if resolved != path else None


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

    # The cwd is written as the registry's path verbatim, and DSH resolves that
    # cwd before comparing it against the same stored path. When the two
    # spellings differ the resolved form never equals the stored one, so the
    # session is filed under no workspace and only a warning is logged
    # (dsh-workspace/lib/index.js:872-890). That is a silent detach, so it is
    # refused here rather than discovered later.
    unlinked = resolve_link_spelling(target_path)
    if unlinked is not None:
        return MoveFailure(
            session_id=session.session_id,
            reason=(
                f"the workspace path {target_path!r} resolves to {unlinked!r}. DSH resolves the "
                "header cwd and compares it against the workspace path it stored, so a session "
                "moved here would belong to no workspace. Register the resolved path, or pick a "
                "workspace that is not reached through a link."
            ),
        )

    # A session DSH already reports as corrupt must not be carried forward. This
    # is the check whose absence let an earlier version write a header that DSH
    # rejects, taking the whole session store down with it.
    expected_directory = project_key(stored_cwd)
    if session.project_dir_name != expected_directory:
        return MoveFailure(
            session_id=session.session_id,
            reason=(
                f"DSH would call this log corrupt: it sits in {session.project_dir_name!r} "
                f"but its cwd implies {expected_directory!r}. Nothing was written. "
                "Restore the artifact to the directory its cwd names, or move it with a "
                "tool that relocates the artifact and the header together."
            ),
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
            # Already correct: in place, and pointing where it should.
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
                old_cwd=artifact_cwd,
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
        source_directory=session.directory,
        target_directory=target_directory_for(session.directory, target_path, session.session_id),
    )


def apply_plan(plan: MovePlan) -> AppliedMove:
    """Write a planned move to disk.

    Nothing is replaced until every artifact has been staged next to its
    destination, so a failure while staging leaves the store untouched. Each
    replacement is preceded by a ``.bak-<epoch-ms>`` sibling copy and performed
    by renaming the staged file over the target, which is atomic on one volume.

    Raises:
        ToolError: when a write fails. Artifacts already replaced keep their
            backups, so the state is recoverable with ``restore``.
    """
    staged: list[tuple[PlannedChange, Path, Path]] = []
    try:
        for change in plan.changes:
            if change.unchanged and change.artifact.path.parent == plan.target_directory:
                continue
            destination = plan.target_directory / change.artifact.filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f"{destination.name}{_STAGING_MARKER}")
            temporary.write_bytes(change.payload)
            staged.append((change, temporary, destination))
    except OSError as error:
        for _, temporary, _ in staged:
            temporary.unlink(missing_ok=True)
        raise ToolError(f"could not stage the move: {error}") from error

    applied: list[AppliedChange] = []
    try:
        for change, temporary, destination in staged:
            source = change.artifact.path
            existing = destination.read_bytes() if destination.is_file() else None
            bytes_before = len(existing) if existing is not None else source.stat().st_size

            if existing == change.payload:
                temporary.unlink(missing_ok=True)
                applied.append(
                    AppliedChange(
                        artifact=change.artifact,
                        old_cwd=change.old_cwd,
                        new_cwd=change.new_cwd,
                        bytes_before=bytes_before,
                        bytes_after=bytes_before,
                        frames=change.frames,
                        backup=None,
                        unchanged=True,
                        destination=destination,
                    )
                )
                continue

            # Keep the bytes that are being displaced. When the destination is
            # empty the displaced bytes are the source's, so the copy is taken
            # from there, which also keeps the backup next to the original.
            if existing is not None:
                backup = destination.with_name(f"{destination.name}{BACKUP_MARKER}{time_ns() // 1_000_000}")
                backup.write_bytes(existing)
            else:
                backup = source.with_name(f"{source.name}{BACKUP_MARKER}{time_ns() // 1_000_000}")
                backup.write_bytes(_read(source))

            temporary.replace(destination)
            applied.append(
                AppliedChange(
                    artifact=change.artifact,
                    old_cwd=change.old_cwd,
                    new_cwd=change.new_cwd,
                    bytes_before=bytes_before,
                    bytes_after=len(change.payload),
                    frames=change.frames,
                    backup=backup,
                    unchanged=False,
                    destination=destination,
                )
            )
    except OSError as error:
        for _, temporary, _ in staged:
            temporary.unlink(missing_ok=True)
        raise ToolError(f"write failed while moving {plan.session_id}: {error}") from error

    moved = plan.relocates and any(not change.unchanged for change in plan.changes)
    leftovers: tuple[Path, ...] = ()
    if moved:
        leftovers = _tidy_source(plan)

    return AppliedMove(
        session_id=plan.session_id,
        changes=tuple(applied),
        source_directory=plan.source_directory,
        target_directory=plan.target_directory,
        moved=moved,
        leftovers=leftovers,
    )


def _tidy_source(plan: MovePlan) -> tuple[Path, ...]:
    """Remove the originals that were relocated, and report what stays behind.

    A session directory can hold files this tool does not own, such as the
    ``session.lock`` lease DSH uses. Those are reported rather than deleted, so
    the only things removed here are the artifacts that were successfully
    written at their destination.
    """
    leftovers: list[Path] = []
    for change in plan.changes:
        source = change.artifact.path
        if change.unchanged:
            continue
        if not source.is_file():
            continue
        try:
            source.unlink()
        except OSError as error:
            raise ToolError(
                f"{plan.session_id}: wrote the artifact to its destination but could not remove "
                f"{source}: {error}. Both copies now exist; remove the source by hand."
            ) from error

    if plan.source_directory.is_dir():
        remaining = sorted(plan.source_directory.iterdir())
        if remaining:
            leftovers = remaining
        else:
            try:
                plan.source_directory.rmdir()
            except OSError:
                leftovers = sorted(plan.source_directory.iterdir())

    return tuple(leftovers)


def verify_move(session: Session, target: Workspace) -> Path:
    """Re-read the session and confirm both halves of the move took effect.

    Checking the header alone is not enough, and believing that it was is what
    let an earlier version report success on a session DSH rejects. This asserts
    the same relation DSH asserts:

    * the artifact sits in the project directory that the cwd names, and
    * the stored cwd is the target workspace path.

    Returns:
        The directory the artifacts were found in.

    Raises:
        ToolError: when either half is wrong, so a caller can never report
            success on faith.
    """
    directory = target_directory_for(session.directory, target.path, session.session_id)
    if not directory.is_dir():
        raise ToolError(f"{session.session_id}: no artifact directory at {directory} after the move")

    canonical = sorted(
        (
            entry
            for entry in directory.iterdir()
            if entry.is_file() and parse_generation(entry.name) is not None
        ),
        key=lambda entry: entry.name,
    )
    if not canonical:
        raise ToolError(f"{session.session_id}: no artifact remains after the move at {directory}")

    header = decode_header(_read(canonical[0]), label=canonical[0].name)
    stored = header.get("cwd")
    if stored != target.path:
        raise ToolError(
            f"{session.session_id}: post-check failed, stored cwd is {stored!r} "
            f"but {target.path!r} was expected"
        )

    expected = project_key(target.path)
    if directory.parent.name != expected:
        raise ToolError(
            f"{session.session_id}: post-check failed, the artifact sits in "
            f"{directory.parent.name!r} but its cwd implies {expected!r}, which DSH treats as a "
            "corrupt session log"
        )
    return directory


def describe_mismatch(session: Session) -> str | None:
    """Explain a directory/header disagreement, or return ``None`` when they agree.

    The project directory name is derived from ``cwd``, so a disagreement means
    the header changed without the artifact moving. DSH reads the header and then
    requires the artifact to be where that header says, which makes this state a
    corrupt log rather than a curiosity: ``list()`` throws and the whole session
    store becomes unreadable, not just this session.
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
