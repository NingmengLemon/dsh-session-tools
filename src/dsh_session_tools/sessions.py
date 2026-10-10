"""Discover session artifacts under ``<dshHome>/sessions``.

The authoritative generation is the one DSH itself would pick: the numerically
highest canonical filename in the session directory
(``resolveGenerationInDirectory``, ``dsh-session-persistence-jsonl/lib/index.js:3327``,
"Select the numerically highest canonical generation"). We reuse that rule so a
session's identity here matches what DSH reports.

A directory legitimately holds several generations at once. DSH reads the
highest and treats the rest as retained history, so finding more than one is
normal rather than a fault, and every one of them has to move together.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .errors import ToolError
from .frames import ArtifactError, decode_header_frame, read_header_bytes
from .paths import NO_CWD_DIRECTORY, project_key

#: The canonical filename shapes, matching DSH's own
#: ``CANONICAL_LOG_FILENAME`` (``dsh-session-format/lib/index.js:464``). A name
#: with no ``.vN`` is version 0, not a legacy leftover.
_CANONICAL = re.compile(r"^session(?:\.v(?P<version>[1-9][0-9]*))?\.jsonl(?P<zstd>\.zstd)?$")

#: Largest version DSH accepts, from ``Number.isSafeInteger`` in
#: ``parseSessionFormatLogFilename``. A name above this is not canonical.
_MAX_VERSION = 2**53 - 1


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

    ``session.v4.jsonl.zstd`` -> 4; ``session.jsonl.zstd`` -> 0, which is the
    canonical version-zero name rather than a legacy leftover; anything
    non-canonical, including temporary and backup names, -> ``None``.

    A version above ``Number.MAX_SAFE_INTEGER`` is rejected because DSH rejects
    it (``Number.isSafeInteger``), so a name like ``session.v9007199254740992``
    is not one this tool should treat as an artifact.
    """
    match = _CANONICAL.match(filename)
    if match is None:
        return None
    raw = match.group("version")
    if raw is None:
        return 0
    version = int(raw)
    return version if version <= _MAX_VERSION else None


def artifacts_of(directory: Path) -> tuple[Artifact, ...]:
    """Return every canonical artifact in ``directory``, highest generation first.

    A directory holding more than one generation is normal: DSH reads the highest
    and keeps the rest as retained history.
    """
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


def encoding_conflict(directory: Path) -> str | None:
    """Explain a mixed-compression session directory, or return ``None``.

    A backend is configured for exactly one physical encoding, and DSH refuses a
    directory that carries the other one: ``resolveGenerationInDirectory``
    collects names matching the opposite suffix and throws ``encodingMismatch``
    (``dsh-session-persistence-jsonl/lib/index.js:3348-3350``). A directory with
    both suffixes is therefore one DSH will not load at all, which is worth
    reporting rather than quietly reading the highest version of.
    """
    artifacts = artifacts_of(directory)
    if not artifacts:
        return None
    compressed = [artifact.filename for artifact in artifacts if artifact.compressed]
    plain = [artifact.filename for artifact in artifacts if not artifact.compressed]
    if compressed and plain:
        return (
            f"holds both zstd and uncompressed artifacts ({', '.join(sorted(compressed + plain))}); "
            "DSH refuses a directory that carries the encoding its backend is not configured for"
        )
    return None


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
            conflict = encoding_conflict(directory)
            if conflict is not None:
                problems.append(ScanProblem(path=directory, reason=conflict))
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


def expected_project_directory(cwd: str | None) -> str:
    """The project directory name DSH derives from a header's ``cwd``.

    A header with no ``cwd`` at all lands in DSH's shared ``_no-cwd`` directory
    (``projectDir``, ``dsh-session-persistence-jsonl/lib/index.js:902-905``); it
    is not an error, it is just not attributable to a workspace.
    """
    if cwd is None:
        return NO_CWD_DIRECTORY
    return project_key(cwd)


def directory_matches_header(session: Session) -> bool:
    """Whether the directory the artifact lives in agrees with the header.

    DSH derives the directory name from ``cwd`` and ``id``, so a mismatch means
    the header and its location have drifted apart. That state is not a
    curiosity: DSH enforces the derivation on every read and calls a log that
    fails it corrupt, so this is reported loudly rather than trusted.
    """
    try:
        return session.project_dir_name == expected_project_directory(session.cwd)
    except ValueError:
        return False
