"""Typed tooling for DSH session artifacts: inspect, verify, and re-home them.

Two mechanisms carry the whole design, both verified against the shipping DSH
source rather than assumed:

1. A session's workspace membership comes from the ``cwd`` stored in its header.
   DSH re-derives membership on every read (``dsh-workspace/lib/index.js:102-104``)
   and derives the artifact path from ``cwd`` + ``id``
   (``dsh-session-persistence-jsonl/lib/index.js:914-927``), so the header *is*
   the membership and the registry's cached session lists never need editing.
2. A session log is a concatenation of independently decodable zstd frames, and
   the **first** frame must contain exactly the header line
   (``assertZstdHeaderFrame``, same file ``:2293-2295``). So a rewrite replaces
   the first frame and copies the rest as raw bytes.

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
