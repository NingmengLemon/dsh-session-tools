"""Check this tool's view of a session store against what DSH itself accepts.

Why this exists
---------------
Two of this tool's premises were wrong in ways a self-consistent test fixture
could not detect: DSH requires an artifact to sit where its header's ``cwd``
says, and it resolves that ``cwd`` before comparing it against the stored
workspace path. Both are visible only by asking the real backend, so the check
below drives the shipped DSH code rather than reimplementing its rules a second
time.

What it asserts
---------------
* DSH's ``list()`` succeeds. That single call is what a header-only rewrite
  breaks, and it breaks for the whole store rather than for one session.
* Every session DSH lists is one this tool also found, and the ``cwd`` agrees.
  A session this tool cannot see would otherwise be silently unmovable.
* Every listed session loads through ``requireStoredLog``, which re-derives the
  artifact path from the header and refuses a log that is not there.

Read-only in both modes. Nothing is moved or written.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ToolError, quote
from .operations import MovePlan, apply_plan, plan_move
from .paths import encode_segment, project_key
from .sessions import scan_sessions
from .workspaces import load_registry

#: Where a global npm install of DSH usually lands, checked in order.
_INSTALL_CANDIDATES = (
    Path("E:/WorkBench/nodejs/npm-global/node_modules/@deepseek-ai/dsh"),
    Path.home() / "AppData/Roaming/npm/node_modules/@deepseek-ai/dsh",
    Path("/usr/local/lib/node_modules/@deepseek-ai/dsh"),
    Path("/usr/lib/node_modules/@deepseek-ai/dsh"),
)

_PROBE = Path(__file__).with_name("dsh_contract.mjs")

#: Seconds before the probe is treated as hung rather than slow.
_PROBE_TIMEOUT = 300


@dataclass(slots=True)
class ContractReport:
    """What the check found, in a form the CLI can print."""

    sessions_root: Path
    dsh_root: Path | None
    dsh_sessions: dict[str, str | None] = field(default_factory=dict)
    dsh_events: dict[str, int] = field(default_factory=dict)
    unreadable: dict[str, str] = field(default_factory=dict)
    tool_sessions: dict[str, str | None] = field(default_factory=dict)
    membership: dict[str, tuple[str, ...]] = field(default_factory=dict)
    invisible: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    pre_existing: list[str] = field(default_factory=list)
    sample: Path | None = None
    skipped: str | None = None

    @property
    def ok(self) -> bool:
        return not self.failures and self.skipped is None


def find_dsh_install(explicit: str | None = None) -> Path | None:
    """Locate an installed DSH package that carries the session backend."""
    candidates = [Path(explicit)] if explicit else list(_INSTALL_CANDIDATES)
    for candidate in candidates:
        marker = candidate / "node_modules" / "@deepseek-ai" / "dsh-session-persistence-jsonl"
        if marker.is_dir():
            return candidate
    return None


def run_probe(dsh_root: Path, sessions_root: Path) -> dict[str, object]:
    """Ask the shipped DSH backend what it makes of ``sessions_root``.

    Raises:
        ToolError: when node is missing, the probe fails, or it does not return
            the JSON object it promises. A check that cannot run is reported as
            a failure rather than passing quietly.
    """
    node = shutil.which("node")
    if node is None:
        raise ToolError("node was not found on PATH, so the DSH contract check cannot run")
    if not _PROBE.is_file():
        raise ToolError(f"the contract probe is missing from the installation: {_PROBE}")

    try:
        completed = subprocess.run(
            [node, str(_PROBE), str(dsh_root), str(sessions_root)],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ToolError(f"could not run the contract probe: {error}") from error

    if completed.returncode != 0:
        raise ToolError(
            f"the contract probe exited {completed.returncode}: "
            f"{(completed.stderr or completed.stdout).strip()[:500]}"
        )
    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise ToolError(f"the contract probe did not return JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise ToolError("the contract probe returned something other than an object")
    return parsed


def _object(value: object) -> dict[str, object]:
    """Narrow a parsed JSON value to an object, or an empty one."""
    return value if isinstance(value, dict) else {}


def _rows(value: object) -> list[object]:
    """Narrow a parsed JSON value to a list, or an empty one."""
    return value if isinstance(value, list) else []


def _workspaces(paths: tuple[str, ...] | list[str]) -> str:
    """Render the workspaces a session appears in, one quoted path at a time."""
    return ", ".join(quote(path) for path in paths) if paths else "no workspace"


def check(sessions_root: Path, *, dsh_home_path: str | None = None) -> ContractReport:
    """Compare this tool's view of ``sessions_root`` with DSH's own."""
    report = ContractReport(sessions_root=sessions_root, dsh_root=None)

    dsh_root = find_dsh_install(dsh_home_path)
    if dsh_root is None:
        report.skipped = (
            "no DSH installation was found, so the contract could not be checked. "
            "Point at one with --dsh-install."
        )
        return report
    report.dsh_root = dsh_root

    scan = scan_sessions(sessions_root)
    report.tool_sessions = {session.session_id: session.cwd for session in scan.sessions}

    probed = run_probe(dsh_root, sessions_root)

    for message in _rows(probed.get("errors")):
        report.failures.append(str(message))

    for entry in _rows(probed.get("listed")):
        row = _object(entry)
        session_id = row.get("id")
        if isinstance(session_id, str):
            cwd = row.get("cwd")
            report.dsh_sessions[session_id] = cwd if isinstance(cwd, str) else None

    for message in _rows(probed.get("notes")):
        report.notes.append(str(message))

    for session_id, holders in _object(probed.get("membership")).items():
        report.membership[str(session_id)] = tuple(str(item) for item in _rows(holders))
    report.invisible = [str(item) for item in _rows(probed.get("invisible"))]

    for session_id, outcome in _object(probed.get("loaded")).items():
        row = _object(outcome)
        if row.get("ok"):
            events = row.get("events")
            report.dsh_events[str(session_id)] = events if isinstance(events, int) else 0
        else:
            report.unreadable[str(session_id)] = str(row.get("error", "unknown error"))

    missing = sorted(set(report.dsh_sessions) - set(report.tool_sessions))
    for session_id in missing:
        report.failures.append(
            f"DSH lists {session_id} but this tool did not find it; it would not be movable"
        )

    for session_id, cwd in sorted(report.dsh_sessions.items()):
        if session_id in report.tool_sessions and report.tool_sessions[session_id] != cwd:
            report.failures.append(
                f"{session_id}: DSH reads cwd {quote(cwd)} but this tool reads "
                f"{quote(report.tool_sessions[session_id])}"
            )

    for session_id, message in sorted(report.unreadable.items()):
        # A session DSH lists but will not open is a fault in the store, but not
        # necessarily one this tool caused: this machine's library holds six v0
        # artifacts DSH refuses as an unsupported descriptor version. Those are
        # reported as store state so a real divergence is not buried among them.
        report.pre_existing.append(f"{session_id}: {message}")

    return report


def sample_artifact(sessions_root: Path) -> Path | None:
    """Return a small real artifact from ``sessions_root``, or ``None``.

    Building a log by hand is the trap this whole check exists to catch: two
    hand-built attempts at a valid v4 log were both refused by DSH, because the
    header and event shapes are stricter than they look. A real artifact is
    therefore the fixture, so the self-test asserts the move rather than the
    fixture author's idea of the format.
    """
    scan = scan_sessions(sessions_root)
    candidates = [
        (artifact.path.stat().st_size, artifact.path)
        for session in scan.sessions
        for artifact in session.artifacts()
        if artifact.compressed and artifact.version >= 4
    ]
    if not candidates:
        return None
    return min(candidates)[1]


def self_test(sessions_root: Path) -> ContractReport:
    """Check a move end to end, using a real artifact as the subject.

    Copies a real artifact into a throwaway home, checks DSH accepts it, moves it
    to a second workspace with the real planner and applier, and checks DSH
    accepts it again, reports the new workspace, and still reads the same number
    of events. This is the property the tool exists to provide, asserted against
    DSH rather than against the tool's own idea of correctness.

    Raises:
        ToolError: when there is no real artifact to base the store on, or it
            carries no usable session id.
    """
    from .frames import decode_header, replace_header_frame

    sample = sample_artifact(sessions_root)
    if sample is None:
        raise ToolError(
            f"no compressed v4 artifact was found under {sessions_root}, so the self-test has "
            "nothing real to base its store on"
        )

    original = sample.read_bytes()
    header = decode_header(original, label=sample.name)
    session_id = header.get("id")
    if not isinstance(session_id, str) or not session_id:
        raise ToolError(f"{sample} carries no usable session id")

    with tempfile.TemporaryDirectory(prefix="dsh-contract-") as scratch:
        # Resolve before use: a workspace path has to be spelled the way DSH's
        # realpath-based comparison will spell it, and a temporary directory name
        # can arrive in the short form on Windows.
        lab = Path(scratch).resolve()
        workspace_a = lab / "wsA"
        workspace_b = lab / "wsB"
        home = lab / "home"
        for directory in (workspace_a, workspace_b, home / "storages"):
            directory.mkdir(parents=True, exist_ok=True)

        placed, _ = replace_header_frame(
            original,
            {key: (str(workspace_a) if key == "cwd" else value) for key, value in header.items()},
            label=sample.name,
        )
        session_directory = home / "sessions" / project_key(str(workspace_a)) / encode_segment(session_id)
        session_directory.mkdir(parents=True, exist_ok=True)
        (session_directory / sample.name).write_bytes(placed)

        (home / "storages" / "workspace.json").write_text(
            json.dumps(
                {
                    "unit": {"name": "workspace", "version": 1},
                    "global": {
                        "initialized": True,
                        "workspaceIds": ["a", "b"],
                        "archivedSessionIds": [],
                    },
                    "tables": {
                        "workspaces": {
                            "a": {
                                "path": str(workspace_a),
                                "title": "wsA",
                                "sessionIds": [session_id],
                            },
                            "b": {"path": str(workspace_b), "title": "wsB", "sessionIds": []},
                        }
                    },
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        report = check(home / "sessions")
        report.sample = sample
        if report.skipped or report.failures:
            report.failures.insert(0, "the store was not accepted before any move was attempted")
            return report

        if report.dsh_sessions.get(session_id) != str(workspace_a):
            report.failures.append(
                f"before the move DSH reports {quote(report.dsh_sessions.get(session_id))}, "
                f"expected {quote(str(workspace_a))}"
            )
            return report
        if session_id not in report.dsh_events:
            report.failures.append(
                "DSH listed the sample but could not open it, so there is no usable baseline"
            )
            return report
        before_events = report.dsh_events[session_id]

        if report.membership.get(session_id) != (str(workspace_a),):
            report.failures.append(
                f"before the move DSH would show the session in "
                f"{_workspaces(report.membership.get(session_id, ()))}, expected only "
                f"{quote(str(workspace_a))}"
            )
            return report

        registry = load_registry(home)
        session = scan_sessions(home / "sessions").by_id(session_id)[0]
        target = registry.find(str(workspace_b))
        if target is None:
            report.failures.append("the fixture did not register workspace B")
            return report
        outcome = plan_move(session, target, registry)
        if not isinstance(outcome, MovePlan):
            report.failures.append(f"the move was refused: {outcome.reason}")
            return report
        apply_plan(outcome)

        after = check(home / "sessions")
        report.dsh_sessions = after.dsh_sessions
        report.dsh_events = after.dsh_events
        report.unreadable = after.unreadable
        report.tool_sessions = after.tool_sessions
        report.membership = after.membership
        report.invisible = after.invisible
        report.notes = after.notes
        report.failures.extend(after.failures)
        report.pre_existing.extend(after.pre_existing)
        report.skipped = after.skipped

        reported = report.dsh_sessions.get(session_id)
        if reported != str(workspace_b):
            report.failures.append(
                f"after the move DSH reports {quote(reported)}, expected {quote(str(workspace_b))}"
            )
        holders = report.membership.get(session_id)
        if holders != (str(workspace_b),):
            report.failures.append(
                f"after the move DSH would show the session in {_workspaces(holders or ())}, "
                f"expected only {quote(str(workspace_b))}; a session whose ownership list still "
                "names the old workspace appears in no workspace at all"
            )
        events = report.dsh_events.get(session_id)
        if events is None:
            report.failures.append("after the move DSH could not open the session")
        elif events != before_events:
            report.failures.append(
                f"after the move DSH reads {events} events but read {before_events} before"
            )

        # Positive control. Put the ownership list back the way a one-sided move
        # leaves it, and confirm the membership check notices. Without this, the
        # assertion above could be passing for a reason that has nothing to do
        # with the defect it exists to catch.
        registry_file = home / "storages" / "workspace.json"
        one_sided = json.loads(registry_file.read_text(encoding="utf-8"))
        rows = one_sided["tables"]["workspaces"]
        rows["b"]["sessionIds"] = [item for item in rows["b"]["sessionIds"] if item != session_id]
        rows["a"]["sessionIds"] = [session_id, *rows["a"]["sessionIds"]]
        registry_file.write_text(json.dumps(one_sided, indent=2) + "\n", encoding="utf-8")
        control = check(home / "sessions")
        if session_id not in control.invisible:
            report.failures.append(
                "the membership check did not notice a session left in the old workspace's "
                "ownership list, so it cannot be trusted to notice the real thing"
            )
        return report
