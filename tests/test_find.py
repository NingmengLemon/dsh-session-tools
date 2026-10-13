"""Tests for ``find``, the title search.

Titles are in the logs, so a search reads them. These tests pin the two things
that decide whether the result is usable: which sessions are in scope, and
whether the second run really reuses what the first one learned.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import FakeHome

from dsh_session_tools.cli import main

ALPHA_SESSION = "session-00000000-0000-0000-0000-0000000000d1"
BETA_SESSION = "session-00000000-0000-0000-0000-0000000000d2"
SUBAGENT = "session-00000000-0000-0000-0000-0000000000d3"


def build(home: FakeHome) -> tuple[Path, Path]:
    alpha = home.workspace_dir("alpha")
    beta = home.workspace_dir("beta")
    home.session(session_id=ALPHA_SESSION, cwd=str(alpha), title="Refactor the parser")
    home.session(session_id=BETA_SESSION, cwd=str(beta), title="Fix the lexer")
    home.session(session_id=SUBAGENT, cwd=str(alpha), title="Refactor subtask", origin="subagent")
    home.write_registry([("ws-alpha", str(alpha)), ("ws-beta", str(beta))])
    return alpha, beta


def run(home: FakeHome, *argv: str) -> int:
    return main(["--dsh-home", str(home.root), *argv])


def test_find_matches_a_title_fragment(home: FakeHome, capsys: pytest.CaptureFixture[str]) -> None:
    build(home)
    assert run(home, "find", "parser") == 0
    out = capsys.readouterr().out

    assert ALPHA_SESSION in out
    assert BETA_SESSION not in out


def test_find_is_case_insensitive(home: FakeHome, capsys: pytest.CaptureFixture[str]) -> None:
    build(home)
    assert run(home, "find", "LEXER") == 0
    assert BETA_SESSION in capsys.readouterr().out


def test_find_skips_subagents_unless_asked(home: FakeHome, capsys: pytest.CaptureFixture[str]) -> None:
    build(home)

    assert run(home, "find", "subtask") == 1
    capsys.readouterr()

    assert run(home, "find", "subtask", "--all") == 0
    assert SUBAGENT in capsys.readouterr().out


def test_find_with_no_match_exits_non_zero(home: FakeHome, capsys: pytest.CaptureFixture[str]) -> None:
    build(home)
    assert run(home, "find", "nothing like this exists") == 1
    assert "no title contains" in capsys.readouterr().out


def test_find_scopes_to_one_cwd(home: FakeHome, capsys: pytest.CaptureFixture[str]) -> None:
    alpha, _beta = build(home)

    # "the" appears in both titles, so the cwd is the only thing narrowing it.
    assert run(home, "find", "the", "--from", str(alpha)) == 0
    out = capsys.readouterr().out

    assert ALPHA_SESSION in out
    assert BETA_SESSION not in out


def test_find_reports_how_many_it_searched(home: FakeHome, capsys: pytest.CaptureFixture[str]) -> None:
    build(home)
    run(home, "find", "parser")
    assert "of 2 searched" in capsys.readouterr().out


def test_the_second_run_answers_from_the_cache(
    home: FakeHome, capsys: pytest.CaptureFixture[str], title_cache_dir: Path
) -> None:
    """Prove the second run reads the cache, by making the cache disagree.

    Damaging the logs cannot show this: that changes their size or modification
    time, which invalidates the entry by design. Rewriting the stored titles keeps
    every stat valid, so the answer the second run gives can only have come from
    the cache.
    """
    build(home)
    assert run(home, "find", "parser") == 0
    capsys.readouterr()

    cache_file = next(title_cache_dir.rglob("titles-*.json"))
    document = json.loads(cache_file.read_text(encoding="utf-8"))
    assert len(document["entries"]) == 2, "the first run should have read both top-level sessions"
    for entry in document["entries"].values():
        entry["title"] = "cached sentinel"
    cache_file.write_text(json.dumps(document), encoding="utf-8")

    assert run(home, "find", "cached sentinel") == 0
    assert capsys.readouterr().out.count("cached sentinel") >= 2

    # The logs still say "parser", so a search that read them would find it.
    assert run(home, "find", "parser") == 1


def test_no_cache_reads_the_logs_every_time(
    home: FakeHome, capsys: pytest.CaptureFixture[str], title_cache_dir: Path
) -> None:
    build(home)
    assert run(home, "--no-cache", "find", "parser") == 0
    capsys.readouterr()

    assert not list(title_cache_dir.rglob("titles-*.json")), "--no-cache still wrote a cache"


def test_find_reports_a_damaged_log_rather_than_matching_it(
    home: FakeHome, capsys: pytest.CaptureFixture[str], title_cache_dir: Path
) -> None:
    build(home)
    for artifact in home.sessions_root.rglob(f"{ALPHA_SESSION}/session*"):
        raw = bytearray(artifact.read_bytes())
        raw[-1] ^= 0xFF
        artifact.write_bytes(bytes(raw))

    assert run(home, "find", "parser") == 1
