"""Session titles, read from the log, because DSH keeps them there.

A session's title is not in its header. DSH appends a ``session/title`` event to
the log each time the title changes, and the title in effect is the last one
(``foldSessionTitle``, ``dsh-session-title/lib/index.js:188-198``; the projection
at ``:172-182``). The header carries none of it: a header written by DSH has no
``title`` key at all.

Reading the title therefore means reading the log, not just its first frame.
Frames are independently decodable, so the walk here decompresses them one at a
time and keeps only the most recent title it has seen.
"""

from __future__ import annotations

import json

from .frames import ArtifactError, decompress_frame, scan_frames
from .sessions import Artifact

_TITLE_EVENT = "session/title"

#: A cheap pre-check before parsing. A frame or line that does not contain these
#: bytes cannot hold a title event, so most of the log is never parsed as JSON.
_NEEDLE = b'"session/title"'


def session_title(artifact: Artifact) -> str | None:
    """Return the title DSH would show for this artifact, or ``None`` if it has none.

    Only the artifact's own generation is read, which is the one DSH opens.

    Raises:
        ArtifactError: when the artifact cannot be decoded.
        OSError: when the file cannot be read.
    """
    raw = artifact.path.read_bytes()
    if not artifact.compressed:
        return _latest_title(raw.split(b"\n"))

    title: str | None = None
    for frame in scan_frames(raw):
        plain = decompress_frame(raw, frame, label=artifact.filename)
        if _NEEDLE not in plain:
            continue
        found = _latest_title(plain.split(b"\n"), label=artifact.filename)
        if found is not None:
            title = found
    return title


def _latest_title(lines: list[bytes], *, label: str = "log") -> str | None:
    """Return the title of the last ``session/title`` event among ``lines``."""
    title: str | None = None
    for line in lines:
        if _NEEDLE not in line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ArtifactError(f"{label}: a session/title candidate is not valid JSON: {error}") from error
        if not isinstance(event, dict) or event.get("type") != _TITLE_EVENT:
            continue
        data = event.get("data")
        if isinstance(data, dict) and isinstance(data.get("title"), str):
            title = data["title"]
    return title


def one_line(text: str) -> str:
    """Make a title safe to print on one terminal line.

    DSH already normalises titles on the way in, but a log is an input this tool
    does not control. Control characters and bidirectional overrides (category Cf)
    are not printable, so they become spaces, and runs of whitespace collapse.
    """
    cleaned = "".join(character if character.isprintable() else " " for character in text)
    return " ".join(cleaned.split())
