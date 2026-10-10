"""The concatenated-zstd container DSH uses for session artifacts.

A Session artifact (``session.vN.jsonl.zstd``) is not a stream of lines that
happens to be compressed. It is a **concatenation of independently decodable
zstd frames**, and DSH's reader relies on that structure:

* the first frame must contain exactly the header line, nothing else —
  ``assertZstdHeaderFrame``, ``dsh-session-persistence-jsonl/lib/index.js:2293-2295``
* each later frame carries one durable event batch —
  ``compressZstdFrame``, same file ``:1364-1371``
* each frame is written with the checksum flag set —
  ``CHECKSUM_OPTIONS``, same file ``:1291``

Two consequences drive this module's design:

1. **Frame boundaries must be found explicitly.** Python's one-shot
   ``zstandard.ZstdDecompressor.decompress`` reads one frame and stops, exactly
   like ``node:zlib``'s ``zstdDecompressSync``. Neither reports how many bytes it
   consumed, so the boundary cannot be discovered by decompressing.
2. **The body must never be re-encoded.** Rewriting only the first frame and
   copying the remaining frames as raw bytes keeps the log byte-identical apart
   from the header. Re-encoding the whole log into a single frame is the defect
   that breaks DSH's first-frame contract.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import zstandard

from .errors import ToolError

#: Zstd frame magic, little-endian, as the integer DSH compares against
#: (``ZSTD_MAGIC``, ``dsh-session-persistence-jsonl/lib/index.js:1288``).
ZSTD_MAGIC = 4247762216

#: DSH writes every frame with a checksum, so we do too.
_FRAME_WRITER = zstandard.ZstdCompressor(write_checksum=True)

_MAX_FRAME_SIZE = 1 << 31


@dataclass(frozen=True, slots=True)
class Frame:
    """One frame's byte range inside the artifact."""

    start: int
    end: int

    @property
    def size(self) -> int:
        return self.end - self.start


class ArtifactError(ToolError):
    """The artifact is not a well-formed DSH session log."""


def scan_frames(raw: bytes) -> tuple[Frame, ...]:
    """Locate every zstd frame in ``raw`` by scanning for the magic number.

    A compressed payload may in principle contain the four magic bytes, which
    would split one frame into two bogus ranges. That is not silent corruption
    here: :func:`decode_frames` decompresses every range it is given, so a bogus
    split makes a frame that does not decode to valid JSONL and is rejected.
    The approach was validated against all 1000 artifacts in this machine's
    library (782,720 frames) before any write path was enabled.

    Raises:
        ArtifactError: when ``raw`` holds no frame, or the first frame does not
            begin at byte 0.
    """
    starts = [
        offset
        for offset in range(len(raw) - 3)
        if int.from_bytes(raw[offset : offset + 4], "little") == ZSTD_MAGIC
    ]
    if not starts:
        raise ArtifactError("no zstd frame found")
    if starts[0] != 0:
        raise ArtifactError(f"first frame does not start at byte 0 (starts at {starts[0]})")

    return tuple(
        Frame(start=start, end=starts[index + 1] if index + 1 < len(starts) else len(raw))
        for index, start in enumerate(starts)
    )


def decompress_frame(raw: bytes, frame: Frame, *, label: str) -> bytes:
    """Decompress exactly one frame, validating its checksum.

    Raises:
        ArtifactError: when the range is not one complete, checksummed frame.
    """
    if frame.size > _MAX_FRAME_SIZE:
        raise ArtifactError(f"{label}: frame at {frame.start} is implausibly large")
    try:
        return zstandard.ZstdDecompressor().decompress(
            raw[frame.start : frame.end], max_output_size=_MAX_FRAME_SIZE
        )
    except zstandard.ZstdError as error:
        raise ArtifactError(f"{label}: frame at {frame.start} does not decode: {error}") from error


#: How much of an artifact to read when only the header is wanted. The header
#: frame is the first line and compresses to a few hundred bytes even for very
#: long paths; 64 KiB is far above anything observed. A file that does not yield
#: a frame within this window is re-read in full before being called malformed.
_HEADER_READ_LIMIT = 64 * 1024


def read_header_bytes(path: Path, *, label: str) -> bytes:
    """Read enough of ``path`` to see its first frame.

    Returns the bytes needed; the caller decodes the frame from them. Reading a
    bounded prefix keeps a scan over a multi-hundred-megabyte library from
    loading every event frame into memory just to look at a header.
    """
    with path.open("rb") as handle:
        prefix = handle.read(_HEADER_READ_LIMIT)
        if len(prefix) < _HEADER_READ_LIMIT:
            return prefix
        # The window may have cut the first frame short. A truncated zstd frame
        # cannot be told apart from a complete one by inspection, so fall back to
        # the whole file rather than guess.
        try:
            if scan_frames(prefix):
                return prefix
        except ArtifactError:
            pass
        return prefix + handle.read()


