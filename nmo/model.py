"""Data model for the NMO correction pipeline.

Times are seconds, offsets are metres (sign preserved), velocities are
m/s.  Nothing in this module does I/O; see segy_reader / velocity.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

import numpy as np


class Reason(enum.IntEnum):
    """Per output-sample provenance code (stored in the reason matrix)."""

    OK = 0
    DEAD_TRACE = 1        # TraceIdentificationCode != plan live code
    STRETCH_MUTE = 2      # tx/t0 - 1 strictly greater than stretch_limit
    OUT_OF_SUPPORT = 3    # tx outside recorded [delay, delay+(ns-1)*dt]


REASON_NAMES = {r.value: r.name for r in Reason}


@dataclass(frozen=True)
class Plan:
    """Processing plan (fixtures/plan.json)."""

    t0_start_s: float
    t0_step_s: float
    output_count: int
    stretch_limit: float
    live_trace_code: int = 1

    @property
    def t0_grid(self) -> np.ndarray:
        """Common zero-offset output time axis (seconds)."""
        return self.t0_start_s + self.t0_step_s * np.arange(self.output_count)


@dataclass
class TraceRecord:
    """One seismic trace with headers already converted to SI units."""

    file_index: int        # 0-based position in the SEG-Y file (identity)
    tracl: int             # TRACE_SEQUENCE_FILE header, for cross-reference
    cdp: int
    offset_m: float        # signed; NMO uses offset**2 so sign is preserved
    live: bool             # TraceIdentificationCode == plan.live_trace_code
    delay_s: float         # DelayRecordingTime (ms) converted to seconds
    dt_s: float            # sample interval (us header) converted to seconds
    samples: np.ndarray    # float64 amplitudes, length ns

    @property
    def ns(self) -> int:
        return int(self.samples.shape[0])

    @property
    def time_first_s(self) -> float:
        """Recorded time of sample 0 (NOT assumed to be zero)."""
        return self.delay_s

    @property
    def time_last_s(self) -> float:
        """Recorded time of the last sample; the source support is the
        closed interval [time_first_s, time_last_s]."""
        return self.delay_s + (self.ns - 1) * self.dt_s

    def sample_times(self) -> np.ndarray:
        return self.delay_s + self.dt_s * np.arange(self.ns)


@dataclass
class VelocityFunction:
    """RMS velocity table for one CDP: v(t0) linear in t0, no extrapolation."""

    cdp: int
    t0_s: np.ndarray     # strictly increasing
    vrms_mps: np.ndarray  # strictly positive

    def interpolate(self, t0: np.ndarray) -> np.ndarray:
        """Linear interpolation in t0.  Raises outside table coverage
        (no extrapolation, per README)."""
        from .errors import VelocityTableError

        t0 = np.asarray(t0, dtype=np.float64)
        lo, hi = self.t0_s[0], self.t0_s[-1]
        # small tolerance so a grid node exactly on the table edge passes
        tol = 1e-12 * max(1.0, abs(lo), abs(hi))
        if t0.min() < lo - tol or t0.max() > hi + tol:
            raise VelocityTableError(
                f"cdp {self.cdp}: output t0 range "
                f"[{t0.min():.6f}, {t0.max():.6f}] s exceeds velocity table "
                f"coverage [{lo:.6f}, {hi:.6f}] s; extrapolation is not allowed"
            )
        return np.interp(t0, self.t0_s, self.vrms_mps)


@dataclass
class CdpResult:
    """NMO-corrected gather and stack for one CDP."""

    cdp: int
    t0_s: np.ndarray          # (n_t0,) common zero-offset axis
    trace_indices: np.ndarray  # (n_traces,) file_index of each row
    tracl: np.ndarray          # (n_traces,) TRACE_SEQUENCE_FILE
    offsets_m: np.ndarray      # (n_traces,) signed offsets
    delays_s: np.ndarray       # (n_traces,)
    live: np.ndarray           # (n_traces,) bool
    corrected: np.ndarray      # (n_traces, n_t0) amplitudes, NaN where invalid
    valid: np.ndarray          # (n_traces, n_t0) bool mask
    reason: np.ndarray         # (n_traces, n_t0) Reason codes
    tx_s: np.ndarray           # (n_traces, n_t0) mapped source times
    stretch: np.ndarray        # (n_traces, n_t0) tx/t0 - 1 (inf at t0=0, off!=0)
    velocity_mps: np.ndarray   # (n_t0,) v(t0) used for this CDP
    fold: np.ndarray           # (n_t0,) int
    stack_sum: np.ndarray      # (n_t0,)
    stack: np.ndarray          # (n_t0,) mean, NaN where fold == 0


@dataclass
class CorrectResult:
    """Full pipeline output: per-CDP results plus run metadata."""

    plan: Plan
    cdps: dict[int, CdpResult] = field(default_factory=dict)
    excluded_traces: list[dict] = field(default_factory=list)
    # trace identity/provenance for explain_sample (not serialised)
    traces_by_cdp: dict[int, list[TraceRecord]] = field(default_factory=dict)

    def cdp_ids(self) -> list[int]:
        return sorted(self.cdps)
