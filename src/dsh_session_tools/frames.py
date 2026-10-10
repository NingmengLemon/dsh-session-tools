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

Frame boundaries are found by **walking each frame's own structure**, which is
what DSH does (``scanZstdFrames``, same file ``:1300-1363``). The frame header
declares how long its header fields are, and each 3-byte block header declares
how long its payload is, so the end of a frame is arithmetic rather than a
guess.

The obvious shortcut, searching for the 4-byte zstd magic number and treating
every hit as a boundary, is unsound: those bytes occur inside compressed
payloads. On this machine's library that false positive is not hypothetical, it
was observed on real artifacts, and it splits one frame into two ranges that do
not decode. Searching is also what makes a scan slow. Walking is both exact and
linear.

Two consequences drive the rest of this module:

1. **The body must never be re-encoded.** Rewriting only the first frame and
   copying the remaining frames as raw bytes keeps the log byte-identical apart
   from the header. Re-encoding the whole log into a single frame is the defect
   that breaks DSH's first-frame contract.
2. **A frame's plaintext is streamed, not reserved.** Asking a decompressor for
   a fixed output ceiling makes it reserve that much memory per frame, which
   turns a routine read of a large real library into a spurious out-of-memory
   failure.
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

#: The magic as it appears on disk.
_MAGIC_BYTES = ZSTD_MAGIC.to_bytes(4, "little")

#: DSH writes every frame with a checksum, so we do too.
_FRAME_WRITER = zstandard.ZstdCompressor(write_checksum=True)

#: Refuse to materialise one frame's plaintext beyond this. A single frame holds
#: one durable event batch, so a size in this range means a decompression bomb or
#: a damaged log rather than a session someone actually had. The limit bounds
#: memory; it does not mirror a DSH rule, since DSH imposes no per-frame cap.
_MAX_FRAME_PLAINTEXT = 1 << 30

#: Block type 1 is a single raw byte, and ignores the declared size
#: (RFC 8878 section 3.1.1.2). Type 3 is reserved.
_BLOCK_TYPE_RLE = 1
_BLOCK_TYPE_RESERVED = 3

#: Frame-header descriptor tables (RFC 8878 section 3.1.1.1.1).
_DICT_ID_SIZE = (0, 1, 2, 4)
_FCS_SIZE_TABLE = (0, 2, 4, 8)


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
    """Return every frame's byte range, in order.

    Raises:
        ArtifactError: when the bytes are not a clean concatenation of complete
            frames, or the log does not begin with one.
    """
    if not raw:
        raise ArtifactError("no zstd frame found")
    if not raw.startswith(_MAGIC_BYTES):
        first = raw.find(_MAGIC_BYTES)
        if first == -1:
            raise ArtifactError("no zstd frame found")
        raise ArtifactError(f"first frame does not start at byte 0 (starts at {first})")
    return tuple(Frame(start=start, end=end) for start, end in _walk_frames(raw, label="log"))


def _walk_frames(raw: bytes, *, label: str) -> list[tuple[int, int]]:
    """Walk the concatenation, returning each ``(start, end)``.

    A torn final frame is refused rather than partially accepted. DSH recovers
    one, because it may be looking at a log a crashed writer left behind; this
    tool refuses, because every operation here rewrites a log, and a partial
    frame is not something to rewrite around.
    """
    total = len(raw)
    frames: list[tuple[int, int]] = []
    position = 0
    while position < total:
        start = position
        position = _walk_one_frame(raw, start=start, label=label)
        frames.append((start, position))
    return frames


def _walk_one_frame(raw: bytes, *, start: int, label: str) -> int:
    """Return the offset just past the frame beginning at ``start``."""
    total = len(raw)
    position = start
    if position + 4 > total or raw[position : position + 4] != _MAGIC_BYTES:
        raise ArtifactError(f"{label}: no frame magic at byte {position}")

    position += 4
    if position >= total:
        raise ArtifactError(f"{label}: frame at {start} ends inside its own header")
    descriptor = raw[position]
    position += 1
    if descriptor & 0x08:
        raise ArtifactError(f"{label}: frame at {start} sets a reserved header bit")

    content_size_flag = (descriptor >> 6) & 0x03
    single_segment = (descriptor >> 5) & 0x01
    has_checksum = bool((descriptor >> 2) & 0x01)
    dictionary_size = _DICT_ID_SIZE[descriptor & 0x03]
    if content_size_flag == 0:
        # A single-segment frame always carries its content size, in one byte.
        content_size_size = 1 if single_segment else 0
    else:
        content_size_size = _FCS_SIZE_TABLE[content_size_flag]
    window_size = 0 if single_segment else 1

    position += dictionary_size + window_size + content_size_size
    if position > total:
        raise ArtifactError(f"{label}: frame at {start} ends inside its header")

    while True:
        if position + 3 > total:
            raise ArtifactError(f"{label}: frame at {start} ends inside a block header")
        header = raw[position] | (raw[position + 1] << 8) | (raw[position + 2] << 16)
        position += 3
        last_block = bool(header & 0x01)
        block_type = (header >> 1) & 0x03
        block_size = header >> 3
        if block_type == _BLOCK_TYPE_RESERVED:
            raise ArtifactError(f"{label}: frame at {start} uses a reserved block type")
        position += 1 if block_type == _BLOCK_TYPE_RLE else block_size
        if position > total:
            raise ArtifactError(f"{label}: frame at {start} ends inside a block payload")
        if last_block:
            break

    if has_checksum:
        position += 4
        if position > total:
            raise ArtifactError(f"{label}: frame at {start} ends inside its checksum")
    return position


