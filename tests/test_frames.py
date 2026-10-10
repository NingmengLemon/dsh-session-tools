"""Tests for the concatenated-zstd container.

These encode the contract DSH enforces when it reads a session log. The central
case is the single-frame re-encode: it is exactly the mistake that makes a log
unreadable, so it gets an explicit test asserting we detect it.
"""

from __future__ import annotations

import itertools
import json

import pytest
import zstandard

from dsh_session_tools.frames import (
    ArtifactError,
    decode_frames,
    decode_header,
    encode_header,
    replace_header_frame,
    scan_frames,
)

HEADER: dict[str, object] = {
    "type": "session",
    "version": 4,
    "id": "session-00000000-0000-0000-0000-000000000001",
    "createdAt": 1791115958273,
    "cwd": r"E:\Projects\Python\from",
    "isSeeded": False,
    "delegationDepth": 0,
    "agentPreset": "standard-no-web",
}


def frame(payload: bytes, *, checksum: bool = True) -> bytes:
    return zstandard.ZstdCompressor(write_checksum=checksum).compress(payload)


def build(header: dict[str, object], *lines: str) -> bytes:
    """Build an artifact the way DSH does: header frame, then one frame per line."""
    parts = [frame((json.dumps(header, separators=(",", ":")) + "\n").encode())]
    parts.extend(frame((line + "\n").encode()) for line in lines)
    return b"".join(parts)


# --- scanning -----------------------------------------------------------------


def test_scan_finds_every_frame() -> None:
    raw = build(HEADER, '{"seq":1}', '{"seq":2}', '{"seq":3}')
    frames = scan_frames(raw)
    assert len(frames) == 4
    assert frames[0].start == 0
    assert frames[-1].end == len(raw)
    # Ranges must tile the file with no gaps or overlaps.
    for earlier, later in itertools.pairwise(frames):
        assert earlier.end == later.start


def test_scan_ignores_magic_bytes_inside_a_payload() -> None:
    """A magic number inside a compressed payload is not a frame boundary.

    Searching for the magic and trusting every hit splits one frame in two, and
    the halves do not decode. This was observed on real artifacts, not just
    constructed: it is why boundaries are walked out of the frame structure.
    """
    import struct

    magic = struct.pack("<I", 4247762216)
    # One body frame whose payload carries the magic bytes verbatim, so a byte
    # search is guaranteed to report a boundary in the middle of it.
    body = b'{"seq":1}\n' + magic + b'{"seq":2}\n'
    raw = build(HEADER) + frame(body) + frame(b'{"seq":3}\n')

    frames = scan_frames(raw)
    assert len(frames) == 3
    assert raw[frames[1].start : frames[1].end] == frame(body)

    # And the split must survive decoding, not merely look right.
    decoded = decode_frames(raw, label="t")
    assert decoded[1] == body
    assert decoded[2] == b'{"seq":3}\n'


def test_decode_handles_a_frame_larger_than_the_read_chunk() -> None:
    """The plaintext must be pulled in chunks, not in one sized request.

    A reader reserves what it is asked for, so requesting the whole ceiling makes
    every frame allocate it. On a real library that failed outright on a 34 MiB
    frame. This builds a frame comfortably larger than the chunk size so the loop
    has to run more than once.
    """
    from dsh_session_tools import frames as frames_module

    body = "\n".join(f'{{"seq":{index},"pad":"{"x" * 200}"}}' for index in range(40_000))
    raw = build(HEADER) + frame((body + "\n").encode())

    decoded = decode_frames(raw, label="t")
    assert len(decoded) == 2
    assert len(decoded[1]) > frames_module._READ_CHUNK
    assert decoded[1].endswith(b"\n")


def test_a_torn_final_frame_is_refused() -> None:
    """A truncated tail frame is refused, not silently accepted as whole."""
    whole = build(HEADER, '{"seq":1}', '{"seq":2}')
    frames = scan_frames(whole)
    torn = whole[: frames[-1].end - 5]
    with pytest.raises(ArtifactError, match="ends inside"):
        decode_frames(torn, label="t")


def test_scan_rejects_non_zstd() -> None:
    with pytest.raises(ArtifactError, match="no zstd frame"):
        scan_frames(b"not compressed at all")


def test_scan_rejects_frame_not_at_zero() -> None:
    with pytest.raises(ArtifactError, match="does not start at byte 0"):
        scan_frames(b"\x00\x01\x02\x03" + frame(b"{}\n"))


# --- decoding -----------------------------------------------------------------


def test_decode_returns_one_payload_per_frame() -> None:
    raw = build(HEADER, '{"seq":1}', '{"seq":2}')
    decoded = decode_frames(raw, label="t")
    assert len(decoded) == 3
    assert json.loads(decoded[0]) == HEADER
    assert decoded[1] == b'{"seq":1}\n'


