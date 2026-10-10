"""Where the data lives, and the one place that decides it."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Config:
    """Resolved locations for one DSH home."""

    dsh_home: Path

    @property
    def sessions_root(self) -> Path:
        return self.dsh_home / "sessions"

    @property
    def workspace_state(self) -> Path:
        return self.dsh_home / "storages" / "workspace.json"

    @classmethod
    def resolve(cls, dsh_home: str | None = None) -> Config:
        """Build a config from an explicit path, ``$DSH_HOME``, or the default.

        The order matches DSH itself: an explicit argument wins, then the
        environment, then ``~/.dsh``.
        """
        if dsh_home:
            return cls(dsh_home=Path(dsh_home).expanduser())
        from_environment = os.environ.get("DSH_HOME")
        if from_environment:
            return cls(dsh_home=Path(from_environment).expanduser())
        return cls(dsh_home=Path.home() / ".dsh")
