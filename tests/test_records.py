"""Tests for ``show`` and ``list --json``, the two record-producing commands.

Both share one record builder, so these tests pin the fields a script would rely
on and the two states that are easy to get wrong: a session no workspace shows,
and a library with nothing in it.
"""

from __future__ import annotations

import json

import pytest
from conftest import FakeHome

from dsh_session_tools.cli import main

ALPHA_SESSION = "session-00000000-0000-0000-0000-0000000000e1"
BETA_SESSION = "session-00000000-0000-0000-0000-0000000000e2"


def build(home: FakeHome) -> tuple[str, str]:
    alpha = str(home.workspace_dir("alpha"))
    beta = str(home.workspace_dir("beta"))
    home.session(
        session_id=ALPHA_SESSION,
        cwd=alpha,
        title="Refactor the parser",
        extra_headers={"createdAt": 1_791_000_000_000},
    )
    home.session(
        session_id=BETA_SESSION,
        cwd=beta,
        title="Fix the lexer",
        extra_headers={"createdAt": 1_791_500_000_000},
    )
    home.write_registry([("ws-alpha", alpha), ("ws-beta", beta)])
    return alpha, beta


def run(home: FakeHome, *argv: str) -> int:
    return main(["--dsh-home", str(home.root), *argv])


def records(home: FakeHome, capsys: pytest.CaptureFixture[str], *argv: str) -> list[dict]:
    assert run(home, "list", "--json", *argv) == 0
    return json.loads(capsys.readouterr().out)


def test_list_json_emits_a_record_per_session(home: FakeHome, capsys: pytest.CaptureFixture[str]) -> None:
    alpha, _beta = build(home)
    rows = records(home, capsys)

    assert {row["id"] for row in rows} == {ALPHA_SESSION, BETA_SESSION}
    first = next(row for row in rows if row["id"] == ALPHA_SESSION)
    assert first["title"] == "Refactor the parser"
    assert first["cwd"] == alpha
    assert first["created"] == "2026-10-03T04:00:00.000Z"
    assert first["workspace"]["title"] == "alpha"
    assert first["artifacts"][0]["version"] == 4
    assert first["artifacts"][0]["compressed"] is True
    assert first["artifacts"][0]["bytes"] > 0
    assert first["generations"] == 1
    assert first["archived"] is False
    assert first["subagent"] is False
    assert first["directory_mismatch"] is None
    assert first["shown_in_no_workspace"] is None


def test_list_json_orders_newest_first(home: FakeHome, capsys: pytest.CaptureFixture[str]) -> None:
    build(home)
    rows = records(home, capsys)
    assert [row["id"] for row in rows] == [BETA_SESSION, ALPHA_SESSION]


def test_list_json_honours_from(home: FakeHome, capsys: pytest.CaptureFixture[str]) -> None:
    alpha, _beta = build(home)
    rows = records(home, capsys, "--from", alpha)
    assert [row["id"] for row in rows] == [ALPHA_SESSION]


def test_list_json_reports_a_session_no_workspace_shows(
    home: FakeHome, capsys: pytest.CaptureFixture[str]
) -> None:
    alpha, _beta = build(home)
    home.detach(ALPHA_SESSION)

    rows = records(home, capsys)
    first = next(row for row in rows if row["id"] == ALPHA_SESSION)
    assert first["shown_in_no_workspace"] is not None
    assert alpha not in str(first["shown_in_no_workspace"])


def test_list_json_of_an_empty_library_is_an_empty_array(
    home: FakeHome, capsys: pytest.CaptureFixture[str]
) -> None:
    home.write_registry([])
    assert run(home, "list", "--json") == 0
    assert json.loads(capsys.readouterr().out) == []


def test_list_json_from_an_unknown_cwd_exits_non_zero(
    home: FakeHome, capsys: pytest.CaptureFixture[str]
) -> None:
    build(home)
    assert run(home, "list", "--json", "--from", str(home.root / "nowhere")) == 1
    assert json.loads(capsys.readouterr().out) == []


def test_show_accepts_a_title_fragment(home: FakeHome, capsys: pytest.CaptureFixture[str]) -> None:
    build(home)
    assert run(home, "show", "lexer") == 0
    out = capsys.readouterr().out

    assert BETA_SESSION in out
    assert "Fix the lexer" in out
    assert "generations: 1" in out
    assert "frames" in out


def test_show_json_includes_frame_counts(home: FakeHome, capsys: pytest.CaptureFixture[str]) -> None:
    build(home)
    assert run(home, "show", ALPHA_SESSION, "--json") == 0
    record = json.loads(capsys.readouterr().out)

    assert record["id"] == ALPHA_SESSION
    assert isinstance(record["artifacts"][0]["frames"], int)
    assert record["artifacts"][0]["frames"] >= 1


def test_show_reports_a_damaged_artifact_without_failing(
    home: FakeHome, capsys: pytest.CaptureFixture[str]
) -> None:
    """Damage makes the title unreadable, and that is reported rather than fatal.

    The frame count still comes out, because counting frames walks the container's
    structure and does not decompress it. A checksum is only verified by decoding,
    which is what ``verify`` does and what a listing deliberately does not pay for.
    """
    build(home)
    artifact = next(home.sessions_root.rglob(f"{ALPHA_SESSION}/session*"))
    raw = bytearray(artifact.read_bytes())
    raw[-1] ^= 0xFF
    artifact.write_bytes(bytes(raw))

    assert run(home, "show", ALPHA_SESSION) == 0
    out = capsys.readouterr().out
    assert "(title unreadable)" in out
    assert "frames" in out


def test_show_refuses_an_unknown_selector(home: FakeHome) -> None:
    build(home)
    assert run(home, "show", "no such session") == 2
