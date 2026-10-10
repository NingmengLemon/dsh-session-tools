"""Tests for discovery and for the move itself, against a throwaway DSH home.

The move tests assert the three properties that matter: only ``cwd`` changes, the
log body survives byte-for-byte, and the revert path restores the original bytes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeHome, workspace_at, write_artifact

from dsh_session_tools.errors import ToolError
from dsh_session_tools.frames import ArtifactError, decode_frames, decode_header, scan_frames
from dsh_session_tools.operations import (
    MoveFailure,
    MovePlan,
    apply_plan,
    describe_mismatch,
    plan_move,
    verify_move,
)
from dsh_session_tools.paths import encode_segment, project_key
from dsh_session_tools.sessions import artifacts_of, parse_generation, scan_sessions
from dsh_session_tools.workspaces import canonical, load_registry

SESSION = "session-00000000-0000-0000-0000-0000000000aa"


# --- discovery ----------------------------------------------------------------


def test_scan_finds_a_session(home: FakeHome) -> None:
    home.session(session_id=SESSION, cwd=str(home.workspace_dir("alpha")))
    scan = scan_sessions(home.sessions_root)
    assert [session.session_id for session in scan.sessions] == [SESSION]
    assert scan.sessions[0].version == 4
    assert not scan.problems


def test_scan_ignores_backups_and_temporaries(home: FakeHome) -> None:
    path = home.session(session_id=SESSION, cwd=str(home.workspace_dir("alpha")))
    # Files a move or an interrupted write could leave behind.
    (path.parent / f"{path.name}.bak-1791590354125").write_bytes(path.read_bytes())
    (path.parent / f"{path.name}.move-tmp").write_bytes(path.read_bytes())
    artifacts = artifacts_of(path.parent)
    assert [artifact.filename for artifact in artifacts] == [path.name]


def test_scan_reads_headers_without_validating_the_body(home: FakeHome) -> None:
    """Damage in the log body must not stop a scan from listing the session.

    A header read only decodes the first frame, so a corrupt event frame is
    invisible here by design — the cost of full validation on every list would be
    reading the whole library. The write path is where it has to be caught, and
    ``test_damaged_log_is_an_error_not_a_refusal`` covers that.
    """
    path = home.session(session_id=SESSION, cwd=str(home.workspace_dir("alpha")))
    raw = bytearray(path.read_bytes())
    raw[-1] ^= 0xFF
    path.write_bytes(bytes(raw))

    scan = scan_sessions(home.sessions_root)
    assert [session.session_id for session in scan.sessions] == [SESSION]
    assert not scan.problems


def test_scan_reports_an_unreadable_header_instead_of_crashing(home: FakeHome) -> None:
    """A damaged *header* frame is a scan problem, reported rather than raised."""
    path = home.session(session_id=SESSION, cwd=str(home.workspace_dir("alpha")))
    raw = bytearray(path.read_bytes())
    raw[6] ^= 0xFF  # inside the first frame
    path.write_bytes(bytes(raw))

    scan = scan_sessions(home.sessions_root)
    assert not scan.sessions
    assert len(scan.problems) == 1
    assert "does not decode" in scan.problems[0].reason


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("session.v4.jsonl.zstd", 4),
        ("session.v12.jsonl.zstd", 12),
        ("session.v3.jsonl", 3),
        ("session.jsonl.zstd", 0),
        ("session.jsonl", 0),
        ("session.v0.jsonl.zstd", None),  # version-zero-tagged names are not canonical
        ("session.v4.jsonl.zstd.bak-1", None),
        ("session.v4.jsonl.zstd.move-tmp", None),
        ("session.v4.jsonl.zstd.pre-sourcemigrate-1", None),
        ("other.jsonl.zstd", None),
    ],
)
def test_parse_generation(filename: str, expected: int | None) -> None:
    assert parse_generation(filename) == expected


# --- planning -----------------------------------------------------------------


def build(home: FakeHome, *, archived: list[str] | None = None) -> tuple[Path, Path]:
    alpha = home.workspace_dir("alpha")
    beta = home.workspace_dir("beta")
    home.session(session_id=SESSION, cwd=str(alpha), lines=['{"seq":1}', '{"seq":2}'])
    home.write_registry([("ws-alpha", str(alpha)), ("ws-beta", str(beta))], archived=archived)
    return alpha, beta


def test_plan_rewrites_only_cwd(home: FakeHome) -> None:
    _alpha, beta = build(home)
    registry = load_registry(home.root)
    scan = scan_sessions(home.sessions_root)
    session = scan.by_id(SESSION)[0]

    plan = plan_move(session, workspace_at(registry, beta), registry)
    assert isinstance(plan, MovePlan)
    assert not plan.is_noop

    change = plan.changes[0]
    before = decode_header(change.artifact.path.read_bytes(), label="before")
    after = decode_header(change.payload, label="after")
    assert {key for key in before if before[key] != after[key]} == {"cwd"}
    assert after["cwd"] == str(beta)
    assert after["version"] == before["version"]  # never re-stamped


def test_plan_does_not_touch_disk(home: FakeHome) -> None:
    _, beta = build(home)
    registry = load_registry(home.root)
    session = scan_sessions(home.sessions_root).by_id(SESSION)[0]
    artifact = session.artifacts()[0].path
    original = artifact.read_bytes()

    plan_move(session, workspace_at(registry, beta), registry)

    assert artifact.read_bytes() == original
    assert not list(artifact.parent.glob("*.bak-*"))


def test_apply_changes_only_the_header_and_keeps_body_frames(home: FakeHome) -> None:
    _, beta = build(home)
    registry = load_registry(home.root)
    session = scan_sessions(home.sessions_root).by_id(SESSION)[0]
    artifact = session.artifacts()[0].path
    before = artifact.read_bytes()

    plan = plan_move(session, workspace_at(registry, beta), registry)
    assert isinstance(plan, MovePlan)
    applied = apply_plan(plan)

    assert applied[0].backup is not None
    after = artifact.read_bytes()
    assert after != before

    before_frames = scan_frames(before)
    after_frames = scan_frames(after)
    assert len(before_frames) == len(after_frames)
    for index in range(1, len(before_frames)):
        assert (
            before[before_frames[index].start : before_frames[index].end]
            == after[after_frames[index].start : after_frames[index].end]
        )
    assert decode_header(after, label="after")["cwd"] == str(beta)
    verify_move(session, workspace_at(registry, beta))


def test_backup_restores_the_original_bytes(home: FakeHome) -> None:
    _, beta = build(home)
    registry = load_registry(home.root)
    session = scan_sessions(home.sessions_root).by_id(SESSION)[0]
    artifact = session.artifacts()[0].path
    before = artifact.read_bytes()

    plan = plan_move(session, workspace_at(registry, beta), registry)
    assert isinstance(plan, MovePlan)
    applied = apply_plan(plan)
    backup = applied[0].backup
    assert backup is not None

    artifact.write_bytes(backup.read_bytes())
    assert artifact.read_bytes() == before


def test_already_in_target_is_a_noop(home: FakeHome) -> None:
    alpha, _ = build(home)
    registry = load_registry(home.root)
    session = scan_sessions(home.sessions_root).by_id(SESSION)[0]

    plan = plan_move(session, workspace_at(registry, alpha), registry)
    assert isinstance(plan, MovePlan)
    assert plan.is_noop


def test_move_refuses_an_archived_session(home: FakeHome) -> None:
    _, beta = build(home, archived=[SESSION])
    registry = load_registry(home.root)
    session = scan_sessions(home.sessions_root).by_id(SESSION)[0]

    outcome = plan_move(session, workspace_at(registry, beta), registry)
    assert isinstance(outcome, MoveFailure)
    assert "archived" in outcome.reason

    allowed = plan_move(
        session,
        workspace_at(registry, beta),
        registry,
        allow_archived=True,
    )
    assert isinstance(allowed, MovePlan)


def test_move_refuses_a_subagent_session(home: FakeHome) -> None:
    alpha = home.workspace_dir("alpha")
    beta = home.workspace_dir("beta")
    home.session(session_id=SESSION, cwd=str(alpha), origin="subagent")
    home.write_registry([("a", str(alpha)), ("b", str(beta))])
    registry = load_registry(home.root)
    session = scan_sessions(home.sessions_root).by_id(SESSION)[0]

    outcome = plan_move(session, workspace_at(registry, beta), registry)
    assert isinstance(outcome, MoveFailure)
    assert "subagent" in outcome.reason


def test_move_refuses_a_missing_target_directory(home: FakeHome) -> None:
    alpha = home.workspace_dir("alpha")
    ghost = home.root / "workspaces" / "ghost"
    home.session(session_id=SESSION, cwd=str(alpha))
    home.write_registry([("a", str(alpha)), ("b", str(ghost))])
    registry = load_registry(home.root)
    session = scan_sessions(home.sessions_root).by_id(SESSION)[0]

    outcome = plan_move(session, workspace_at(registry, ghost), registry)
    assert isinstance(outcome, MoveFailure)
    assert "not an existing directory" in outcome.reason


def test_move_refuses_when_generations_disagree(home: FakeHome) -> None:
    """Two generations in one directory pointing at different cwds: refuse.

    Generations share a directory, so a mismatch is reachable when an earlier
    tool relocated some but not all of them.
    """
    alpha = home.workspace_dir("alpha")
    beta = home.workspace_dir("beta")
    third = home.workspace_dir("third")
    directory = home.sessions_root / project_key(str(alpha)) / encode_segment(SESSION)
    common = {
        "type": "session",
        "id": SESSION,
        "createdAt": 1791115958273,
        "isSeeded": False,
        "delegationDepth": 0,
    }
    write_artifact(
        directory,
        filename="session.v4.jsonl.zstd",
        header={**common, "version": 4, "cwd": str(alpha)},
        lines=['{"seq":1}'],
    )
    write_artifact(
        directory,
        filename="session.v3.jsonl.zstd",
        header={**common, "version": 3, "cwd": str(third)},
        lines=['{"seq":1}'],
    )
    home.write_registry([("a", str(alpha)), ("b", str(beta))])
    registry = load_registry(home.root)
    session = scan_sessions(home.sessions_root).by_id(SESSION)[0]
    assert len(session.artifacts()) == 2

    outcome = plan_move(session, workspace_at(registry, beta), registry)
    assert isinstance(outcome, MoveFailure)
    assert "refusing to guess" in outcome.reason


def test_move_rewrites_every_generation(home: FakeHome) -> None:
    alpha = home.workspace_dir("alpha")
    beta = home.workspace_dir("beta")
    home.session(session_id=SESSION, cwd=str(alpha), version=4)
    home.session(session_id=SESSION, cwd=str(alpha), version=3)
    home.write_registry([("a", str(alpha)), ("b", str(beta))])
    registry = load_registry(home.root)
    session = scan_sessions(home.sessions_root).by_id(SESSION)[0]
    assert len(session.artifacts()) == 2

    plan = plan_move(session, workspace_at(registry, beta), registry)
    assert isinstance(plan, MovePlan)
    assert len(plan.changes) == 2
    apply_plan(plan)

    for artifact in session.artifacts():
        assert decode_header(artifact.path.read_bytes(), label="after")["cwd"] == str(beta)


def test_damaged_log_is_an_error_not_a_refusal(home: FakeHome) -> None:
    """A body-corrupt log lists fine, then fails loudly when a move is planned.

    The distinction matters: a damaged log is not a policy refusal, and it must
    never be reported as one.
    """
    alpha = home.workspace_dir("alpha")
    beta = home.workspace_dir("beta")
    path = home.session(session_id=SESSION, cwd=str(alpha))
    home.write_registry([("a", str(alpha)), ("b", str(beta))])
    registry = load_registry(home.root)

    raw = bytearray(path.read_bytes())
    raw[-1] ^= 0xFF
    path.write_bytes(bytes(raw))

    session = scan_sessions(home.sessions_root).by_id(SESSION)[0]
    with pytest.raises(ArtifactError, match="does not decode"):
        plan_move(session, workspace_at(registry, beta), registry)


# --- registry -----------------------------------------------------------------


def test_registry_ignores_cached_session_lists(home: FakeHome) -> None:
    alpha, beta = build(home)
    registry = load_registry(home.root)
    assert registry.find(str(alpha)) is not None
    assert registry.find(str(beta)) is not None
    assert registry.find(str(home.root / "nope")) is None


def test_registry_reports_archive_membership(home: FakeHome) -> None:
    build(home, archived=[SESSION])
    registry = load_registry(home.root)
    assert registry.is_archived(SESSION)
    assert not registry.is_archived("other")


def test_registry_rejects_a_missing_document(home: FakeHome) -> None:
    with pytest.raises(ToolError, match="workspace state not found"):
        load_registry(home.root)


def test_canonical_is_stable_for_a_real_directory(home: FakeHome) -> None:
    alpha = home.workspace_dir("alpha")
    assert canonical(str(alpha)) == canonical(alpha)
    # A path that does not exist still resolves to something deterministic.
    assert canonical(str(home.root / "nope")) == canonical(str(home.root / "nope"))


# --- mismatch reporting -------------------------------------------------------


def test_describe_mismatch_detects_a_relocated_header(home: FakeHome) -> None:
    alpha = home.workspace_dir("alpha")
    beta = home.workspace_dir("beta")
    path = home.session(session_id=SESSION, cwd=str(alpha))
    home.write_registry([("a", str(alpha))])
    session = scan_sessions(home.sessions_root).by_id(SESSION)[0]
    assert describe_mismatch(session) is None

    # Rewrite cwd the way a naive tool would, without relocating the artifact.
    plan_header = decode_header(path.read_bytes(), label="t")
    from dsh_session_tools.frames import replace_header_frame

    rewritten, _ = replace_header_frame(path.read_bytes(), {**plan_header, "cwd": str(beta)}, label="t")
    path.write_bytes(rewritten)

    moved = scan_sessions(home.sessions_root).by_id(SESSION)[0]
    report = describe_mismatch(moved)
    assert report is not None
    assert "its cwd implies" in report
    # The session is still discoverable: DSH reads the header, not the location.
    assert moved.cwd == str(beta)


def test_write_artifact_uses_the_expected_directory(home: FakeHome) -> None:
    alpha = home.workspace_dir("alpha")
    path = home.session(session_id=SESSION, cwd=str(alpha))
    expected = home.sessions_root / project_key(str(alpha)) / encode_segment(SESSION)
    assert path.parent == expected
    assert decode_frames(path.read_bytes(), label="t")


def test_uncompressed_artifact_is_read_too(home: FakeHome) -> None:
    alpha = home.workspace_dir("alpha")
    path = write_artifact(
        home.sessions_root / project_key(str(alpha)) / encode_segment(SESSION),
        filename="session.jsonl",
        header={"type": "session", "version": 0, "id": SESSION, "cwd": str(alpha)},
        lines=['{"seq":1}'],
        compressed=False,
    )
    home.write_registry([("a", str(alpha))])
    scan = scan_sessions(home.sessions_root)
    assert [session.session_id for session in scan.sessions] == [SESSION]
    assert scan.sessions[0].version == 0
    assert path.read_text().endswith("\n")
