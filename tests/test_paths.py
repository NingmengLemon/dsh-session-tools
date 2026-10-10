"""Tests for DSH's two on-disk encodings.

The vectors are real paths and session ids taken from this machine's session
library, plus the escape rules transcribed from the shipping implementation. A
wrong escape here would silently sweep the wrong directory, so these are worth
pinning exactly.
"""

from __future__ import annotations

import pytest

from dsh_session_tools.paths import encode_segment, project_dir, project_key, session_dir

# (cwd, project directory name) pairs observed in a real ~/.dsh/sessions tree.
PROJECT_VECTORS: list[tuple[str, str]] = [
    (r"E:\MyWorkspace\hello-sunnysport", "--E-MyWorkspace-hello-sunnysport--"),
    (r"E:\Projects\Android\BiliRoamingX", "--E-Projects-Android-BiliRoamingX--"),
    (r"E:\Projects\Android\EndfieldChargingAnim", "--E-Projects-Android-EndfieldChargingAnim--"),
    (r"E:\Projects\Python\UniversalWorkspace", "--E-Projects-Python-UniversalWorkspace--"),
    (r"E:\Projects\Python\lemony_rhythmgame_workspace", "--E-Projects-Python-lemony_rhythmgame_workspace--"),
    (r"\\wsl.localhost\Debian\home\lemon\swaptube", "--wsl.localhost-Debian-home-lemon-swaptube--"),
]


@pytest.mark.parametrize(("cwd", "expected"), PROJECT_VECTORS)
def test_project_key_matches_real_directories(cwd: str, expected: str) -> None:
    assert project_key(cwd) == expected


def test_project_key_collapses_separator_runs() -> None:
    # A run of separators becomes a single dash, and leading dashes are stripped.
    assert project_key("a//b") == "--a-b--"
    assert project_key("a\\\\b") == "--a-b--"
    assert project_key("/a/b") == "--a-b--"
    assert project_key("C:/a/b") == "--C-a-b--"


def test_project_key_escapes_unsafe_characters() -> None:
    # A space has no safe representation and becomes ~0020.
    assert project_key("/tmp/a b") == "--tmp-a~0020b--"
    # A literal tilde is escaped rather than passed through, so the encoding
    # stays unambiguous.
    assert project_key("/tmp/a~b") == "--tmp-a~007Eb--"


def test_project_key_never_returns_an_empty_stem() -> None:
    assert project_key("/") == "--root--"
    assert project_key("\\\\") == "--root--"


def test_project_key_rejects_empty_path() -> None:
    with pytest.raises(ValueError, match="empty project path"):
        project_key("")


def test_project_key_honours_fs_component_limit() -> None:
    key = project_key("/" + "a" * 400)
    assert len(key) == 255  # 2 dashes + 251 + 2 dashes


def test_encode_segment_passes_safe_ids_through() -> None:
    # Every id in a real library looks like this.
    for session_id in (
        "session-a5864bb4-04f5-4fb5-9ee4-becb0a7b5e22",
        "32ba2b7f-d352-4c2c-9dbd-a2edc7e09793",
        "263befcb-6843-4fdd-8175-8195c99caf56",
        "session.1_a-b",
    ):
        assert encode_segment(session_id) == session_id


def test_encode_segment_escapes() -> None:
    assert encode_segment("a/b") == "a~002Fb"
    assert encode_segment("a\\b") == "a~005Cb"
    assert encode_segment("a:b") == "a~003Ab"
    assert encode_segment("a~b") == "a~007Eb"
    assert encode_segment("é") == "~00E9"
    assert encode_segment("a b") == "a~0020b"


def test_encode_segment_reserved_names() -> None:
    # These two are special-cased ahead of the general escape loop.
    assert encode_segment(".") == "~002E"
    assert encode_segment("..") == "~002E~002E"


def test_encode_segment_rejects_empty() -> None:
    with pytest.raises(ValueError, match="empty path segment"):
        encode_segment("")


def test_directory_helpers_compose() -> None:
    from pathlib import Path

    root = Path("/sessions")
    assert project_dir(root, r"E:\a\b") == Path("/sessions/--E-a-b--")
    assert session_dir(root, r"E:\a\b", "session-1") == Path("/sessions/--E-a-b--/session-1")
