"""The rechunk executor.

Two deterministic passes with external fragment spooling:

1. **Scatter** -- iterate the *source* grid once.  Every present source chunk
   is decoded at most once; every intersection with a target chunk is copied
   into a per-target fragment file in the spool.  Missing source chunks are
   never decoded and contribute nothing to the spool, so the target assembly
   keeps the fill value they leave behind.

2. **Assemble** -- iterate the *target* grid once.  A full declared-shape
   target buffer is initialised to fill, the spooled fragments are scattered
   into it, and the buffer is encoded and written exactly once.

The executor never holds more than one decoded source chunk or one target
assembly at a time, and never materialises the whole array.
"""

import hashlib
import json
import os
import struct

import numpy as np

from . import codec
from .accounting import ByteCounter, InterpreterOverhead, Ledger
from .errors import SpoolError, VerificationError
from .plan import planned_peak_bytes, target_chunk_ids_covered_by_source
from .zmetadata import chunk_key, chunk_local_shape

FRAGMENT_MAGIC = b"FRG1"
FRAGMENT_HEADER = struct.Struct("<4s6i")  # magic, global_lo[3], shape[3]

TARGET_DTYPE = np.dtype("<i4")


def _grid_coords(dims):
    for i in range(dims[0]):
        for j in range(dims[1]):
            for k in range(dims[2]):
                yield (i, j, k)


def _product(values):
    n = 1
    for v in values:
        n *= v
    return n


def _coords(ranges):
    for i in ranges[0]:
        for j in ranges[1]:
            for k in ranges[2]:
                yield (i, j, k)


def _target_local_shape(G, norm):
    return chunk_local_shape(G, norm["target_shape"], norm["target_chunks"])


