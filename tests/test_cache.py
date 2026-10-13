"""Tests for the title cache.

The cache is an optimisation, so the properties that matter are the ones that
keep it from becoming a source of wrong answers: an entry is trusted only while
the artifact it describes is unchanged, a damaged cache reads as empty, and no
cache failure can fail a command.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import FakeHome

from dsh_session_tools.cache import TitleCache, default_cache_path
from dsh_session_tools.frames import ArtifactError
from dsh_session_tools.sessions import artifacts_of
from dsh_session_tools.titles import session_title

SESSION = "session-00000000-0000-0000-0000-0000000000c1"


def _artifact(home: FakeHome):
    alpha = home.workspace_dir("alpha")
    path = home.session(session_id=SESSION, cwd=str(alpha), title="cache me")
    return artifacts_of(path.parent)[0]


def test_a_round_trip_returns_the_stored_title(home: FakeHome, tmp_path: Path) -> None:
    artifact = _artifact(home)
    path = tmp_path / "titles.json"

    cache = TitleCache.load(path)
    assert cache.lookup(artifact) == (False, None)
    cache.store(artifact, "cache me")
    cache.save()

    reloaded = TitleCache.load(path)
    assert reloaded.lookup(artifact) == (True, "cache me")
    assert session_title(artifact, cache=reloaded) == "cache me"


def test_a_cached_answer_is_used_without_reading_the_log(home: FakeHome, tmp_path: Path) -> None:
    """The point of the cache: a usable entry means the log is not touched."""
    artifact = _artifact(home)
    raw = bytearray(artifact.path.read_bytes())
    raw[-1] ^= 0xFF
    artifact.path.write_bytes(bytes(raw))

    # Without a cache this artifact cannot answer at all, which is what makes the
    # cached answer below evidence of anything.
    with pytest.raises(ArtifactError):
        session_title(artifact)

    cache = TitleCache.load(tmp_path / "titles.json")
    cache.store(artifact, "from the cache")

    assert session_title(artifact, cache=cache) == "from the cache"
    assert cache.hits == 1


def test_a_changed_artifact_invalidates_its_entry(home: FakeHome, tmp_path: Path) -> None:
    artifact = _artifact(home)
    cache = TitleCache.load(tmp_path / "titles.json")
    cache.store(artifact, "stale")
    assert cache.lookup(artifact) == (True, "stale")

    # Appending changes both size and modification time, as DSH appending a batch
    # to a live log would.
    artifact.path.write_bytes(artifact.path.read_bytes() + b"x")

    assert cache.lookup(artifact) == (False, None)


def test_a_missing_file_reads_as_empty(tmp_path: Path) -> None:
    cache = TitleCache.load(tmp_path / "absent.json")
    assert cache.entries == {}


def test_a_corrupt_file_reads_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "titles.json"
    path.write_text("{ this is not json", encoding="utf-8")
    assert TitleCache.load(path).entries == {}


def test_a_foreign_format_is_discarded(tmp_path: Path) -> None:
    path = tmp_path / "titles.json"
    path.write_text(json.dumps({"format": 999, "entries": {"a": {"size": 1}}}), encoding="utf-8")
    assert TitleCache.load(path).entries == {}


def test_a_non_string_title_is_not_trusted(home: FakeHome, tmp_path: Path) -> None:
    """A hand-edited cache must not put a non-title into a title field."""
    artifact = _artifact(home)
    stat = artifact.path.stat()
    path = tmp_path / "titles.json"
    path.write_text(
        json.dumps(
            {
                "format": 1,
                "entries": {
                    str(artifact.path): {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "title": 7}
                },
            }
        ),
        encoding="utf-8",
    )

    assert TitleCache.load(path).lookup(artifact) == (False, None)


def test_saving_nothing_writes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "titles.json"
    TitleCache.load(path).save()
    assert not path.exists()


def test_a_save_that_cannot_write_does_not_raise(home: FakeHome, tmp_path: Path) -> None:
    """A cache problem must never fail the command that was reading titles."""
    artifact = _artifact(home)
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")

    cache = TitleCache.load(blocker / "titles.json")
    cache.store(artifact, "anything")
    cache.save()  # must not raise


def test_the_cache_path_depends_on_the_library(tmp_path: Path) -> None:
    one = default_cache_path(tmp_path / "sessions")
    two = default_cache_path(tmp_path / "other-sessions")
    assert one != two
    assert one.name.startswith("titles-") and one.name.endswith(".json")