def decompress_frame(raw: bytes, frame: Frame, *, label: str) -> bytes:
    """Decompress one frame, validating its checksum.

    The plaintext is read through a stream rather than requested with a fixed
    ``max_output_size``, because that ceiling is reserved per frame and a real
    library contains frames whose plaintext runs to hundreds of megabytes.

    Raises:
        ArtifactError: when the frame does not decode, is truncated, or its
            plaintext would exceed :data:`_MAX_FRAME_PLAINTEXT`.
    """
    blob = raw[frame.start : frame.end]
    try:
        reader = zstandard.ZstdDecompressor().stream_reader(blob)
        with reader:
            plain = reader.read(_MAX_FRAME_PLAINTEXT + 1)
    except zstandard.ZstdError as error:
        raise ArtifactError(f"{label}: frame at {frame.start} does not decode: {error}") from error
    except MemoryError as error:
        raise ArtifactError(
            f"{label}: not enough memory to decode the frame at {frame.start}; close other programs and retry"
        ) from error

    if len(plain) > _MAX_FRAME_PLAINTEXT:
        raise ArtifactError(
            f"{label}: frame at {frame.start} expands beyond {_MAX_FRAME_PLAINTEXT} bytes; refusing"
        )
    if not plain:
        raise ArtifactError(f"{label}: frame at {frame.start} is empty")
    return plain


def decode_frames(raw: bytes, *, label: str, verify_lines: bool = False) -> tuple[bytes, ...]:
    """Decode every frame and assert the container's structural invariants.

    The invariants are the ones DSH's own reader enforces, so an artifact that
    passes here is one DSH will accept:

    * the bytes are a clean concatenation of complete frames
    * every frame is non-empty and every checksum verifies
    * the first frame is exactly one header line (the ``assertZstdHeaderFrame`` rule)
    * the concatenated plaintext ends with a newline (no torn tail line)

    Note which invariant is *not* here: a frame boundary is not required to fall
    on a line boundary. DSH writes one batch per frame, and a batch holds many
    newline-separated events, so most real frames carry several lines.

    Args:
        verify_lines: also parse every line as JSON. That is the difference
            between proving the container is intact and proving its contents are
            well-formed, and it costs most of the running time: parsing dominated
            everything else on a measured sample. Callers that merely need the
            header or the frame ranges leave it off, which is why a move no
            longer pays for it. ``dsh-sessions verify`` turns it on.

    Raises:
        ArtifactError: on the first violated invariant.
    """
    frames = scan_frames(raw)
    decoded = tuple(decompress_frame(raw, frame, label=label) for frame in frames)

    _assert_header_frame(decoded[0], label=label)

    text = b"".join(decoded)
    if not text.endswith(b"\n"):
        raise ArtifactError(f"{label}: log has no trailing newline (torn tail line)")

    if verify_lines:
        for number, line in enumerate(text[:-1].split(b"\n"), start=1):
            try:
                json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise ArtifactError(f"{label}: line {number} is not valid JSON: {error}") from error

    return decoded


def _assert_header_frame(plain: bytes, *, label: str) -> None:
    """Assert DSH's ``assertZstdHeaderFrame`` rule: one line, nothing else."""
    newlines = plain.count(b"\n")
    if plain.find(b"\n") != len(plain) - 1:
        raise ArtifactError(
            f"{label}: first frame is not exactly one header line ({len(plain)} bytes, {newlines} newlines)"
        )


#: How much of an artifact to read when only the header is wanted. The header
#: frame is the first line and compresses to a few hundred bytes even for very
#: long paths, so one read settles it in practice; the window grows for the rare
#: artifact whose first frame is large.
_HEADER_READ_LIMIT = 64 * 1024


def read_header_bytes(path: Path, *, label: str) -> bytes:
    """Read exactly the bytes of ``path``'s first frame.

    Listing a library means reading the header of every artifact, and a library
    can hold hundreds of megabytes of event frames that no header read needs.
    The first frame's extent is arithmetic on its own header, so this returns
    that frame and nothing more.

    The window grows rather than assuming the header is small: a real artifact
    can carry a first frame of hundreds of megabytes, and the walk has to see
    the whole of it to report where it ends.
    """
    window = _HEADER_READ_LIMIT
    with path.open("rb") as handle:
        while True:
            handle.seek(0)
            prefix = handle.read(window)
            whole_file = len(prefix) < window
            try:
                end = _walk_one_frame(prefix, start=0, label=label)
                if end <= len(prefix):
                    return prefix[:end]
            except ArtifactError:
                if whole_file:
                    # The file is malformed and fully in hand; hand it over so the
                    # caller's assertions produce the diagnosis.
                    return prefix
            if whole_file:
                # The first frame runs past the end of the file.
                return prefix
            window *= 8


def decode_header_frame(raw: bytes, *, label: str) -> bytes:
    """Return the first frame's plaintext, without decoding the rest of the log.

    :func:`decode_frames` remains the full gate and is what the write path uses.

    Raises:
        ArtifactError: when the first frame is missing or is not exactly one line.
    """
    frames = scan_frames(raw)
    header = decompress_frame(raw, frames[0], label=label)
    _assert_header_frame(header, label=label)
    return header


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

    check = decompress_frame(frame, Frame(start=0, end=len(frame)), label="re-encoded header")
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
