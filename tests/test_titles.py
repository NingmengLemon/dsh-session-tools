"""Tests for reading session titles from the log, and for showing them in ``list``.

DSH keeps a title as a ``session/title`` event in the log, never in the header, so
these tests build artifacts the way DSH writes them and assert the reader finds
the event rather than a header key.
"""

from __future__ import annotations

import json

import pytest
from conftest import FakeHome, title_event, write_artifact

from dsh_session_tools.cli import main
from dsh_session_tools.frames import ArtifactError
from dsh_session_tools.paths import encode_segment, project_key
from dsh_session_tools.sessions import artifacts_of, scan_sessions
from dsh_session_tools.titles import one_line, session_title

SESSION = "session-00000000-0000-0000-0000-0000000000cc"
UNTITLED = "session-00000000-0000-0000-0000-0000000000dd"


def _only_artifact(home: FakeHome, session_id: str):
    session = next(
        item for item in scan_sessions(home.sessions_root).sessions if item.session_id == session_id
    )
    return session, session.artifacts()[0]


def test_title_comes_from_the_log_and_the_header_has_none(home: FakeHome) -> None:
    alpha = home.workspace_dir("alpha")
    home.session(session_id=SESSION, cwd=str(alpha), title="Refactor the parser")

    session, artifact = _only_artifact(home, SESSION)
    assert "title" not in session.header
    assert session_title(artifact) == "Refactor the parser"


def test_the_last_title_event_wins(home: FakeHome) -> None:
    """A rename appends a new event; the earlier one stays in the log."""
    alpha = home.workspace_dir("alpha")
    home.session(
        session_id=SESSION,
        cwd=str(alpha),
        lines=[
            title_event("first draft", seq=2),
            '{"seq":3,"type":"turn/start"}',
            title_event("renamed by the user", seq=4),
        ],
    )

    _, artifact = _only_artifact(home, SESSION)
    assert session_title(artifact) == "renamed by the user"


def test_a_session_without_a_title_event_has_none(home: FakeHome) -> None:
    alpha = home.workspace_dir("alpha")
    home.session(session_id=UNTITLED, cwd=str(alpha))

    _, artifact = _only_artifact(home, UNTITLED)
    assert session_title(artifact) is None


def test_text_that_only_mentions_the_event_is_not_a_title(home: FakeHome) -> None:
    """A user message may quote the event name; only a real event counts."""
    alpha = home.workspace_dir("alpha")
    quoting = {"seq": 2, "type": "user/message", "data": {"text": 'why is "session/title" empty?'}}
    home.session(session_id=SESSION, cwd=str(alpha), lines=[json.dumps(quoting)])

    _, artifact = _only_artifact(home, SESSION)
    assert session_title(artifact) is None


def test_a_malformed_title_candidate_is_an_error_not_a_silent_miss(home: FakeHome) -> None:
    alpha = home.workspace_dir("alpha")
    home.session(session_id=SESSION, cwd=str(alpha), lines=['{"seq":2,"type":"session/title", broken'])

    _, artifact = _only_artifact(home, SESSION)
    with pytest.raises(ArtifactError, match="not valid JSON"):
        session_title(artifact)


def test_an_uncompressed_artifact_is_read_too(home: FakeHome) -> None:
    alpha = home.workspace_dir("alpha")
    directory = home.sessions_root / project_key(str(alpha)) / encode_segment(SESSION)
    header: dict[str, object] = {"type": "session", "version": 4, "id": SESSION, "cwd": str(alpha)}
    write_artifact(
        directory,
        filename="session.v4.jsonl",
        header=header,
        lines=[title_event("plain text log", seq=2)],
        compressed=False,
    )

    (artifact,) = artifacts_of(directory)
    assert session_title(artifact) == "plain text log"


def test_one_line_removes_controls_and_bidi_overrides() -> None:
    assert one_line("a\tb\u202ec\n d") == "a b c d"
    assert one_line("   ") == ""


def test_list_shows_the_title_above_the_id(home: FakeHome, capsys: pytest.CaptureFixture[str]) -> None:
    alpha = home.workspace_dir("alpha")
    home.write_registry([("ws-alpha", str(alpha))])
    home.session(session_id=SESSION, cwd=str(alpha), title="Refactor the parser")
    home.session(session_id=UNTITLED, cwd=str(alpha))

    assert main(["--dsh-home", str(home.root), "list", "--from", str(alpha)]) == 0
    out = capsys.readouterr().out

    assert out.index("Refactor the parser") < out.index(SESSION)
    assert "(untitled)" in out
    assert UNTITLED in out