class Executor:
    def __init__(self, source_root, norm, plan_report, spool_dir, output_stage,
                 source_attrs=None):
        self.source_root = source_root
        self.norm = norm
        self.report = plan_report
        self.spool_dir = spool_dir
        self.output_stage = output_stage
        self.source_attrs = source_attrs or {}

        self.frag_dir = os.path.join(spool_dir, "fragments")
        os.makedirs(self.frag_dir, exist_ok=True)

        self.ledger = Ledger(norm["data_buffer_budget_bytes"], name="data")
        self.compressed = ByteCounter("compressed")
        self.interpreter = InterpreterOverhead()

        self.decode_calls = {}   # source key -> count
        self.encode_calls = {}   # target key -> count
        self.fill_chunks = []    # source keys skipped as missing
        self.fragment_bytes = 0
        self.fragment_headers = 0
        self.output_manifest = {}
        self.output_stage_bytes = 0

        # Present / missing split of the source grid.
        self.present_keys = set()
        self.missing_keys = set()
        src_dims = norm["source_grid_dims"]
        for g in _grid_coords(src_dims):
            key = chunk_key(g)
            path = os.path.join(source_root, key)
            if os.path.exists(path):
                self.present_keys.add(key)
            else:
                self.missing_keys.add(key)

        # Expected coverage per target: logical cells minus cells owned by
        # missing source chunks.  Missing cells stay at fill.
        self.expected_present_cells = {}
        self.expected_piece_counts = {}
        self.target_order = []
        for G in _grid_coords(norm["target_grid_dims"]):
            self.target_order.append(G)
        self._compute_expected_coverage()

    # ------------------------------------------------------------------
    def _compute_expected_coverage(self):
        for G in self.target_order:
            key = chunk_key(G)
            self.expected_present_cells[key] = 0
            self.expected_piece_counts[key] = 0
        for g in _grid_coords(self.norm["source_grid_dims"]):
            gk = chunk_key(g)
            ranges = target_chunk_ids_covered_by_source(g, self.norm)
            if ranges is None:
                continue
            for G in _coords(ranges):
                piece = _intersect_for(g, G, self.norm)
                if piece is None:
                    continue
                Gk = chunk_key(G)
                if gk in self.present_keys:
                    # Missing source chunks write no spool fragment; the
                    # corresponding target cells stay at fill.
                    self.expected_piece_counts[Gk] += 1
                    self.expected_present_cells[Gk] += _product(piece[3])

    # ------------------------------------------------------------------
    def run(self):
        self.interpreter.capture()
        self._write_metadata()
        self._scatter()
        self._assemble()
        self._check_counts()
        self._finalize_metadata()
        return self._build_report()

    # ------------------------------------------------------------------
    def _write_metadata(self):
        shape = list(self.norm["target_shape"])
        chunks = list(self.norm["target_chunks"])
        zarray = {
            "zarr_format": 2,
            "shape": shape,
            "chunks": chunks,
            "dtype": "<i4",
            "compressor": {"id": "zlib", "level": self.norm["target_compressor_level"]},
            "fill_value": int(self.norm["target_fill_value"]),
            "order": "C",
            "filters": None,
            "dimension_separator": ".",
        }
        zattrs = self._permuted_attrs()
        for name, obj in ((".zarray", zarray), (".zattrs", zattrs)):
            path = os.path.join(self.output_stage, name)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(obj, f, indent=4, sort_keys=True)
                f.write("\n")
            self.output_stage_bytes += os.path.getsize(path)

    def _permuted_attrs(self):
        p = self.norm["axis_permutation"]
        attrs = {}
        src_attrs = self.source_attrs
        if isinstance(src_attrs.get("axis_names"), list) and len(src_attrs["axis_names"]) == 3:
            attrs["axis_names"] = [src_attrs["axis_names"][p[k]] for k in range(3)]
            attrs["source_axis_names"] = list(src_attrs["axis_names"])
        if "units" in src_attrs:
            attrs["units"] = src_attrs["units"]
        if "note" in src_attrs:
            attrs["note"] = src_attrs["note"]
        attrs["rechunk"] = {
            "axis_permutation": list(p),
            "target_chunks": list(self.norm["target_chunks"]),
            "description": "target = source.transpose(axis_permutation)",
        }
        return attrs

    # ------------------------------------------------------------------
    def _scatter(self):
        self.ledger.set_stage("scatter")
        C = self.norm["source_chunks"]
        p = self.norm["axis_permutation"]
        for g in _grid_coords(self.norm["source_grid_dims"]):
            gk = chunk_key(g)
            src_path = os.path.join(self.source_root, gk)
            if gk in self.missing_keys:
                # Fill semantics: zero decoder calls, no fragment written.
                self.fill_chunks.append(gk)
                continue

            # Decode exactly once.
            raw = codec.read_file(src_path)
            self.compressed.add_input(len(raw))
            src = codec.decode_chunk(raw, C, f"source chunk {gk}")  # full C shape
            del raw
            self._record_decode(gk)

            src_key = f"src:{gk}"
            self.ledger.acquire(src_key, f"decoded source chunk {gk}", src.nbytes)
            tview_key = f"srcview:{gk}"
            src_t = src.transpose(p)
            # Transposed array is a view: charged zero.
            self.ledger.acquire(tview_key, f"transposed view of {gk}", 0, owner=src_key)
            try:
                self._scatter_one(g, src_t, src)
            finally:
                self.ledger.release(tview_key)
                self.ledger.release(src_key)
                del src_t, src

    def _scatter_one(self, g, src_t, src):
        norm = self.norm
        ranges = target_chunk_ids_covered_by_source(g, norm)
        if ranges is None:
            return
        for G in _coords(ranges):
            piece = _intersect_for(g, G, norm)
            if piece is None:
                continue
            src_local_lo, src_local_hi, tgt_local_lo, shape = piece
            sl = tuple(
                slice(src_local_lo[k], src_local_hi[k]) for k in range(3)
            )
            view = src_t[sl]
            piece_arr = np.ascontiguousarray(view)
            # A contiguous slice may still be a view; charge only real copies.
            if np.shares_memory(piece_arr, src):
                piece_key = f"fragview:{chunk_key(g)}:{chunk_key(G)}"
                self.ledger.acquire(
                    piece_key, f"fragment view {g}->{G}", 0, owner=f"src:{chunk_key(g)}"
                )
            else:
                piece_key = f"frag:{chunk_key(g)}:{chunk_key(G)}"
                self.ledger.acquire(
                    piece_key, f"fragment copy {g}->{G}", piece_arr.nbytes
                )
            try:
                global_lo = tuple(
                    G[k] * norm["target_chunks"][k] + tgt_local_lo[k] for k in range(3)
                )
                header = FRAGMENT_HEADER.pack(
                    FRAGMENT_MAGIC, *global_lo, *shape
                )
                path = os.path.join(self.frag_dir, chunk_key(G) + ".frag")
                with open(path, "ab") as f:
                    f.write(header)
                    piece_arr.tofile(f)
                self.fragment_bytes += len(header) + piece_arr.nbytes
                self.fragment_headers += 1
            finally:
                self.ledger.release(piece_key)

    # ------------------------------------------------------------------
    def _assemble(self):
        self.ledger.set_stage("assemble")
        norm = self.norm
        TC = norm["target_chunks"]
        fill = norm["target_fill_value"]
        level = norm["target_compressor_level"]

        for G in self.target_order:
            Gk = chunk_key(G)
            tgt = codec.make_target_chunk(TC, fill)
            tkey = f"tgt:{Gk}"
            self.ledger.acquire(tkey, f"target assembly {Gk}", tgt.nbytes)
            covered = 0
            try:
                covered = self._read_fragments(G, tgt)
                expected_pieces = self.expected_piece_counts[Gk]
                expected_cells = self.expected_present_cells[Gk]
                if covered != expected_cells:
                    raise VerificationError(
                        f"target chunk {Gk}: covered {covered} cells, expected "
                        f"{expected_cells} from present source chunks"
                    )
                # Encode exactly once and write exactly once.
                self.ledger.set_stage("encode")
                encoded = codec.encode_chunk(tgt, level)
                self.ledger.set_stage("assemble")
                self._record_encode(Gk)
                self.compressed.add_output(len(encoded))
                dst = os.path.join(self.output_stage, Gk)
                tmp = dst + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(encoded)
                os.replace(tmp, dst)
                digest = hashlib.sha256(encoded).hexdigest()
                self.output_manifest[Gk] = {
                    "size": len(encoded),
                    "sha256": digest,
                    "cells": int(tgt.size),
                }
                self.output_stage_bytes += len(encoded)
                del encoded
            finally:
                self.ledger.release(tkey)
                del tgt

    def _read_fragments(self, G, tgt):
        Gk = chunk_key(G)
        path = os.path.join(self.frag_dir, Gk + ".frag")
        expected_pieces = self.expected_piece_counts[Gk]
        if expected_pieces == 0:
            if os.path.exists(path):
                raise SpoolError(f"target chunk {Gk}: unexpected fragment file")
            return 0
        if not os.path.exists(path):
            raise SpoolError(f"target chunk {Gk}: missing fragment file")

        TC = self.norm["target_chunks"]
        covered = 0
        seen = 0
        with open(path, "rb") as f:
            while True:
                header = f.read(FRAGMENT_HEADER.size)
                if not header:
                    break
                if len(header) != FRAGMENT_HEADER.size:
                    raise SpoolError(f"target chunk {Gk}: truncated fragment header")
                magic, *fields = FRAGMENT_HEADER.unpack(header)
                if magic != FRAGMENT_MAGIC:
                    raise SpoolError(f"target chunk {Gk}: bad fragment magic {magic!r}")
                global_lo = tuple(fields[:3])
                shape = tuple(fields[3:])
                if any(s <= 0 for s in shape):
                    raise SpoolError(f"target chunk {Gk}: non-positive fragment shape")
                n = _product(shape)
                raw = f.read(n * 4)
                if len(raw) != n * 4:
                    raise SpoolError(
                        f"target chunk {Gk}: fragment {seen} truncated "
                        f"({len(raw)}/{n * 4} bytes)"
                    )
                # The raw fragment bytes are a live uncompressed buffer; the
                # numpy array over them is a shared view (charged once).
                raw_key = f"rawfrag:{Gk}:{seen}"
                view_key = f"readfrag:{Gk}:{seen}"
                self.ledger.acquire(
                    raw_key, f"spool fragment bytes {Gk}#{seen}", len(raw)
                )
                try:
                    frag = np.frombuffer(raw, dtype=TARGET_DTYPE).reshape(shape)
                    self.ledger.acquire(
                        view_key, f"spool fragment view {Gk}#{seen}", 0, owner=raw_key
                    )
                    try:
                        lo = tuple(
                            global_lo[k] - G[k] * TC[k] for k in range(3)
                        )
                        sl = tuple(slice(lo[k], lo[k] + shape[k]) for k in range(3))
                        tgt[sl] = frag
                    finally:
                        self.ledger.release(view_key)
                finally:
                    self.ledger.release(raw_key)
                    del raw, frag
                covered += n
                seen += 1
        if seen != expected_pieces:
            raise SpoolError(
                f"target chunk {Gk}: read {seen} fragments, expected {expected_pieces}"
            )
        return covered

    # ------------------------------------------------------------------
    def _record_decode(self, key):
        self.decode_calls[key] = self.decode_calls.get(key, 0) + 1
        if self.decode_calls[key] > 1:
            raise AssertionError(f"source chunk {key} decoded more than once")

    def _record_encode(self, key):
        self.encode_calls[key] = self.encode_calls.get(key, 0) + 1
        if self.encode_calls[key] > 1:
            raise AssertionError(f"target chunk {key} encoded more than once")

    def _check_counts(self):
        if set(self.decode_calls) != self.present_keys:
            missing = self.present_keys - set(self.decode_calls)
            extra = set(self.decode_calls) - self.present_keys
            raise AssertionError(f"decode set mismatch: missing={missing} extra={extra}")
        if any(v != 1 for v in self.decode_calls.values()):
            raise AssertionError("a source chunk was not decoded exactly once")
        expected_targets = {chunk_key(G) for G in self.target_order}
        if set(self.encode_calls) != expected_targets:
            missing = expected_targets - set(self.encode_calls)
            raise AssertionError(f"target chunks not written: {sorted(missing)}")
        if any(v != 1 for v in self.encode_calls.values()):
            raise AssertionError("a target chunk was not encoded exactly once")
        if set(self.fill_chunks) != self.missing_keys:
            raise AssertionError("missing-chunk fill bookkeeping mismatch")
        if len(self.output_manifest) != _product(self.norm["target_grid_dims"]):
            raise AssertionError("output manifest is incomplete")
        if self.ledger.live_count != 0:
            raise AssertionError(
                f"{self.ledger.live_count} data buffers still live after execution"
            )
        if self.ledger.peak > self.norm["data_buffer_budget_bytes"]:
            raise AssertionError("data buffer peak exceeds budget")

    # ------------------------------------------------------------------
    def _finalize_metadata(self):
        """Re-read and validate every staged chunk and the metadata."""
        expected_meta = {
            "zarr_format": 2,
            "shape": list(self.norm["target_shape"]),
            "chunks": list(self.norm["target_chunks"]),
            "dtype": "<i4",
            "fill_value": int(self.norm["target_fill_value"]),
            "order": "C",
            "filters": None,
            "dimension_separator": ".",
        }
        with open(os.path.join(self.output_stage, ".zarray"), "rb") as f:
            staged = json.loads(f.read())
        for k, v in expected_meta.items():
            if staged.get(k) != v:
                raise VerificationError(
                    f"staged .zarray {k}={staged.get(k)!r} != expected {v!r}"
                )
        if staged.get("compressor") != {
            "id": "zlib", "level": self.norm["target_compressor_level"]
        }:
            raise VerificationError("staged .zarray compressor mismatch")

        expected_bytes = _product(self.norm["target_chunks"]) * 4
        for G in self.target_order:
            Gk = chunk_key(G)
            path = os.path.join(self.output_stage, Gk)
            if not os.path.isfile(path):
                raise VerificationError(f"staged target chunk {Gk} is missing")
            with open(path, "rb") as f:
                raw = f.read()
            digest = hashlib.sha256(raw).hexdigest()
            meta = self.output_manifest[Gk]
            if digest != meta["sha256"] or len(raw) != meta["size"]:
                raise VerificationError(f"staged target chunk {Gk} changed after write")
            # Independent decode with the stdlib, checking full physical shape.
            # Validation streams the payload and counts decoded bytes, so at
            # most one chunk-sized numeric buffer is live; it is charged to
            # the same ledger for honesty.
            vkey = f"validate:{Gk}"
            self.ledger.acquire(vkey, f"staged validation {Gk}", expected_bytes)
            try:
                try:
                    decoded = codec.validate_zlib_stream(
                        raw, expected_bytes, f"staged target chunk {Gk}"
                    )
                except Exception as e:
                    raise VerificationError(str(e))
                if decoded != expected_bytes:
                    raise VerificationError(
                        f"staged target chunk {Gk} decoded to {decoded}, "
                        f"expected {expected_bytes}"
                    )
            finally:
                self.ledger.release(vkey)
            del raw

        # No unexpected data files in the output staging directory.
        allowed = {".zarray", ".zattrs"} | {
            chunk_key(G) for G in self.target_order
        }
        for name in os.listdir(self.output_stage):
            if name not in allowed:
                raise VerificationError(f"unexpected file in staged target: {name}")

        # Manifest for post-publication auditing.
        manifest = {
            "source": self.source_root,
            "axis_permutation": list(self.norm["axis_permutation"]),
            "target_shape": list(self.norm["target_shape"]),
            "target_chunks": list(self.norm["target_chunks"]),
            "target_keys": self.output_manifest,
        }
        with open(os.path.join(self.spool_dir, "output-manifest.json"), "w",
                  encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, sort_keys=True)
            f.write("\n")

    # ------------------------------------------------------------------
    def _build_report(self):
        peak_est = planned_peak_bytes(self.norm, self.report)
        counts = {
            "source_logical_chunks": _product(self.norm["source_grid_dims"]),
            "source_present_chunks": len(self.present_keys),
            "source_missing_chunks": len(self.missing_keys),
            "source_decode_calls": sum(self.decode_calls.values()),
            "source_fill_chunks_zero_decode": len(self.fill_chunks),
            "target_chunks_written": len(self.encode_calls),
            "target_encode_calls": sum(self.encode_calls.values()),
            "target_write_calls": sum(self.encode_calls.values()),
            "fragments_written": self.fragment_headers,
            "intersections_planned": self.report["counts"]["intersections"],
            "per_source_decode_max": max(self.decode_calls.values(), default=0),
            "per_target_encode_max": max(self.encode_calls.values(), default=0),
        }
        return {
            "status": "ok",
            "source": self.source_root,
            "output_staging": self.output_stage,
            "spool_dir": self.spool_dir,
            "axis_permutation": list(self.norm["axis_permutation"]),
            "inverse_permutation": list(self.norm["inverse_permutation"]),
            "source_shape": list(self.norm["source_shape"]),
            "source_chunks": list(self.norm["source_chunks"]),
            "target_shape": list(self.norm["target_shape"]),
            "target_chunks": list(self.norm["target_chunks"]),
            "fill_value": int(self.norm["target_fill_value"]),
            "counts": counts,
            # Flattened aliases keep the report easy to assert against.
            **counts,
            "decode_calls_per_source": dict(sorted(self.decode_calls.items())),
            "encode_calls_per_target": dict(sorted(self.encode_calls.items())),
            "fill_source_chunks": sorted(self.fill_chunks),
            "missing_source_chunks": sorted(self.missing_keys),
            "data_buffer_accounting": {
                **self.ledger.report(),
                "measurement": (
                    "unique live uncompressed numeric arrays; shared views charged "
                    "once; compressed bytes and interpreter excluded"
                ),
                "predicted": peak_est,
            },
            "compressed_bytes": self.compressed.report(),
            "interpreter_overhead": self.interpreter.report(),
            "temporary_space": {
                "fragment_spool_bytes": self.fragment_bytes,
                "fragment_files": _product(self.norm["target_grid_dims"]),
                "output_staging_bytes": self.output_stage_bytes,
                "spool_dir": self.spool_dir,
            },
        }


def _intersect_for(g, G, norm):
    """Small local wrapper returning the tuple form used by the executor."""
    from .plan import intersect

    piece = intersect(g, G, norm)
    if piece is None:
        return None
    return (
        piece["src_local_lo"],
        piece["src_local_hi"],
        piece["tgt_local_lo"],
        piece["shape"],
    )


def run_conversion(source_root, norm, plan_report, spool_dir, output_stage,
                   source_attrs):
    ex = Executor(source_root, norm, plan_report, spool_dir, output_stage,
                  source_attrs)
    return ex.run()
