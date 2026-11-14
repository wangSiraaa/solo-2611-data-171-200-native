"""Gradient-table loading and validation.

The TSV columns are ``volume, b_s_mm2, gx, gy, gz`` where ``volume`` is the
zero-based index into the fourth NIfTI dimension.  Rows must cover every
volume exactly once; ordering inside the file is irrelevant.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

GRADIENT_FRAME = "voxel_axes"
UNIT_TOL = 1e-5


class GradientError(ValueError):
    """Raised when a gradient table is malformed or inconsistent."""


@dataclass(frozen=True)
class GradientTable:
    """Validated per-volume b-values and image-axis gradient directions."""

    volume: np.ndarray  # int (n_vol,)
    bvals: np.ndarray  # float (n_vol,), s/mm^2
    gradients: np.ndarray  # float (n_vol, 3), image-axis frame, unit rows
    b0_mask: np.ndarray  # bool (n_vol,)
    b0_threshold: float

    @property
    def n_volumes(self) -> int:
        return int(self.volume.shape[0])

    @property
    def diffusion_volumes(self) -> np.ndarray:
        return self.volume[~self.b0_mask]


def load_gradient_table(
    path: str, n_volumes: int, b0_threshold: float
) -> GradientTable:
    """Parse and validate a gradient TSV against the image volume count."""
    try:
        raw = np.genfromtxt(path, delimiter="\t", names=True, dtype=None, encoding="utf-8")
    except Exception as exc:
        raise GradientError(f"cannot read gradient table {path!r}: {exc}") from exc

    required = ("volume", "b_s_mm2", "gx", "gy", "gz")
    if raw.dtype.names is None or any(name not in raw.dtype.names for name in required):
        raise GradientError(
            f"{path}: expected columns {required}, got {raw.dtype.names}"
        )

    rows = np.atleast_1d(raw)
    volume = np.asarray([int(r["volume"]) for r in rows], dtype=int)
    bvals = np.asarray([float(r["b_s_mm2"]) for r in rows], dtype=float)
    grads = np.asarray(
        [[float(r["gx"]), float(r["gy"]), float(r["gz"])] for r in rows],
        dtype=float,
    )

    if volume.shape[0] != n_volumes:
        raise GradientError(
            f"{path}: {volume.shape[0]} gradient rows but image has "
            f"{n_volumes} volumes; fourth-dimension correspondence is required"
        )
    if np.any(volume < 0) or np.any(volume >= n_volumes):
        raise GradientError(
            f"{path}: volume indices must lie in [0, {n_volumes - 1}]"
        )
    if np.unique(volume).size != volume.size:
        raise GradientError(f"{path}: duplicate volume indices in gradient table")
    if not np.array_equal(np.sort(volume), np.arange(n_volumes)):
        raise GradientError(
            f"{path}: gradient volumes must be a permutation covering "
            f"0..{n_volumes - 1} exactly once"
        )

    # Re-order everything to volume order so array indices match the 4th axis.
    order = np.argsort(volume)
    volume, bvals, grads = volume[order], bvals[order], grads[order]

    if np.any(~np.isfinite(bvals)) or np.any(bvals < 0.0):
        raise GradientError(f"{path}: b-values must be finite and non-negative")

    b0_mask = bvals <= b0_threshold
    norms = np.linalg.norm(grads, axis=1)
    # b0 rows must carry a zero direction; diffusion rows must be unit length.
    if np.any(norms[b0_mask] > UNIT_TOL):
        raise GradientError(
            f"{path}: b<= {b0_threshold:g} rows must have zero gradient vector"
        )
    diff_norm_err = np.abs(norms[~b0_mask] - 1.0)
    if np.any(diff_norm_err > UNIT_TOL):
        bad = np.where(~b0_mask)[0][diff_norm_err > UNIT_TOL]
        raise GradientError(
            f"{path}: diffusion gradient rows must be unit length (tol "
            f"{UNIT_TOL:g}); offending volumes {volume[bad].tolist()}"
        )
    if np.any(~np.isfinite(grads[~b0_mask])):
        raise GradientError(f"{path}: non-finite gradient components")
    if not np.any(~b0_mask):
        raise GradientError(f"{path}: no diffusion (b > {b0_threshold:g}) volumes")

    return GradientTable(
        volume=volume,
        bvals=bvals,
        gradients=grads,
        b0_mask=b0_mask,
        b0_threshold=float(b0_threshold),
    )
