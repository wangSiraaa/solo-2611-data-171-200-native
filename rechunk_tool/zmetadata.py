"""Zarr v2 metadata parsing, strict validation and validation-only codecs.

The executor reads and writes the raw zlib payloads itself; this module only
uses the independent ``numcodecs`` Zlib implementation inside ``verify``.
"""

import json
import os

from .errors import MetadataError

SUPPORTED_DTYPES = {"<i4": 4}
SUPPORTED_ORDER = "C"
SUPPORTED_COMPRESSOR = "zlib"


def _read_json(path):
    try:
        with open(path, "rb") as f:
            return json.loads(f.read())
    except FileNotFoundError:
        raise MetadataError(f"missing metadata file: {path}")
    except (OSError, ValueError) as e:
        raise MetadataError(f"cannot parse metadata file {path}: {e}")


def _product(values):
    n = 1
    for v in values:
        n *= v
    return n


def load_source_meta(store_root, label="source"):
    """Load and strictly validate a Zarr v2 array's .zarray.

    Rejects anything outside the agreed scope: only a C-order little-endian
    int32 three-dimensional array compressed with zlib and no filters is
    accepted.
    """
    path = os.path.join(store_root, ".zarray")
    meta = _read_json(path)
    if not isinstance(meta, dict):
        raise MetadataError(f"{label}: .zarray is not a JSON object")

    if meta.get("zarr_format") != 2:
        raise MetadataError(f"{label}: zarr_format must be 2, got {meta.get('zarr_format')!r}")

    shape = meta.get("shape")
    chunks = meta.get("chunks")
    if not isinstance(shape, list) or not isinstance(chunks, list):
        raise MetadataError(f"{label}: shape/chunks must be arrays")
    if len(shape) != 3 or len(chunks) != 3:
        raise MetadataError(f"{label}: only 3-dimensional arrays are supported")
    if not all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in shape):
        raise MetadataError(f"{label}: shape must be positive integers, got {shape!r}")
    if not all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in chunks):
        raise MetadataError(f"{label}: chunks must be positive integers, got {chunks!r}")

    dtype = meta.get("dtype")
    if dtype not in SUPPORTED_DTYPES:
        raise MetadataError(f"{label}: only little-endian int32 (<i4) is supported, got {dtype!r}")

    order = meta.get("order")
    if order != SUPPORTED_ORDER:
        raise MetadataError(f"{label}: only C order is supported, got {order!r}")

    if meta.get("filters") not in (None, []):
        raise MetadataError(f"{label}: filters are not supported, got {meta.get('filters')!r}")

    compressor = meta.get("compressor")
    if not isinstance(compressor, dict) or compressor.get("id") != SUPPORTED_COMPRESSOR:
        raise MetadataError(f"{label}: only a single zlib compressor is supported, got {compressor!r}")
    level = compressor.get("level", 1)
    if not isinstance(level, int) or isinstance(level, bool) or not -1 <= level <= 9:
        raise MetadataError(f"{label}: invalid zlib level {level!r}")

    if "fill_value" not in meta:
        raise MetadataError(f"{label}: fill_value is required")
    fill = meta["fill_value"]
    if fill is None:
        raise MetadataError(f"{label}: fill_value None is unsupported with int32")
    if isinstance(fill, bool) or not isinstance(fill, int):
        raise MetadataError(f"{label}: fill_value must be an integer, got {fill!r}")
    if not -(2**31) <= fill < 2**31:
        raise MetadataError(f"{label}: fill_value does not fit int32")

    separator = meta.get("dimension_separator", ".")
    if separator != ".":
        raise MetadataError(f"{label}: only '.' dimension_separator is supported, got {separator!r}")

    return {
        "shape": tuple(shape),
        "chunks": tuple(chunks),
        "dtype": dtype,
        "itemsize": SUPPORTED_DTYPES[dtype],
        "order": order,
        "compressor_level": level,
        "fill_value": fill,
        "chunk_bytes": _product(chunks) * SUPPORTED_DTYPES[dtype],
        "shape_product": _product(shape) * SUPPORTED_DTYPES[dtype],
    }


def load_attrs(store_root):
    """Return attributes if present.  .zattrs is informational, not essential."""
    try:
        return _read_json(os.path.join(store_root, ".zattrs"))
    except MetadataError as e:
        # Only a missing .zattrs is tolerable for the source.
        if "missing metadata file" in str(e):
            return {}
        raise


def grid_dims(shape, chunks):
    return tuple((s + c - 1) // c for s, c in zip(shape, chunks))


def chunk_key(coord):
    return ".".join(str(int(v)) for v in coord)


def chunk_local_shape(grid_index, shape, chunks):
    """Logical extent of a chunk, clipped to the array domain."""
    return tuple(
        min(chunks[i], shape[i] - grid_index[i] * chunks[i]) for i in range(3)
    )


def all_grid_coords(grid_dims_):
    for i in range(grid_dims_[0]):
        for j in range(grid_dims_[1]):
            for k in range(grid_dims_[2]):
                yield (i, j, k)