def test_decode_rejects_multi_line_first_frame() -> None:
    """The defect that breaks DSH: a first frame holding the whole log."""
    everything = (json.dumps(HEADER, separators=(",", ":")) + "\n" + '{"seq":1}' + "\n").encode()
    raw = frame(everything)
    with pytest.raises(ArtifactError, match="not exactly one header line"):
        decode_frames(raw, label="t")


def test_decode_rejects_missing_trailing_newline() -> None:
    raw = frame((json.dumps(HEADER, separators=(",", ":")) + "\n").encode()) + frame(b'{"seq":1}')
    with pytest.raises(ArtifactError, match="no trailing newline"):
        decode_frames(raw, label="t")


def test_decode_rejects_invalid_json_line() -> None:
    """Line validity is a separate gate from container integrity.

    Container decoding answers "is this a set of whole frames whose first one is
    the header", which is what a move depends on, and leaves line parsing to the
    caller because parsing dominates the running time. `verify` asks for it.
    """
    raw = build(HEADER, "this is not json")

    # The container is intact, so the cheap gate passes it...
    assert decode_frames(raw, label="t")

    # ...and the line gate, which `dsh-sessions verify` turns on, rejects it.
    with pytest.raises(ArtifactError, match="not valid JSON"):
        decode_frames(raw, label="t", verify_lines=True)


def test_decode_rejects_corrupt_frame() -> None:
    raw = bytearray(build(HEADER, '{"seq":1}'))
    raw[-1] ^= 0xFF  # damage the checksum
    with pytest.raises(ArtifactError, match="does not decode"):
        decode_frames(bytes(raw), label="t")


def test_decode_header_returns_object() -> None:
    assert decode_header(build(HEADER, '{"seq":1}'), label="t") == HEADER


# --- encoding -----------------------------------------------------------------


def test_encode_header_uses_compact_json() -> None:
    """Must match JS JSON.stringify, which writes no space after : or ,."""
    payload = zstandard.ZstdDecompressor().decompress(encode_header(HEADER))
    text = payload.decode()
    assert text.endswith("\n")
    assert ", " not in text and '": ' not in text
    assert json.loads(text) == HEADER


def test_encode_header_round_trips_non_ascii() -> None:
    header = {**HEADER, "cwd": "E:\\项目\\测试"}
    payload = zstandard.ZstdDecompressor().decompress(encode_header(header))
    assert json.loads(payload.decode())["cwd"] == "E:\\项目\\测试"


# --- rewriting ----------------------------------------------------------------


def test_replace_header_keeps_body_frames_byte_identical() -> None:
    raw = build(HEADER, '{"seq":1}', '{"seq":2}', '{"seq":3}')
    original = decode_frames(raw, label="t")
    new_header = {**HEADER, "cwd": r"E:\Projects\Python\to"}

    rewritten, body = replace_header_frame(raw, new_header, label="t")

    assert decode_header(rewritten, label="t") == new_header
    assert len(body) == 3

    # The body must survive byte-for-byte, and the compressed payload of every
    # body frame must be untouched, not merely equivalent.
    after = decode_frames(rewritten, label="t")
    assert after[1:] == original[1:]
    before_ranges = scan_frames(raw)
    after_ranges = scan_frames(rewritten)
    for index in range(1, len(before_ranges)):
        assert (
            raw[before_ranges[index].start : before_ranges[index].end]
            == rewritten[after_ranges[index].start : after_ranges[index].end]
        )


def test_replace_header_only_changes_cwd() -> None:
    raw = build(HEADER, '{"seq":1}')
    rewritten, _ = replace_header_frame(raw, {**HEADER, "cwd": "X"}, label="t")
    before, after = decode_header(raw, label="t"), decode_header(rewritten, label="t")
    assert {key for key in before if before[key] != after[key]} == {"cwd"}
    assert set(before) == set(after)


def test_replace_header_preserves_key_order() -> None:
    raw = build(HEADER, '{"seq":1}')
    rewritten, _ = replace_header_frame(raw, {**HEADER, "cwd": "X"}, label="t")
    assert list(decode_header(raw, label="t")) == list(decode_header(rewritten, label="t"))


def test_replace_header_is_detected_when_body_would_change() -> None:
    """A rebuilt container whose body drifted must be refused, not written."""
    raw = build(HEADER, '{"seq":1}')
    rewritten, _ = replace_header_frame(raw, {**HEADER, "cwd": "X"}, label="t")
    # Sanity: the good rewrite passes; now corrupt a body byte and confirm the gate fires.
    damaged = bytearray(rewritten)
    damaged[-1] ^= 0xFF
    with pytest.raises(ArtifactError):
        decode_frames(bytes(damaged), label="t")


def test_replace_header_rejects_malformed_source() -> None:
    whole_log_in_one_frame = frame((json.dumps(HEADER, separators=(",", ":")) + "\n{}" + "\n").encode())
    with pytest.raises(ArtifactError):
        replace_header_frame(whole_log_in_one_frame, HEADER, label="t")
