"""Tests for picking a session by id, id prefix or title, and for ordering in ``list``.

Every test runs against a throwaway DSH home from the ``home`` fixture, so the
developer's real library is never read here.
"""

from __future__ import annotations

import pytest
from conftest import FakeHome, title_event

from dsh_session_tools.cli import _find_session, main
from dsh_session_tools.errors import ToolError
from dsh_session_tools.sessions import scan_sessions
from dsh_session_tools.titles import TitleReader

FIRST = "session-11111111-aaaa-4aaa-8aaa-000000000001"
SECOND = "session-22222222-bbbb-4bbb-8bbb-000000000002"
THIRD = "session-33333333-cccc-4ccc-8ccc-000000000003"

#: Epoch milliseconds, so ordering tests can state which session is newer.
EARLY = 1_791_000_000_000
LATE = 1_791_500_000_000


def _populate(home: FakeHome) -> str:
    alpha = str(home.workspace_dir("alpha"))
    home.write_registry([("ws-alpha", alpha)])
    home.session(session_id=FIRST, cwd=alpha, title="Refactor the parser", extra_headers={"createdAt": EARLY})
    home.session(session_id=SECOND, cwd=alpha, title="Fix the lexer bug", extra_headers={"createdAt": LATE})
    home.session(session_id=THIRD, cwd=alpha, title="Write parser docs", extra_headers={"createdAt": LATE})
    return alpha


def test_an_exact_id_selects_its_session(home: FakeHome) -> None:
    _populate(home)
    scan = scan_sessions(home.sessions_root)
    assert _find_session(scan, FIRST, TitleReader()).session_id == FIRST


def test_a_unique_id_prefix_selects_its_session(home: FakeHome) -> None:
    _populate(home)
    scan = scan_sessions(home.sessions_root)
    assert _find_session(scan, "session-22222222", TitleReader()).session_id == SECOND


def test_a_title_fragment_selects_its_session_ignoring_case(home: FakeHome) -> None:
    _populate(home)
    scan = scan_sessions(home.sessions_root)
    assert _find_session(scan, "LEXER", TitleReader()).session_id == SECOND


def test_an_ambiguous_title_is_refused_with_the_candidates(home: FakeHome) -> None:
    _populate(home)
    scan = scan_sessions(home.sessions_root)
    with pytest.raises(ToolError) as caught:
        _find_session(scan, "parser", TitleReader())
    message = str(caught.value)
    assert "matches 2 sessions" in message
    assert FIRST in message
    assert THIRD in message


def test_an_unknown_selector_is_refused(home: FakeHome) -> None:
    _populate(home)
    scan = scan_sessions(home.sessions_root)
    with pytest.raises(ToolError, match="session not found"):
        _find_session(scan, "no-such-session", TitleReader())


def test_an_empty_selector_is_refused(home: FakeHome) -> None:
    _populate(home)
    scan = scan_sessions(home.sessions_root)
    with pytest.raises(ToolError, match="empty"):
        _find_session(scan, "", TitleReader())


def test_a_title_match_does_not_hide_an_exact_id(home: FakeHome) -> None:
    """The id stages run first, so an id is never overridden by a title match."""
    alpha = _populate(home)
    home.session(
        session_id="session-44444444-dddd-4ddd-8ddd-000000000004",
        cwd=alpha,
        lines=[
            title_event(f"mentions {FIRST} in its title", seq=2),
        ],
    )
    scan = scan_sessions(home.sessions_root)
    assert _find_session(scan, FIRST, TitleReader()).session_id == FIRST


def test_a_session_with_an_unreadable_log_does_not_block_a_title_match(home: FakeHome) -> None:
    alpha = _populate(home)
    broken = home.session(session_id="session-55555555-eeee-4eee-8eee-000000000005", cwd=alpha)
    raw = bytearray(broken.read_bytes())
    raw[-1] ^= 0xFF
    broken.write_bytes(bytes(raw))
    scan = scan_sessions(home.sessions_root)
    assert _find_session(scan, "lexer", TitleReader()).session_id == SECOND


def test_created_at_reads_the_header_epoch_milliseconds(home: FakeHome) -> None:
    _populate(home)
    scan = scan_sessions(home.sessions_root)
    session = _find_session(scan, FIRST, TitleReader())
    assert session.created_at is not None
    assert session.created_at.timestamp() == EARLY / 1000


def test_created_at_is_none_for_a_missing_or_absurd_value(home: FakeHome) -> None:
    alpha = str(home.workspace_dir("alpha"))
    home.session(session_id=FIRST, cwd=alpha, extra_headers={"createdAt": "yesterday"})
    home.session(session_id=SECOND, cwd=alpha, extra_headers={"createdAt": 10**30})
    scan = scan_sessions(home.sessions_root)
    assert _find_session(scan, FIRST, TitleReader()).created_at is None
    assert _find_session(scan, SECOND, TitleReader()).created_at is None


def test_list_shows_the_creation_time_and_puts_the_newest_first(
    home: FakeHome, capsys: pytest.CaptureFixture[str]
) -> None:
    alpha = _populate(home)
    assert main(["--dsh-home", str(home.root), "list", "--from", alpha]) == 0
    out = capsys.readouterr().out

    assert "created 2026-" in out
    # SECOND and THIRD share the later time; FIRST is older, so it comes last.
    assert out.index(FIRST) > out.index(SECOND)
    assert out.index(FIRST) > out.index(THIRD)
