"""SEG-Y reading with header validation.

Reads the real trace headers (CDP bytes 21-24, offset 37-40,
TraceIdentificationCode 29-30, DelayRecordingTime 109-110 ms,
sample count 115-116, sample interval 117-118 us) and the binary
header, cross-checks them, and converts everything to SI seconds.

File order is preserved in TraceRecord.file_index but never assumed to
mean anything: grouping by CDP happens later.
"""

from __future__ import annotations

import segyio
import numpy as np

from .errors import HeaderContradictionError, UnsupportedFormatError
from .model import Plan, TraceRecord

# 4-byte floating point formats this pipeline accepts (segyio converts
# both to float32 transparently).  Anything else is diagnosed, not guessed.
SUPPORTED_FORMATS = {1: "IBM float32", 5: "IEEE float32"}

_US_TO_S = 1e-6
_MS_TO_S = 1e-3


def _check_headers(f, path: str) -> tuple[int, int]:
    """Cross-check binary header against every trace header.

    Returns (ns, dt_us).  Raises HeaderContradictionError on disagreement.
    A zero trace-header field means "not written" and falls back to the
    binary header (common SEG-Y practice); two different non-zero values
    are a contradiction.
    """
    bin_ns = int(f.bin[segyio.BinField.Samples])
    bin_dt = int(f.bin[segyio.BinField.Interval])
    if bin_ns <= 0:
        raise HeaderContradictionError(
            f"{path}: binary header sample count {bin_ns} is not positive"
        )
    if bin_dt <= 0:
        raise HeaderContradictionError(
            f"{path}: binary header sample interval {bin_dt} us is not positive"
        )
    actual_ns = len(f.samples)
    if actual_ns != bin_ns:
        raise HeaderContradictionError(
            f"{path}: binary header says {bin_ns} samples/trace but the file "
            f"actually stores {actual_ns}"
        )
    tf = segyio.TraceField
    for i in range(f.tracecount):
        h = f.header[i]
        tr_ns = int(h[tf.TRACE_SAMPLE_COUNT])
        tr_dt = int(h[tf.TRACE_SAMPLE_INTERVAL])
        if tr_ns not in (0, bin_ns):
            raise HeaderContradictionError(
                f"{path}: trace {i} header sample count {tr_ns} contradicts "
                f"binary header {bin_ns}"
            )
        if tr_dt not in (0, bin_dt):
            raise HeaderContradictionError(
                f"{path}: trace {i} header interval {tr_dt} us contradicts "
                f"binary header {bin_dt} us"
            )
    return bin_ns, bin_dt


def read_segy(path: str, plan: Plan) -> list[TraceRecord]:
    """Read and validate a SEG-Y file into TraceRecords (SI units)."""
    try:
        f = segyio.open(path, mode="r", ignore_geometry=True, strict=True)
    except Exception as exc:  # segyio raises bare RuntimeError/OSError
        raise UnsupportedFormatError(
            f"{path}: cannot open as SEG-Y rev1 ({exc})"
        ) from exc
    with f:
        fmt = int(f.format)
        if fmt not in SUPPORTED_FORMATS:
            raise UnsupportedFormatError(
                f"{path}: sample format code {fmt} is not supported "
                f"(supported: {SUPPORTED_FORMATS}); refusing to guess"
            )
        ns, dt_us = _check_headers(f, path)
        dt_s = dt_us * _US_TO_S
        tf = segyio.TraceField
        records: list[TraceRecord] = []
        for i in range(f.tracecount):
            h = f.header[i]
            delay_ms = int(h[tf.DelayRecordingTime])
            tic = int(h[tf.TraceIdentificationCode])
            samples = np.asarray(f.trace[i], dtype=np.float64)
            if samples.shape[0] != ns:
                raise HeaderContradictionError(
                    f"{path}: trace {i} holds {samples.shape[0]} samples, "
                    f"expected {ns}"
                )
            records.append(
                TraceRecord(
                    file_index=i,
                    tracl=int(h[tf.TRACE_SEQUENCE_FILE]),
                    cdp=int(h[tf.CDP]),
                    offset_m=float(int(h[tf.offset])),  # signed, metres
                    live=(tic == plan.live_trace_code),
                    delay_s=delay_ms * _MS_TO_S,
                    dt_s=dt_s,
                    samples=samples,
                )
            )
    if not records:
        raise HeaderContradictionError(f"{path}: file contains no traces")
    return records
