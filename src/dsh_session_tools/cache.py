"""Remember session titles between runs.

Reading a title means decoding a session's log, and a search reads every log in
the library, so the same work would be repeated on every command. This keeps the
answers in a small JSON document keyed by the artifact a title came from, and
trusts an entry only while that artifact's size and modification time are
unchanged.

Three properties matter more than speed here:

* **A stale entry must never win.** The key is the artifact's own size and
  modification time, so a rewritten or appended log invalidates its entry. A log
  DSH appends to changes both.
* **A cache problem must never fail a command.** Every failure to read, parse or
  write the document is treated as an empty cache, because the cache is an
  optimisation and not a source of truth.
* **The cache never invents a title.** It stores the answer the log gave,
  including ``None`` for a session that has no title event, so "no title" and
  "not looked up yet" stay distinguishable.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .sessions import Artifact

#: Bumped when the on-disk shape changes, so an old document is discarded rather
#: than misread.
_FORMAT = 1


def default_cache_path(sessions_root: Path) -> Path:
    """Where the title cache for one session library belongs.

    The library's own path is hashed into the filename so two DSH homes, or one
    home reached two ways, cannot share entries.
    """
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Caches")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    digest = hashlib.sha256(str(sessions_root).encode("utf-8")).hexdigest()[:16]
    return Path(base) / "dsh-session-tools" / f"titles-{digest}.json"


@dataclass(slots=True)
class TitleCache:
    """A best-effort store of ``artifact -> title`` answers."""

    path: Path
    entries: dict[str, dict[str, object]] = field(default_factory=dict)
    hits: int = 0
    misses: int = 0
    _dirty: bool = False

    @classmethod
    def load(cls, path: Path) -> TitleCache:
        """Read the document, or start empty when it is missing or unusable."""
        cache = cls(path=path)
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return cache
        if not isinstance(document, dict) or document.get("format") != _FORMAT:
            return cache
        entries = document.get("entries")
        if isinstance(entries, dict):
            for key, value in entries.items():
                if isinstance(key, str) and isinstance(value, dict):
                    cache.entries[key] = value
        return cache

    @staticmethod
    def _key(artifact: Artifact) -> str:
        return str(artifact.path)

    def lookup(self, artifact: Artifact) -> tuple[bool, str | None]:
        """Return ``(known, title)`` for ``artifact``.

        ``known`` is false when the entry is absent or the artifact has changed
        since it was written, so a caller can tell "no title" from "not read".
        """
        entry = self.entries.get(self._key(artifact))
        if entry is None:
            self.misses += 1
            return False, None
        try:
            stat = artifact.path.stat()
        except OSError:
            self.misses += 1
            return False, None
        if entry.get("size") != stat.st_size or entry.get("mtime_ns") != stat.st_mtime_ns:
            self.misses += 1
            return False, None
        title = entry.get("title")
        if title is not None and not isinstance(title, str):
            self.misses += 1
            return False, None
        self.hits += 1
        return True, title

    def store(self, artifact: Artifact, title: str | None) -> None:
        """Remember one answer, replacing whatever was there."""
        try:
            stat = artifact.path.stat()
        except OSError:
            return
        self.entries[self._key(artifact)] = {
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "title": title,
        }
        self._dirty = True

    def save(self) -> None:
        """Write the document if anything changed. Never raises."""
        if not self._dirty:
            return
        document = {"format": _FORMAT, "entries": self.entries}
        temporary = self.path.with_name(f"{self.path.name}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(document, separators=(",", ":")), encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)
            return
        self._dirty = False
