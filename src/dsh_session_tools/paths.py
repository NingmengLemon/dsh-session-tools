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
filesystem limit. Truncation is why ``project_key`` cannot be inverted.
"""

from __future__ import annotations

from pathlib import Path

#: Characters that pass through unescaped into a directory name.
_SAFE = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")

#: Filesystem separators that collapse into a single dash.
_SEPARATORS = frozenset("/\\:")

#: Maximum length of the readable part, leaving room for the ``--`` delimiters
#: and staying inside a 255-byte filesystem component.
_READABLE_LIMIT = 251


def _escape(code: int) -> str:
    """Encode one code unit the way DSH does: ``~`` plus four uppercase hex digits."""
    return f"~{code:04X}"


def project_key(cwd: str) -> str:
    """Return the project directory name DSH derives from a session's ``cwd``.

    Raises:
        ValueError: if ``cwd`` is empty, which DSH also refuses.
    """
    if not cwd:
        raise ValueError("cannot encode an empty project path")

    readable: list[str] = []
    separator_run = False

    for character in cwd:
        if character in _SEPARATORS:
            # A run of separators collapses to exactly one dash.
            if not separator_run:
                readable.append("-")
            separator_run = True
        elif character != "~" and character in _SAFE:
            readable.append(character)
            separator_run = False
        else:
            readable.append(_escape(ord(character)))
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
    for character in raw:
        if character != "~" and character in _SAFE:
            parts.append(character)
        else:
            parts.append(_escape(ord(character)))
    return "".join(parts)


def project_dir(sessions_root: Path, cwd: str) -> Path:
    """Return the project directory holding the artifacts of sessions in ``cwd``."""
    return sessions_root / project_key(cwd)


def session_dir(sessions_root: Path, cwd: str, session_id: str) -> Path:
    """Return the directory holding one session's artifacts."""
    return project_dir(sessions_root, cwd) / encode_segment(session_id)
