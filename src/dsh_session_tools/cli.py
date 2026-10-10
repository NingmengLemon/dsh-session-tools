"""Command line interface.

Every subcommand is read-only except ``move`` and ``restore``, and both of those
back up before they write. ``move`` previews with ``--dry-run``.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from .config import Config
from .errors import ToolError
from .frames import ArtifactError, decode_frames
from .operations import MoveFailure, apply_plan, describe_mismatch, plan_move, verify_move
from .sessions import ScanResult, Session, scan_sessions
from .workspaces import Registry, Workspace, canonical, load_registry

_PROGRAM = "dsh-sessions"


# --- small formatting helpers -------------------------------------------------


def _human_bytes(count: int) -> str:
    size = float(count)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"


def _short(text: str, width: int = 46) -> str:
    return text if len(text) <= width else text[: width - 1] + "\u2026"


def _headline(session: Session) -> str:
    """Best available human label for a session.

    DSH derives a display title from the first prompt and stores it durably in
    the header once it exists. A session that never got one is shown by id.
    """
    title = session.header.get("title")
    return title if isinstance(title, str) and title else session.session_id


# --- host process detection ---------------------------------------------------


def dsh_looks_running() -> bool:
    """Report whether a DSH host process appears to be running.

    Best effort, and deliberately biased toward false positives: the caller only
    uses it to print a warning. A live DSH holds session logs open for append, so
    writing while it runs is the one way this tool could lose data.
    """
    try:
        if sys.platform == "win32":
            completed = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "(Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" |"
                    " Select-Object -ExpandProperty CommandLine) -join [char]10",
                ],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        else:
            completed = subprocess.run(
                ["ps", "-Ao", "command"], capture_output=True, text=True, timeout=20, check=False
            )
    except (OSError, subprocess.SubprocessError):
        return False

    needle_a = "dsh" + chr(92) + "lib" + chr(92) + "bin.js"
    needle_b = "dsh/lib/bin.js"
    needle_c = "@deepseek-ai" + chr(92) + "dsh"
    needle_d = "@deepseek-ai/dsh"
    for line in completed.stdout.splitlines():
        if "-lstrip" in line or "Get-CimInstance" in line:
            continue
        if needle_a in line or needle_b in line or needle_c in line or needle_d in line:
            return True
    return False


# --- loading ------------------------------------------------------------------


def _load(config: Config) -> tuple[Registry, ScanResult]:
    registry = load_registry(config.dsh_home)
    return registry, scan_sessions(config.sessions_root)


def _find_session(scan: ScanResult, session_id: str) -> Session:
    matches = scan.by_id(session_id)
    if not matches:
        raise ToolError(f"session not found: {session_id}")
    if len(matches) > 1:
        locations = ", ".join(str(match.directory) for match in matches)
        raise ToolError(f"session id appears in {len(matches)} directories: {locations}")
    return matches[0]


def _resolve_target(registry: Registry, raw: str) -> Workspace:
    workspace = registry.find(raw)
    if workspace is None:
        listing = "\n".join(
            f"    {candidate.path}" for candidate in sorted(registry.workspaces, key=lambda w: w.path)
        )
        raise ToolError(f"not a registered workspace: {raw}\n  registered paths:\n{listing}")
    return workspace


# --- subcommands --------------------------------------------------------------


def cmd_verify(args: argparse.Namespace) -> int:
    """Check every artifact can be read frame-by-frame, the way DSH reads it."""
    config = Config.resolve(args.dsh_home)
    scan = scan_sessions(config.sessions_root)

    checked = 0
    total_frames = 0
    failures: list[str] = []

    for session in scan.sessions:
        for artifact in session.artifacts():
            if not artifact.compressed:
                continue
            try:
                frames = decode_frames(artifact.path.read_bytes(), label=artifact.filename, verify_lines=True)
            except (ArtifactError, OSError) as error:
                failures.append(f"{session.session_id} / {artifact.filename}: {error}")
                continue
            checked += 1
            total_frames += len(frames)

    print(f"artifacts checked : {checked}")
    print(f"frames decoded    : {total_frames}")
    print(f"failures          : {len(failures)}")
    for line in failures:
        print(f"  ! {line}")

    if scan.problems:
        print(f"\nunreadable sessions skipped: {len(scan.problems)}")
        for problem in scan.problems:
            print(f"  ? {problem.path}: {problem.reason}")

    return 1 if (failures or scan.problems) else 0


def cmd_list(args: argparse.Namespace) -> int:
    """Group sessions by their stored cwd, and show which workspace that is."""
    config = Config.resolve(args.dsh_home)
    registry, scan = _load(config)

    grouped: dict[str, list[Session]] = {}
    for session in scan.sessions:
        grouped.setdefault(session.cwd or "(no cwd)", []).append(session)

    if args.from_path:
        wanted = canonical(args.from_path)
        for cwd, sessions in grouped.items():
            if canonical(cwd) != wanted:
                continue
            workspace = registry.resolve_cwd(cwd)
            print(f"{len(sessions)} session(s) stored under {cwd}")
            print(f"  workspace: {workspace.title if workspace else 'NOT REGISTERED'}")
            print()
            for session in sorted(sessions, key=_headline):
                artifacts = session.artifacts()
                flagged = describe_mismatch(session)
                marks = []
                if registry.is_archived(session.session_id):
                    marks.append("archived")
                if session.is_subagent:
                    marks.append("subagent")
                if len(artifacts) > 1:
                    marks.append(f"{len(artifacts)} generations")
                if flagged:
                    marks.append(f"MISMATCH: {flagged}")
                suffix = f"   [{', '.join(marks)}]" if marks else ""
                print(f"  {session.session_id}")
                print(f"      {_short(_headline(session))}{suffix}")
            return 0
        print(f"no sessions stored under {args.from_path}")
        return 1

    print(f"workspace registry : {registry.source}")
    print(f"sessions root      : {config.sessions_root}")
    print(f"{len(registry.workspaces)} workspaces, {len(scan.sessions)} sessions\n")

    rows: list[tuple[int, str, str, bool]] = []
    for cwd, sessions in grouped.items():
        workspace = registry.resolve_cwd(cwd)
        rows.append((len(sessions), cwd, workspace.title if workspace else "", workspace is None))

    rows.sort(key=lambda row: (-row[0], row[1]))
    for count, cwd, title, unregistered in rows:
        marker = "  [not a registered workspace]" if unregistered else ""
        print(f"{count:>5}  {_short(cwd, 54):<54} {title}{marker}")

    unregistered_sessions = sum(row[0] for row in rows if row[3])
    unregistered_cwds = sum(1 for row in rows if row[3])
    print(
        f"\n{len(scan.sessions)} sessions total; "
        f"{unregistered_sessions} in {unregistered_cwds} unregistered cwd(s)"
    )
    print("use --from <path> to list one group in detail")
    return 0


def cmd_move(args: argparse.Namespace) -> int:
    """Move one session to another workspace by rewriting its header's cwd."""
    config = Config.resolve(args.dsh_home)
    registry, scan = _load(config)
    session = _find_session(scan, args.session)
    target = _resolve_target(registry, args.to)

    outcome = plan_move(
        session,
        target,
        registry,
        allow_subagent=args.allow_subagent,
        allow_archived=args.allow_archived,
    )

    if isinstance(outcome, MoveFailure):
        print(f"{outcome.session_id}: refused — {outcome.reason}")
        return 1

    changes = outcome.changes
    print(f"session   : {session.session_id}")
    print(f"directory : {session.directory}")
    print(f"artifacts : {', '.join(change.artifact.filename for change in changes)}")
    source = outcome.source
    print(f"cwd       : {session.cwd}   [{source.title if source else 'not a registered workspace'}]")
    print(f"target    : {target.path}   [{target.title}]")
    print()

    for change in changes:
        if change.unchanged:
            print(f"  {change.artifact.filename}: already correct, skipped")
        else:
            print(
                f"  {change.artifact.filename}: {change.old_cwd} -> {change.new_cwd}"
                f"   ({len(change.payload)} bytes, {change.frames} frames)"
            )

    if outcome.is_noop:
        print("\nnothing to do — the session already belongs to that workspace")
        return 0

    if args.dry_run:
        print("\ndry run — nothing was written. Re-run without --dry-run to apply.")
        return 0

    running = dsh_looks_running()
    if running:
        print("\nwarning: a DSH process looks alive. A live DSH appends to session logs,")
        print("         so writing now risks losing events. Close DSH first.")
        if not args.force:
            print("         Nothing was written. Re-run with --force to override.")
            return 1

    applied = apply_plan(outcome)
    for change in applied:
        if change.backup is not None:
            print(f"  backed up: {change.backup}")

    verify_move(session, target)
    print(f"\ndone — {session.session_id} now reports as belonging to {target.title}.")
    print("start DSH; the session should appear under that workspace.")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    """Put one artifact back from a ``.bak-<epoch-ms>`` sibling."""
    source = Path(args.backup)
    if not source.is_file():
        raise ToolError(f"backup not found: {source}")
    if not source.name.rsplit(".", 1)[-1].startswith("bak-"):
        raise ToolError(f"refusing: {source.name} does not look like a .bak-<epoch-ms> file")

    target = source.with_name(source.name.rsplit(".bak-", 1)[0])
    if source.stat().st_size == 0:
        raise ToolError(f"refusing: {source} is empty")

    current = target.read_bytes() if target.is_file() else None
    backup = source.read_bytes()
    if current == backup:
        print(f"{target} already matches {source.name}; nothing to do")
        return 0

    target.write_bytes(backup)
    print(f"restored {target} from {source.name}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Report sessions whose directory and header disagree, plus near-miss cwds."""
    config = Config.resolve(args.dsh_home)
    registry, scan = _load(config)

    mismatched = [session for session in scan.sessions if describe_mismatch(session)]
    print(f"sessions scanned            : {len(scan.sessions)}")
    print(f"directory/header mismatches : {len(mismatched)}")
    for session in mismatched[:20]:
        print(f"  {session.session_id}: {describe_mismatch(session)}")

    cwds = {session.cwd for session in scan.sessions if session.cwd}
    unregistered = sorted(cwd for cwd in cwds if registry.resolve_cwd(cwd) is None)
    print(f"\ndistinct cwds               : {len(cwds)}")
    print(f"unregistered cwds           : {len(unregistered)}")
    for cwd in unregistered:
        count = sum(1 for session in scan.sessions if session.cwd == cwd)
        exists = Path(cwd).is_dir()
        print(f"  {count:>4} session(s)  {cwd}   [{'exists' if exists else 'MISSING on disk'}]")

    archived = sum(1 for session in scan.sessions if registry.is_archived(session.session_id))
    print(f"\narchived sessions           : {archived}")
    print(f"archived ids with no session: {len(registry.archived_session_ids) - archived}")
    return 0


# --- entry point --------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_PROGRAM,
        description=(
            "Move DSH sessions between workspaces by rewriting the stored header's cwd. "
            "Nothing is deleted; the log body is never re-encoded."
        ),
    )
    parser.add_argument(
        "--dsh-home",
        metavar="PATH",
        help="DSH home directory (default: $DSH_HOME, else ~/.dsh)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    verify = subparsers.add_parser("verify", help="check every artifact decodes frame-by-frame")
    verify.set_defaults(func=cmd_verify)

    listing = subparsers.add_parser("list", help="group sessions by stored cwd")
    listing.add_argument("--from", dest="from_path", metavar="PATH", help="list one cwd group in detail")
    listing.set_defaults(func=cmd_list)

    move = subparsers.add_parser("move", help="move a session to another workspace")
    move.add_argument("--session", required=True, metavar="ID", help="session id to move")
    move.add_argument("--to", required=True, metavar="PATH", help="target workspace path")
    move.add_argument("--dry-run", action="store_true", help="show the plan without writing")
    move.add_argument("--force", action="store_true", help="proceed even if DSH looks alive")
    move.add_argument("--allow-subagent", action="store_true", help="allow moving a subagent session")
    move.add_argument("--allow-archived", action="store_true", help="allow moving an archived session")
    move.set_defaults(func=cmd_move)

    restore = subparsers.add_parser("restore", help="restore one artifact from its backup")
    restore.add_argument("backup", metavar="FILE.bak-MS", help="the backup file to restore from")
    restore.set_defaults(func=cmd_restore)

    doctor = subparsers.add_parser("doctor", help="report mismatches and unregistered cwds")
    doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ToolError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
