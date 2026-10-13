"""Command line interface.

Every subcommand is read-only except ``move`` and ``restore``, and both of those
back up before they write. ``move`` previews with ``--dry-run``.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from time import time_ns

from .config import Config
from .contract import check, self_test
from .errors import ToolError
from .frames import ArtifactError, decode_frames, decode_header
from .operations import (
    BACKUP_MARKER,
    MoveFailure,
    apply_plan,
    describe_mismatch,
    plan_move,
    verify_move,
)
from .paths import encode_segment, project_key
from .sessions import ScanResult, Session, parse_generation, scan_sessions
from .titles import one_line, session_title
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


def _display_title(session: Session) -> str:
    """The label a person scans for: the session's title, or a stated placeholder.

    DSH keeps a title as a ``session/title`` event in the log, not in the header,
    so reading it means reading the log (see :mod:`.titles`). A log that cannot be
    read says so, rather than presenting the session as untitled.
    """
    artifacts = session.artifacts()
    try:
        title = session_title(artifacts[0]) if artifacts else None
    except (ArtifactError, OSError):
        return "(title unreadable)"
    if title is None:
        return "(untitled)"
    return one_line(title) or "(untitled)"


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
            labels = {session.directory: _display_title(session) for session in sessions}
            for session in sorted(sessions, key=lambda item: labels[item.directory].casefold()):
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
                print(f"  {_short(labels[session.directory], 72)}{suffix}")
                print(f"      {session.session_id}")
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
    """Move one session to another workspace.

    Two things change together, and both are required: the header's ``cwd``, and
    the directory the artifacts live in. DSH derives the artifact path from the
    header and enforces that derivation on every read, so changing only the
    header makes the session a log DSH calls corrupt.
    """
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
        print(f"{outcome.session_id}: refused - {outcome.reason}")
        return 1

    changes = outcome.changes
    print(f"session   : {session.session_id}")
    print(f"directory : {session.directory}")
    print(f"artifacts : {', '.join(change.artifact.filename for change in changes)}")
    source = outcome.source
    print(f"cwd       : {session.cwd}   [{source.title if source else 'not a registered workspace'}]")
    print(f"target    : {target.path}   [{target.title}]")
    if outcome.relocates:
        print(f"relocate  : {outcome.source_directory.parent.name} -> {outcome.target_directory.parent.name}")
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
        print("\nnothing to do - the session already belongs to that workspace")
        return 0

    if args.dry_run:
        print("\ndry run - nothing was written. Re-run without --dry-run to apply.")
        return 0

    if dsh_looks_running():
        print("\nwarning: a DSH process looks alive. A live DSH appends to session logs,")
        print("         so writing now risks losing events. Close DSH first.")
        if not args.force:
            print("         Nothing was written. Re-run with --force to override.")
            return 1

    applied = apply_plan(outcome)
    for change in applied.changes:
        if change.backup is not None:
            print(f"  backed up: {change.backup}")

    directory = verify_move(session, target)
    print(f"\ndone - {session.session_id} is now stored under {target.title}.")
    print(f"       artifact : {directory}")
    print("       start DSH; the session should appear under that workspace.")

    leftovers = applied.leftovers
    if applied.moved:
        # The destination is verified above, so whatever is still in the old
        # directory is either a byte copy of what now lives here, or a file this
        # tool never owned. Only the first kind is removed.
        reclaimed, leftovers = reclaim_source_directory(
            applied.source_directory, keep_backups=args.keep_backups
        )
        if reclaimed:
            print(f"       reclaimed: {reclaimed}")

    if leftovers:
        print("\n       not touched, and not created by this tool:")
        for leftover in leftovers:
            print(f"         {leftover}")
    return 0


def reclaim_source_directory(directory: Path, *, keep_backups: bool) -> tuple[str | None, tuple[Path, ...]]:
    """Remove the emptied session directory, and the byte copies a move took.

    Only reached once the move has been verified, so the copies being removed are
    duplicates of bytes that are already readable at their destination. With
    ``keep_backups`` they stay behind, which is what ``restore`` wants them for.

    Returns:
        A description of what was removed, if anything, and whatever is genuinely
        still in the old directory.
    """
    if not directory.is_dir():
        return None, ()
    try:
        if not keep_backups:
            for entry in list(directory.iterdir()):
                if entry.is_file() and BACKUP_MARKER in entry.name:
                    entry.unlink()
        remaining = tuple(sorted(directory.iterdir()))
        if remaining:
            return None, remaining
        directory.rmdir()
        parent = directory.parent
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            return f"{parent.name} and its session directory are now empty and removed", ()
        return f"{directory.name} is now empty and removed", ()
    except OSError as error:
        return f"could not remove {directory}: {error}", ()


def cmd_restore(args: argparse.Namespace) -> int:
    """Undo a move from one ``.bak-<epoch-ms>`` copy.

    The backup holds the bytes as they were before a move, which means its header
    names the workspace the session came from. Writing those bytes back where the
    backup sits would produce exactly the state DSH calls corrupt, so the copy is
    placed where its own header says it belongs and the live artifact is moved
    aside first. Restoring therefore reverses both halves of a move.
    """
    source = Path(args.backup)
    if not source.is_file():
        raise ToolError(f"backup not found: {source}")
    if BACKUP_MARKER not in source.name:
        raise ToolError(f"refusing: {source.name} does not look like a {BACKUP_MARKER}<epoch-ms> file")
    if source.stat().st_size == 0:
        raise ToolError(f"refusing: {source} is empty")

    artifact_name = source.name.split(BACKUP_MARKER, 1)[0]
    if parse_generation(artifact_name) is None:
        raise ToolError(f"refusing: {artifact_name!r} is not a canonical artifact name")

    raw = source.read_bytes()
    try:
        header = decode_header(raw, label=artifact_name)
    except ArtifactError as error:
        raise ToolError(
            f"refusing: {source.name} does not hold a readable session header: {error}"
        ) from error

    session_id = header.get("id")
    cwd = header.get("cwd")
    if not isinstance(session_id, str) or not isinstance(cwd, str):
        raise ToolError(f"refusing: {source.name} has no usable id and cwd to place it by")

    destination = source.parent.parent.parent / project_key(cwd) / encode_segment(session_id) / artifact_name

    # DSH refuses a log when the same id appears under two project directories
    # (findLog, dsh-session-persistence-jsonl/lib/index.js:3370), so the live copy
    # has to leave the directory it currently occupies. Putting it back where the
    # restored bytes came from would only create the mirror of that problem, so it
    # is kept beside itself instead.
    live = _live_artifacts(session_id, exclude=source)
    if live and not args.discard_current:
        parked: list[Path] = []
        for path in live:
            # The destination is about to be overwritten, so it may stay put and
            # be replaced. Any other live copy would become a duplicate id.
            if path == destination:
                continue
            where = _park(path)
            if where is not None:
                parked.append(where)
        if parked:
            print("the copy that was current is kept as:")
            for path in parked:
                print(f"  {path}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(raw)
    source.unlink()
    print(f"restored {destination}")
    print(f"  from   {source}  (removed)")
    print(f"  cwd    {cwd}")
    return 0


def _live_artifacts(session_id: str, *, exclude: Path) -> tuple[Path, ...]:
    """Every canonical artifact of ``session_id`` in the current sessions root."""
    sessions_root = exclude.parent.parent.parent
    encoded = encode_segment(session_id)
    found: list[Path] = []
    if not sessions_root.is_dir():
        return ()
    for project in sessions_root.iterdir():
        candidate = project / encoded
        if not candidate.is_dir():
            continue
        for entry in candidate.iterdir():
            if entry.is_file() and parse_generation(entry.name) is not None:
                found.append(entry)
    return tuple(sorted(found))


def _park(path: Path) -> Path | None:
    """Move one live artifact aside under a backup name, returning where it went."""
    parked = path.with_name(f"{path.name}{BACKUP_MARKER}{time_ns() // 1_000_000}")
    try:
        path.replace(parked)
    except OSError:
        return None
    return parked


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


def cmd_contract(args: argparse.Namespace) -> int:
    """Check this tool's view of a store against what DSH itself accepts."""
    if args.self_test:
        config = Config.resolve(args.dsh_home)
        report = self_test(config.sessions_root)
        print("contract self-test: build a store, move a session, ask real DSH")
    else:
        config = Config.resolve(args.dsh_home)
        report = check(config.sessions_root, dsh_home_path=args.dsh_install)
        print(f"sessions root      : {report.sessions_root}")

    if report.skipped is not None:
        print(f"\nskipped: {report.skipped}")
        return 2

    print(f"DSH installation   : {report.dsh_root}")
    print(f"DSH lists          : {len(report.dsh_sessions)} session(s)")
    print(f"this tool sees     : {len(report.tool_sessions)} session(s)")
    print(f"DSH opened         : {len(report.dsh_events)} session(s)")

    print(f"\nfailures           : {len(report.failures)}")
    for message in report.failures:
        print(f"  ! {message}")

    if report.pre_existing:
        print(f"\nalready unopenable in this store ({len(report.pre_existing)}), not caused by this tool:")
        for message in report.pre_existing[:10]:
            print(f"  ? {message}")
        if len(report.pre_existing) > 10:
            print(f"  ? ... and {len(report.pre_existing) - 10} more")

    if report.ok:
        print("\nDSH and this tool agree on every session.")
        return 0
    return 1


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
    move.add_argument(
        "--keep-backups",
        action="store_true",
        help="keep the byte copies taken during the move instead of reclaiming them",
    )
    move.set_defaults(func=cmd_move)

    restore = subparsers.add_parser("restore", help="restore one artifact from its backup")
    restore.add_argument("backup", metavar="FILE.bak-MS", help="the backup file to restore from")
    restore.add_argument(
        "--discard-current",
        action="store_true",
        help="delete the artifact that is current instead of keeping it under a backup name",
    )
    restore.set_defaults(func=cmd_restore)

    doctor = subparsers.add_parser("doctor", help="report mismatches and unregistered cwds")
    doctor.set_defaults(func=cmd_doctor)

    contract = subparsers.add_parser(
        "contract",
        help="check this tool's view against what the installed DSH accepts",
    )
    contract.add_argument(
        "--dsh-install",
        metavar="PATH",
        help="the installed @deepseek-ai/dsh package to drive (default: found automatically)",
    )
    contract.add_argument(
        "--self-test",
        action="store_true",
        help="build a throwaway store, move a session in it, and ask DSH about the result",
    )
    contract.set_defaults(func=cmd_contract)

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