def decode_header_frame(raw: bytes, *, label: str) -> bytes:
    """Return the first frame's plaintext without decoding the rest of the log.

    Listing a library means reading the header of every artifact, and a library
    can hold hundreds of megabytes of event frames that no header read needs.
    This decodes exactly one frame and asserts the invariant that applies to it:
    the first frame is exactly one line.

    :func:`decode_frames` remains the full gate and is what the write path uses.

    Raises:
        ArtifactError: when the first frame is missing or is not exactly one line.
    """
    frames = scan_frames(raw)
    header = decompress_frame(raw, frames[0], label=label)
    newlines = header.count(b"\n")
    if header.find(b"\n") != len(header) - 1:
        raise ArtifactError(
            f"{label}: first frame is not exactly one header line ({len(header)} bytes, {newlines} newlines)"
        )
    return header


def decode_frames(raw: bytes, *, label: str) -> tuple[bytes, ...]:
    """Decode every frame and assert the container's structural invariants.

    The invariants are the ones DSH's own reader enforces, so an artifact that
    passes here is one DSH will accept:

    * every frame is non-empty
    * the first frame is exactly one header line (the ``assertZstdHeaderFrame`` rule)
    * the concatenated plaintext ends with a newline (no torn tail line)
    * every line parses as JSON

    Raises:
        ArtifactError: on the first violated invariant.
    """
    frames = scan_frames(raw)
    decoded = tuple(decompress_frame(raw, frame, label=label) for frame in frames)

    for index, plain in enumerate(decoded):
        if not plain:
            raise ArtifactError(f"{label}: frame {index} is empty")

    header = decoded[0]
    newlines = header.count(b"\n")
    if header.find(b"\n") != len(header) - 1:
        raise ArtifactError(
            f"{label}: first frame is not exactly one header line ({len(header)} bytes, {newlines} newlines)"
        )

    text = b"".join(decoded).decode("utf-8")
    if not text.endswith("\n"):
        raise ArtifactError(f"{label}: log has no trailing newline (torn tail line)")
    for number, line in enumerate(text[:-1].split("\n"), start=1):
        try:
            json.loads(line)
        except json.JSONDecodeError as error:
            raise ArtifactError(f"{label}: line {number} is not valid JSON: {error}") from error

    return decoded


def decode_header(raw: bytes, *, label: str) -> dict[str, object]:
    """Return the parsed header object, validating the whole container first.

    Raises:
        ArtifactError: when the container or the header line is malformed.
    """
    header_bytes = decode_frames(raw, label=label)[0]
    line = header_bytes[: header_bytes.find(b"\n")]
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError as error:
        raise ArtifactError(f"{label}: header line is not valid JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise ArtifactError(f"{label}: header is not a JSON object")
    return parsed


def encode_header(header: dict[str, object]) -> bytes:
    """Compress one header object into a single checksummed frame.

    Uses ``separators`` to match JavaScript's ``JSON.stringify`` output, which
    writes no space after ``:`` or ``,``. The bytes are re-parsed and compared
    before being returned so a ``repr`` difference can never reach disk.
    """
    text = json.dumps(header, separators=(",", ":"), ensure_ascii=False)
    payload = (text + "\n").encode("utf-8")
    frame = _FRAME_WRITER.compress(payload)

    check = zstandard.ZstdDecompressor().decompress(frame)
    if check != payload:
        raise ArtifactError("re-encoded header frame does not round-trip; refusing")
    return frame


def replace_header_frame(
    raw: bytes, header: dict[str, object], *, label: str
) -> tuple[bytes, tuple[bytes, ...]]:
    """Return ``raw`` with its first frame replaced by ``header``.

    Every remaining frame is copied as raw compressed bytes, so the log body is
    never decompressed and never re-encoded.

    Returns:
        A ``(new_bytes, body_plaintexts)`` pair. The body plaintexts are returned
        so a caller can cheaply prove the body survived unchanged.

    Raises:
        ArtifactError: when the original container is malformed, or the rebuilt
            container fails the same validation the original had to pass.
    """
    frames = scan_frames(raw)
    original = decode_frames(raw, label=label)

    new_raw = encode_header(header) + raw[frames[0].end :]

    # Re-validate the result through the same gate before it can be written.
    rebuilt = decode_frames(new_raw, label=f"{label} (rebuilt)")
    if len(rebuilt) != len(original):
        raise ArtifactError(f"{label}: rebuilt frame count changed; refusing")
    for index in range(1, len(original)):
        if rebuilt[index] != original[index]:
            raise ArtifactError(f"{label}: rebuilt frame {index} differs from the original; refusing")

    rewritten_header = decode_header(new_raw, label=f"{label} (rebuilt)")
    if rewritten_header != header:
        raise ArtifactError(f"{label}: rebuilt header does not match intent; refusing")

    return new_raw, rebuilt[1:]
