"""Discover session artifacts under ``<dshHome>/sessions``.

The authoritative generation is the one DSH itself would pick: the numerically
highest canonical filename in the session directory
(``resolveGenerationInDirectory``, ``dsh-session-persistence-jsonl/lib/index.js:3326``,
"Select the numerically highest canonical generation"). We reuse that rule so a
session's identity here matches what DSH reports.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .errors import ToolError
from .frames import ArtifactError, decode_header_frame, read_header_bytes
from .paths import project_key

#: Canonical artifact filenames, highest generation first. This is the same set
#: DSH writes; anything else in a session directory is ignored.
_CANONICAL = re.compile(r"^session(?:\.v(?P<version>[1-9][0-9]*))?\.jsonl(?P<zstd>\.zstd)?$")


@dataclass(frozen=True, slots=True)
class Artifact:
    """One generation file of a session."""

    path: Path
    filename: str
    version: int
    compressed: bool


@dataclass(frozen=True, slots=True)
class Session:
    """One discovered session, with every generation it has on disk."""

    session_id: str
    directory: Path
    project_dir_name: str
    header: dict[str, object]

    @property
    def cwd(self) -> str | None:
        """The stored ``cwd``, which is what decides workspace membership."""
        value = self.header.get("cwd")
        return value if isinstance(value, str) else None

    @property
    def version(self) -> int | None:
        value = self.header.get("version")
        return value if isinstance(value, int) else None

    @property
    def origin(self) -> str | None:
        value = self.header.get("origin")
        return value if isinstance(value, str) else None

    @property
    def is_subagent(self) -> bool:
        return self.origin == "subagent"

    def artifacts(self) -> tuple[Artifact, ...]:
        """Every canonical generation file in this session's directory."""
        return artifacts_of(self.directory)


@dataclass(frozen=True, slots=True)
class ScanProblem:
    """A directory or artifact we could not use, kept so it can be reported."""

    path: Path
    reason: str


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Everything one pass over the session root found."""

    sessions: tuple[Session, ...]
    problems: tuple[ScanProblem, ...]

    def by_id(self, session_id: str) -> tuple[Session, ...]:
        return tuple(session for session in self.sessions if session.session_id == session_id)


def parse_generation(filename: str) -> int | None:
    """Return the format generation a canonical filename declares, or ``None``.

    ``session.v4.jsonl.zstd`` -> 4; ``session.jsonl.zstd`` -> 0 (legacy naming);
    anything non-canonical, including temporary and backup names, -> ``None``.
    """
    match = _CANONICAL.match(filename)
    if match is None:
        return None
    raw = match.group("version")
    return int(raw) if raw is not None else 0


def artifacts_of(directory: Path) -> tuple[Artifact, ...]:
    """Return every canonical artifact in ``directory``, highest generation first."""
    found: list[Artifact] = []
    try:
        entries = list(directory.iterdir())
    except OSError:
        return ()
    for entry in entries:
        if not entry.is_file() or entry.is_symlink():
            continue
        version = parse_generation(entry.name)
        if version is None:
            continue
        found.append(
            Artifact(
                path=entry,
                filename=entry.name,
                version=version,
                compressed=entry.name.endswith(".zstd"),
            )
        )
    found.sort(key=lambda artifact: (-artifact.version, artifact.filename))
    return tuple(found)


def read_header(artifact: Artifact) -> dict[str, object]:
    """Read one artifact's header.

    Only the first frame is decoded, so listing a large library stays cheap. The
    full container gate (:func:`~dsh_session_tools.frames.decode_frames`) runs on
    the write path and in ``dsh-sessions verify``.

    Raises:
        ArtifactError: when the artifact is malformed.
    """
    if artifact.compressed:
        raw = read_header_bytes(artifact.path, label=artifact.filename)
        frame = decode_header_frame(raw, label=artifact.filename)
        line = frame[: frame.find(b"\n")]
    else:
        line = read_header_bytes(artifact.path, label=artifact.filename).split(b"\n", 1)[0]

    try:
        parsed = json.loads(line)
    except json.JSONDecodeError as error:
        raise ArtifactError(f"{artifact.filename}: header line is not valid JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise ArtifactError(f"{artifact.filename}: header is not a JSON object")
    return parsed


def scan_sessions(sessions_root: Path) -> ScanResult:
    """Walk ``sessions_root`` and collect every usable session.

    Directories that are symlinks or junctions are skipped rather than followed,
    matching DSH's own refusal to treat them as session storage.
    """
    sessions: list[Session] = []
    problems: list[ScanProblem] = []

    if not sessions_root.is_dir():
        raise ToolError(f"sessions root is not a directory: {sessions_root}")

    for project in sorted(sessions_root.iterdir()):
        if not project.is_dir() or project.is_symlink():
            continue
        for directory in sorted(project.iterdir()):
            if not directory.is_dir() or directory.is_symlink():
                continue
            artifacts = artifacts_of(directory)
            if not artifacts:
                continue
            try:
                header = read_header(artifacts[0])
            except (ArtifactError, OSError) as error:
                problems.append(ScanProblem(path=directory, reason=str(error)))
                continue
            session_id = header.get("id")
            if not isinstance(session_id, str) or not session_id:
                problems.append(ScanProblem(path=directory, reason="header has no usable id"))
                continue
            sessions.append(
                Session(
                    session_id=session_id,
                    directory=directory,
                    project_dir_name=project.name,
                    header=header,
                )
            )

    return ScanResult(sessions=tuple(sessions), problems=tuple(problems))


def directory_matches_header(session: Session) -> bool:
    """Whether the directory the artifact lives in agrees with the header.

    DSH derives the directory name from ``cwd`` and ``id``, so a mismatch means
    the header and its location have drifted apart. Reported, never silently
    trusted.
    """
    cwd = session.cwd
    if cwd is None:
        return False
    try:
        return session.project_dir_name == project_key(cwd)
    except ValueError:
        return False
