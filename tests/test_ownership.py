"""Tests for the workspace registry's ownership list, the half a move can miss.

A session is visible in a workspace only when the workspace's stored
``sessionIds`` names it *and* its resolved ``cwd`` equals that workspace's stored
path (``dsh-workspace/lib/index.js:102-104``). A move that relocates the
artifacts and rewrites the header but leaves the ownership list alone therefore
produces a session that appears in no workspace at all, which is what these
tests pin down.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import FakeHome, workspace_at

from dsh_session_tools.cli import main
from dsh_session_tools.errors import ToolError
from dsh_session_tools.operations import (
    MovePlan,
    apply_plan,
    describe_invisible,
    plan_move,
    verify_ownership,
)
from dsh_session_tools.sessions import scan_sessions
from dsh_session_tools.workspaces import (
    Registry,
    apply_ownership,
    load_registry,
    plan_ownership,
    reclaim_registry_backup,
)

SESSION = "session-00000000-0000-0000-0000-0000000000ee"
OTHER = "session-00000000-0000-0000-0000-0000000000ef"
SUBAGENT = "session-00000000-0000-0000-0000-0000000000f0"


def build(home: FakeHome) -> tuple[Path, Path, Path]:
    """A session under alpha, plus beta and gamma as further workspaces."""
    alpha = home.workspace_dir("alpha")
    beta = home.workspace_dir("beta")
    gamma = home.workspace_dir("gamma")
    home.session(session_id=SESSION, cwd=str(alpha))
    home.session(session_id=OTHER, cwd=str(gamma))
    home.write_registry([("ws-alpha", str(alpha)), ("ws-beta", str(beta)), ("ws-gamma", str(gamma))])
    return alpha, beta, gamma


def document_of(home: FakeHome) -> dict:
    return json.loads(home.workspace_state.read_text(encoding="utf-8"))


def write_document(home: FakeHome, document: dict) -> None:
    home.workspace_state.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8", newline="\n")


def record_for(document: dict, path: Path) -> dict:
    for record in document["tables"]["workspaces"].values():
        if record["path"] == str(path):
            return record
    raise AssertionError(f"no workspace record for {path}")


def move(home: FakeHome, target: Path) -> None:
    """Run one real move, the way the CLI does but without the liveness probe."""
    registry = load_registry(home.root)
    session = scan_sessions(home.sessions_root).by_id(SESSION)[0]
    workspace = workspace_at(registry, target)
    plan = plan_move(session, workspace, registry)
    assert isinstance(plan, MovePlan), plan
    apply_plan(plan)
    verify_ownership(home.root, SESSION, workspace)


def make_ledger_stale(home: FakeHome, owner: Path) -> None:
    """Put the ownership list back the way a one-sided move leaves it."""
    home.detach(SESSION)
    home.attach(SESSION, str(owner))


def test_a_move_transfers_ownership(home: FakeHome) -> None:
    alpha, beta, _ = build(home)
    assert home.own_by_path(SESSION) == [str(alpha)]

    move(home, beta)

    assert home.own_by_path(SESSION) == [str(beta)]


def test_planning_alone_changes_nothing(home: FakeHome) -> None:
    """A dry run has to be able to show the registry edit without making it."""
    alpha, beta, _ = build(home)
    before = home.workspace_state.read_bytes()

    registry = load_registry(home.root)
    session = scan_sessions(home.sessions_root).by_id(SESSION)[0]
    plan = plan_move(session, workspace_at(registry, beta), registry)
    assert isinstance(plan, MovePlan)

    assert plan.ownership.needed
    assert home.workspace_state.read_bytes() == before
    assert home.own_by_path(SESSION) == [str(alpha)]


def test_a_stale_ownership_list_is_repaired_by_re_running_the_move(home: FakeHome) -> None:
    """The exact state a one-sided move leaves behind.

    The artifacts and the header are already correct, so nothing about the log
    needs to change; only the ownership list still names the old workspace. That
    is invisible in DSH rather than merely misplaced, so a re-run must not call it
    a no-op.
    """
    alpha, beta, _ = build(home)
    move(home, beta)
    make_ledger_stale(home, alpha)
    assert home.own_by_path(SESSION) == [str(alpha)]

    registry = load_registry(home.root)
    session = scan_sessions(home.sessions_root).by_id(SESSION)[0]
    target = workspace_at(registry, beta)
    plan = plan_move(session, target, registry)
    assert isinstance(plan, MovePlan)

    assert not plan.is_noop, "a stale ownership list is work, not a no-op"
    assert all(change.unchanged for change in plan.changes), "the artifact was already correct"
    assert [workspace.path for workspace in plan.ownership.detach_from] == [str(alpha)]
    assert plan.ownership.attach

    applied = apply_plan(plan)
    assert applied.ownership.changed
    verify_ownership(home.root, SESSION, target)
    assert home.own_by_path(SESSION) == [str(beta)]


def test_describe_invisible_names_the_stale_workspace(home: FakeHome) -> None:
    alpha, beta, _ = build(home)
    move(home, beta)
    make_ledger_stale(home, alpha)

    registry = load_registry(home.root)
    session = scan_sessions(home.sessions_root).by_id(SESSION)[0]
    reason = describe_invisible(session, registry)

    assert reason is not None
    # Plain quoting, so a Windows path reads as one path rather than two.
    assert str(alpha) in reason
    assert str(beta) in reason
    assert "\\\\" not in reason


def test_a_healthy_session_is_not_reported(home: FakeHome) -> None:
    alpha, _, _ = build(home)
    registry = load_registry(home.root)
    session = scan_sessions(home.sessions_root).by_id(SESSION)[0]

    assert home.own_by_path(SESSION) == [str(alpha)]
    assert describe_invisible(session, registry) is None


def test_an_unaccounted_session_is_reported(home: FakeHome) -> None:
    build(home)
    home.detach(SESSION)
    registry = load_registry(home.root)
    session = scan_sessions(home.sessions_root).by_id(SESSION)[0]

    assert home.own_by_path(SESSION) == []
    assert describe_invisible(session, registry) == "no workspace accounts for it, so DSH lists it nowhere"


def test_a_subagent_is_not_reported_as_invisible(home: FakeHome) -> None:
    """DSH nests a subagent under its parent and never accounts it in a workspace.

    Measured on the development library: none of its 194 subagent sessions
    appears in any workspace record. Counting that as invisible would turn the
    normal state into a false alarm, which is how a report stops being read.
    """
    alpha = home.workspace_dir("alpha")
    home.session(session_id=SUBAGENT, cwd=str(alpha), origin="subagent")
    home.write_registry([("ws-alpha", str(alpha))])
    home.detach(SUBAGENT)

    registry = load_registry(home.root)
    session = scan_sessions(home.sessions_root).by_id(SUBAGENT)[0]

    assert session.is_subagent
    assert home.own_by_path(SUBAGENT) == []
    assert describe_invisible(session, registry) is None


def test_double_accounting_is_reported_because_dsh_refuses_to_start(home: FakeHome) -> None:
    alpha, beta, _ = build(home)
    document = document_of(home)
    record = record_for(document, beta)
    record["sessionIds"] = [SESSION, *record["sessionIds"]]
    write_document(home, document)
    assert home.own_by_path(SESSION) == sorted([str(alpha), str(beta)])

    registry = load_registry(home.root)
    session = scan_sessions(home.sessions_root).by_id(SESSION)[0]
    reason = describe_invisible(session, registry)

    assert reason is not None
    assert "2 workspaces" in reason
    assert "refuses to start" in reason


def test_a_move_repairs_double_accounting(home: FakeHome) -> None:
    """DSH will not start with an id in two records; the move leaves it in one."""
    _alpha, beta, _ = build(home)
    document = document_of(home)
    record = record_for(document, beta)
    record["sessionIds"] = [SESSION, *record["sessionIds"]]
    write_document(home, document)

    move(home, beta)

    assert home.own_by_path(SESSION) == [str(beta)]


def test_the_rest_of_the_document_survives_the_edit(home: FakeHome) -> None:
    alpha, beta, gamma = build(home)
    before = document_of(home)

    move(home, beta)

    text = home.workspace_state.read_text(encoding="utf-8")
    assert text.endswith("}\n"), "DSH writes a trailing newline"
    after = document_of(home)
    assert after["unit"] == before["unit"]
    assert after["global"] == before["global"]
    assert set(after["tables"]["workspaces"]) == set(before["tables"]["workspaces"])

    rows_before = before["tables"]["workspaces"]
    rows_after = after["tables"]["workspaces"]
    touched = sorted(rows_after[key]["path"] for key in rows_after if rows_after[key] != rows_before[key])
    # Only the two records whose ownership changed are rewritten.
    assert touched == sorted([str(alpha), str(beta)])
    assert record_for(after, gamma)["sessionIds"] == record_for(before, gamma)["sessionIds"]


def test_an_edit_is_idempotent(home: FakeHome) -> None:
    _alpha, beta, _ = build(home)
    registry = load_registry(home.root)
    target = workspace_at(registry, beta)
    edit = plan_ownership(registry, SESSION, target)

    first = apply_ownership(home.root, edit)
    assert first.changed
    settled = home.workspace_state.read_bytes()

    again = plan_ownership(load_registry(home.root), SESSION, target)
    assert not again.needed
    second = apply_ownership(home.root, again)
    assert second.changed == ()
    assert home.workspace_state.read_bytes() == settled


def test_the_edit_refuses_a_target_whose_path_changed(home: FakeHome) -> None:
    """A registry rewritten in between must be detected, not overwritten."""
    _alpha, beta, _ = build(home)
    registry = load_registry(home.root)
    edit = plan_ownership(registry, SESSION, workspace_at(registry, beta))

    document = document_of(home)
    record_for(document, beta)["path"] = str(home.workspace_dir("moved-elsewhere"))
    write_document(home, document)

    with pytest.raises(ToolError, match="refusing to file the session by a stale path"):
        apply_ownership(home.root, edit)


def test_the_registry_backup_is_kept_until_it_is_reclaimed(home: FakeHome) -> None:
    _alpha, beta, _ = build(home)
    registry = load_registry(home.root)
    session = scan_sessions(home.sessions_root).by_id(SESSION)[0]
    plan = plan_move(session, workspace_at(registry, beta), registry)
    assert isinstance(plan, MovePlan)

    applied = apply_plan(plan)
    backup = applied.ownership.backup
    assert backup is not None and backup.is_file()
    assert json.loads(backup.read_text(encoding="utf-8"))["tables"]["workspaces"]

    reclaim_registry_backup(backup)
    assert not backup.exists()
    # Only the copy goes; the document itself is still there.
    assert home.workspace_state.is_file()


def test_a_dry_run_leaves_the_registry_untouched(home: FakeHome, capsys: pytest.CaptureFixture[str]) -> None:
    alpha, beta, _ = build(home)
    before = home.workspace_state.read_bytes()

    code = main(["--dsh-home", str(home.root), "move", "--session", SESSION, "--to", str(beta), "--dry-run"])
    assert code == 0
    out = capsys.readouterr().out

    assert "registry  :" in out
    assert home.workspace_state.read_bytes() == before
    assert home.own_by_path(SESSION) == [str(alpha)]


def test_doctor_counts_sessions_that_no_workspace_shows(
    home: FakeHome, capsys: pytest.CaptureFixture[str]
) -> None:
    alpha, beta, _ = build(home)
    move(home, beta)
    make_ledger_stale(home, alpha)

    assert main(["--dsh-home", str(home.root), "doctor"]) == 0
    out = capsys.readouterr().out

    assert "not visible in any workspace  : 1" in out
    assert SESSION in out


def test_restore_puts_ownership_back(home: FakeHome, capsys: pytest.CaptureFixture[str]) -> None:
    alpha, beta, _ = build(home)
    registry = load_registry(home.root)
    session = scan_sessions(home.sessions_root).by_id(SESSION)[0]
    plan = plan_move(session, workspace_at(registry, beta), registry)
    assert isinstance(plan, MovePlan)
    applied = apply_plan(plan)
    backup = applied.changes[0].backup
    assert backup is not None
    assert home.own_by_path(SESSION) == [str(beta)]

    assert main(["--dsh-home", str(home.root), "restore", str(backup)]) == 0
    capsys.readouterr()

    assert home.own_by_path(SESSION) == [str(alpha)]


def test_holders_and_registry_helpers_agree(home: FakeHome) -> None:
    """The registry view the reports use matches the raw document."""
    alpha, beta, _ = build(home)
    registry: Registry = load_registry(home.root)

    holders = registry.holders(SESSION)
    assert [workspace.path for workspace in holders] == [str(alpha)]
    assert registry.holders("session-nobody") == ()
    assert [workspace.path for workspace in holders] == home.own_by_path(SESSION)
    assert registry.home == home.root

    move(home, beta)
    assert [workspace.path for workspace in load_registry(home.root).holders(SESSION)] == [str(beta)]
