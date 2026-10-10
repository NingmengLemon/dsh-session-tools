"""DSH's on-disk naming rules, transcribed from the shipping implementation.

Two different encodings are needed and they are not interchangeable:

``project_key``
    Maps a session's ``cwd`` to the project directory that groups its artifact::

        E:\\Projects\\Python\\foo  ->  --E-Projects-Python-foo--

    Source: ``dsh-session-persistence-jsonl/lib/index.js:875-894`` (``projectKey``).

``encode_segment``
    Maps a session id to its directory segment inside the project directory::

        session-abc  ->  session-abc
        a/b          ->  a~002Fb

    Source: ``dsh-session-persistence-jsonl/lib/index.js:853-865`` (``encodeSegment``).

Both are lossy in the same documented way: filesystem separators collapse to a
single ``-`` and the readable part is truncated so the component fits a
filesystem limit. Truncation is why ``project_key`` cannot be inverted, and it
also means two different paths can share one project directory, which is a
property DSH has rather than a bug this transcription introduced.

Both iterate UTF-16 code units, because the original does: JavaScript indexes a
string by code unit, so a character above U+FFFF is escaped as its surrogate
pair. Iterating Python code points instead would emit ``~1F600`` where DSH
writes ``~D83D~DE00``, and the two would disagree about which directory a path
belongs to.
"""

from __future__ import annotations

from pathlib import Path

#: Characters that pass through unescaped into a directory name.
_SAFE = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")

#: Filesystem separators that collapse into a single dash.
_SEPARATORS = frozenset("/\\:")

#: Maximum length of the readable part. ``--`` + 251 + ``--`` is 255 characters,
#: which is the component limit the original truncates for.
_READABLE_LIMIT = 251

#: The project directory DSH uses for a session whose header carries no ``cwd``
#: (``projectDir``, ``dsh-session-persistence-jsonl/lib/index.js:902-905``).
NO_CWD_DIRECTORY = "_no-cwd"


def _escape(code: int) -> str:
    """Encode one UTF-16 code unit the way DSH does: ``~`` plus four hex digits."""
    return f"~{code:04X}"


def _code_units(text: str) -> list[int]:
    """The UTF-16 code units of ``text``, as integers.

    This is what makes the transcription faithful rather than merely close: the
    original walks code units, so an astral character is two escapes, not one.
    """
    encoded = text.encode("utf-16-le", "surrogatepass")
    return [encoded[index] | (encoded[index + 1] << 8) for index in range(0, len(encoded), 2)]


def project_key(cwd: str) -> str:
    """Return the project directory name DSH derives from a session's ``cwd``.

    Raises:
        ValueError: if ``cwd`` is empty, which DSH also refuses.
    """
    if not cwd:
        raise ValueError("cannot encode an empty project path")

    readable: list[str] = []
    separator_run = False

    for code in _code_units(cwd):
        character = chr(code)
        if character in _SEPARATORS:
            # A run of separators collapses to exactly one dash.
            if not separator_run:
                readable.append("-")
            separator_run = True
        elif character != "~" and character in _SAFE:
            readable.append(character)
            separator_run = False
        else:
            readable.append(_escape(code))
            separator_run = False

    joined = "".join(readable).lstrip("-")
    return f"--{(joined or 'root')[:_READABLE_LIMIT]}--"


def encode_segment(raw: str) -> str:
    """Return the directory segment DSH derives from a session id.

    Raises:
        ValueError: if ``raw`` is empty, which DSH also refuses.
    """
    if not raw:
        raise ValueError("cannot encode an empty path segment")
    if raw == ".":
        return "~002E"
    if raw == "..":
        return "~002E~002E"

    parts: list[str] = []
    for code in _code_units(raw):
        character = chr(code)
        if character != "~" and character in _SAFE:
            parts.append(character)
        else:
            parts.append(_escape(code))
    return "".join(parts)


def project_dir(sessions_root: Path, cwd: str | None) -> Path:
    """Return the project directory holding the artifacts of sessions in ``cwd``.

    ``cwd`` is ``None`` for a header that carries no ``cwd`` at all, which DSH
    files under a shared ``_no-cwd`` directory rather than refusing.
    """
    if cwd is None:
        return sessions_root / NO_CWD_DIRECTORY
    return sessions_root / project_key(cwd)


def session_dir(sessions_root: Path, cwd: str | None, session_id: str) -> Path:
    """Return the directory holding one session's artifacts."""
    return project_dir(sessions_root, cwd) / encode_segment(session_id)
