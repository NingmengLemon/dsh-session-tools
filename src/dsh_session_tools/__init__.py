"""Typed tooling for DSH session artifacts: inspect, verify, and re-home them.

A move is two coupled changes, both verified against the shipping DSH source:

1. A session log is a concatenation of independently decodable zstd frames, and
   the **first** frame must contain exactly the header line
   (``assertZstdHeaderFrame``, ``dsh-session-persistence-jsonl/lib/index.js:2293-2295``).
   So a rewrite replaces the first frame and copies the rest as raw bytes. Frame
   boundaries are walked out of each frame's structure, the way DSH does
   (``scanZstdFrames``, same file ``:1300-1363``); searching for the magic number
   instead is unsound, because it occurs inside compressed payloads.
2. The artifact's path is derived from ``cwd`` + ``id`` (same file ``:914-927``)
   and that derivation is **enforced on every read** by ``assertStoredIdentity``
   (``:3383-3395``), from ``readGenerationHeader`` (``:3091``, so on ``list()``)
   and ``decodeStoredLog`` (``:2820``, so on ``open()``). Rewriting only the
   header therefore does not move a session: it makes one artifact that DSH calls
   corrupt, and because that error escapes ``list()`` (``:3043``) it takes the
   whole session store down with it. Hence a move relocates the artifact *and*
   rewrites the ``cwd``.

The workspace registry still needs no edit, because membership is re-derived from
the header on every read (``dsh-workspace/lib/index.js:102-104``) rather than
taken from the registry's cached lists.

:mod:`dsh_session_tools.contract` checks both of those against the installed DSH
rather than against this package's own idea of them.

The public surface is deliberately small; everything below the CLI is importable
so behaviour can be tested and scripted directly.
"""

from __future__ import annotations

from .config import Config
from .errors import ToolError
from .frames import (
    ZSTD_MAGIC,
    ArtifactError,
    Frame,
    decode_frames,
    decode_header,
    encode_header,
    replace_header_frame,
    scan_frames,
)
from .operations import (
    AppliedChange,
    MoveFailure,
    MovePlan,
    PlannedChange,
    apply_plan,
    describe_mismatch,
    plan_move,
    verify_move,
)
from .paths import encode_segment, project_dir, project_key, session_dir
from .sessions import (
    Artifact,
    ScanProblem,
    ScanResult,
    Session,
    artifacts_of,
    parse_generation,
    read_header,
    scan_sessions,
)
from .workspaces import Registry, Workspace, canonical, load_registry

__all__ = [
    "ZSTD_MAGIC",
    "AppliedChange",
    "Artifact",
    "ArtifactError",
    "Config",
    "Frame",
    "MoveFailure",
    "MovePlan",
    "PlannedChange",
    "Registry",
    "ScanProblem",
    "ScanResult",
    "Session",
    "ToolError",
    "Workspace",
    "apply_plan",
    "artifacts_of",
    "canonical",
    "decode_frames",
    "decode_header",
    "describe_mismatch",
    "encode_header",
    "encode_segment",
    "load_registry",
    "parse_generation",
    "plan_move",
    "project_dir",
    "project_key",
    "read_header",
    "replace_header_frame",
    "scan_frames",
    "scan_sessions",
    "session_dir",
    "verify_move",
]

__version__ = "0.1.0"
