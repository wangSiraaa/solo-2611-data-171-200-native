"""Low-level Zarr v2 chunk codec helpers (executor side).

The executor implements zlib handling directly so that:

* truncation is detected (``zlib.decompress`` silently accepts a truncated
  stream and the streaming decoder has to be asked about ``eof``);
* decode / encode counters are exact and centralised.

Decoded chunks are always C-order ``<i4`` arrays whose shape equals the
*declared* chunk shape (never the clipped edge shape).  Missing source chunks
are never decoded; they are handled by the fill path.
"""

import zlib

import numpy as np

from .errors import CorruptSource

DTYPE = np.dtype("<i4")


def read_file(path):
    with open(path, "rb") as f:
        return f.read()


def decode_chunk(raw, chunk_shape, label):
    """Decode a zlib-compressed int32 chunk into a C-order <i4 ndarray view.

    ``raw`` must be a complete zlib stream containing exactly
    ``prod(chunk_shape) * 4`` bytes.  Raises ``CorruptSource`` otherwise.

    The returned array is a **read-only view** over the decompressed byte
    buffer: it is not copied, so the caller charges the underlying storage
    exactly once (see ``executor.Executor._scatter``).
    """
    expected = int(np.prod(chunk_shape)) * 4
    if len(raw) == 0:
        raise CorruptSource(f"{label}: existing chunk payload is empty")
    d = zlib.decompressobj()
    try:
        out = d.decompress(raw)
        tail = d.flush()
    except zlib.error as e:
        raise CorruptSource(f"{label}: zlib decode failed: {e}")
    if tail:
        out = out + tail
    if not d.eof:
        raise CorruptSource(f"{label}: zlib stream is truncated (incomplete)")
    if d.unused_data:
        raise CorruptSource(f"{label}: trailing bytes after zlib stream")
    if len(out) != expected:
        raise CorruptSource(
            f"{label}: decoded size {len(out)} != expected {expected} "
            f"for chunk shape {tuple(chunk_shape)}"
        )
    arr = np.frombuffer(out, dtype=DTYPE).reshape(chunk_shape)
    # The read-only array keeps ``out`` alive; no second uncompressed copy.
    return arr


def encode_chunk(arr, level):
    """Compress a C-contiguous target chunk, returning raw zlib bytes."""
    a = np.ascontiguousarray(arr, dtype=DTYPE)
    return zlib.compress(memoryview(a), level)


def validate_zlib_stream(raw, expected_bytes, label):
    """Validate a zlib stream without materialising its decompressed output.

    Streams the payload and counts decoded bytes; returns the count.  Used for
    pre-publication checks so validation itself does not decode a parallel copy
    of the whole chunk.
    """
    if len(raw) == 0:
        raise CorruptSource(f"{label}: empty payload")
    d = zlib.decompressobj()
    total = 0
    try:
        for start in range(0, len(raw), 65536):
            total += len(d.decompress(raw[start:start + 65536]))
        total += len(d.flush())
    except zlib.error as e:
        raise CorruptSource(f"{label}: zlib decode failed: {e}")
    if not d.eof:
        raise CorruptSource(f"{label}: zlib stream is truncated (incomplete)")
    if d.unused_data:
        raise CorruptSource(f"{label}: trailing bytes after zlib stream")
    if total != expected_bytes:
        raise CorruptSource(
            f"{label}: decoded size {total} != expected {expected_bytes}"
        )
    return total


def fill_chunk(chunk_shape, fill_value):
    """A full declared-shape chunk filled with the fill value."""
    out = np.empty(chunk_shape, dtype=DTYPE)
    out[...] = np.int32(fill_value)
    return out


def make_target_chunk(chunk_shape, fill_value):
    """Fresh target assembly buffer, initialised to fill."""
    out = np.empty(chunk_shape, dtype=DTYPE)
    out[...] = np.int32(fill_value)
    return out
