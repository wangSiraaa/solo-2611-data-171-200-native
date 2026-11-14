"""Voxel-wise fixed linear diffusion-tensor model.

Model (world-RAS coordinates, symmetric tensor D)::

    S(b, g) = S0 * exp(-b * g^T D g)
    y = -ln(S / S0) / b = X * d
    X row = [gx^2, gy^2, gz^2, 2 gx gy, 2 gx gz, 2 gy gz]
    d     = [Dxx, Dyy, Dzz, Dxy, Dxz, Dyz]

Every voxel is handled independently: measurements that are non-positive or
non-finite cannot enter a logarithm and are dropped *for that voxel only*.
A voxel whose surviving design matrix is rank-deficient or ill-conditioned is
left unfitted even though the full gradient table itself is full rank.
"""

from __future__ import annotations

from dataclasses import dataclass

import nibabel as nib
import numpy as np

from .geometry import image_to_world_rotation, validate_affine
from .gradients import GradientError, GradientTable, load_gradient_table

#: singular values below this fraction of the largest are treated as zero
RANK_SV_TOL = 1e-8

# Exclusion / status vocabulary (kept stable for diagnostics and tests).
EXCL_B0_NON_POSITIVE = "b0_non_positive_or_nonfinite"
EXCL_DWI_NON_POSITIVE = "dwi_non_positive_or_nonfinite"

STATUS_OK = "fitted"
STATUS_UNPHYSICAL = "fitted_unphysical_negative_eigenvalue"
STATUS_ISOTROPIC_DIR = "fitted_isotropic_direction_undefined"
STATUS_TIED_DIR = "fitted_tied_principal_eigenvalues_direction_undefined"
STATUS_INSUFFICIENT_B0 = "excluded_insufficient_valid_b0"
STATUS_INSUFFICIENT_ROWS = "excluded_insufficient_diffusion_rows"
STATUS_RANK_DEFICIENT = "excluded_design_rank_deficient"
STATUS_ILL_CONDITIONED = "excluded_design_condition_too_large"


class FitError(ValueError):
    """Raised when a whole-case input is unusable."""


@dataclass
class FitResult:
    """All arrays keep the input spatial grid (no resampling)."""

    case_name: str
    affine: np.ndarray
    header: nib.Nifti1Header
    spatial_shape: tuple
    rotation: np.ndarray  # 3x3 R used for gradient frame conversion
    gradients_world: np.ndarray  # (n_vol, 3), world-RAS unit directions
    table: GradientTable
    data: np.ndarray  # scaled physical 4D signal

    # 3D per-voxel products
    tensor: np.ndarray  # (..., 6) [Dxx,Dyy,Dzz,Dxy,Dxz,Dyz], world RAS
    fa: np.ndarray  # NaN where not computable
    md: np.ndarray  # NaN where not computable
    evals: np.ndarray  # (..., 3), ascending order; NaN where unfitted
    evecs: np.ndarray  # (..., 3, 3) columns = eigenvectors; NaN where unfitted
    principal_dir: np.ndarray  # (..., 3) world RAS; NaN when undefined
    s0: np.ndarray  # mean of valid b0; NaN when insufficient
    residual_sum_sq: np.ndarray  # RSS over the rows used; NaN when unfitted
    n_valid_b0: np.ndarray  # int
    n_valid_directions: np.ndarray  # int surviving diffusion rows used
    condition_number: np.ndarray  # of the voxel design; NaN when < 1 row
    fitted: np.ndarray  # bool: a tensor was estimated
    physical: np.ndarray  # bool: fitted and no significantly negative eigenvalue
    metric_valid: np.ndarray  # bool: FA/MD defined (physical + computable)
    principal_defined: np.ndarray  # bool: unique principal direction exists
    diagnostics: list  # list[dict], one per voxel


def _voxel_design(gradients_world: np.ndarray) -> np.ndarray:
    """Build the full 6-column symmetric-tensor design matrix."""
    gx, gy, gz = gradients_world[:, 0], gradients_world[:, 1], gradients_world[:, 2]
    return np.column_stack(
        [gx * gx, gy * gy, gz * gz, 2.0 * gx * gy, 2.0 * gx * gz, 2.0 * gy * gz]
    )


def fit_case(
    image_path: str,
    gradient_path: str,
    plan: dict,
    case_name: str,
) -> FitResult:
    """Run the complete voxel-wise fit for one image/gradient pair."""
    img = nib.load(image_path)
    if not isinstance(img, nib.Nifti1Image):
        raise FitError(f"{image_path}: expected a NIfTI-1 image")
    header = img.header.copy()
    affine = img.get_sform() if int(header["sform_code"]) > 0 else np.asarray(img.affine)
    validate_affine(header, affine)

    # Physical, scale-applied signal (int16 payload * scl_slope + scl_inter).
    data = np.asarray(img.get_fdata(), dtype=float)
    spatial_shape = data.shape[:3]
    n_volumes = data.shape[3]

    table = load_gradient_table(
        gradient_path, n_volumes, float(plan["b0_threshold"])
    )
    if plan.get("gradient_frame", "voxel_axes") != "voxel_axes":
        raise GradientError(
            f"unsupported gradient_frame {plan.get('gradient_frame')!r}; "
            "only 'voxel_axes' is implemented"
        )

    rotation = image_to_world_rotation(affine)
    # g_world = R g_input; b0 zero vectors stay zero, unit lengths are preserved
    # because R is orthogonal (including the improper det=-1 case).
    gradients_world = table.gradients @ rotation.T

    full_design = _voxel_design(gradients_world)
    b0_idx = np.where(table.b0_mask)[0]
    diff_idx = np.where(~table.b0_mask)[0]
    bvals_diff = table.bvals[diff_idx]

    min_b0 = int(plan["min_valid_b0"])
    min_rows = int(plan["min_diffusion_rows"])
    max_cond = float(plan["max_design_condition"])
    eig_tol = float(plan["eigenvalue_tolerance_mm2_s"])
    principal_gap = float(plan["principal_gap_relative"])

    nvox = int(np.prod(spatial_shape))
    flat = data.reshape(nvox, n_volumes)

    tensor = np.full((nvox, 6), np.nan)
    fa = np.full(nvox, np.nan)
    md = np.full(nvox, np.nan)
    evals = np.full((nvox, 3), np.nan)
    evecs = np.full((nvox, 3, 3), np.nan)
    pdir = np.full((nvox, 3), np.nan)
    s0 = np.full(nvox, np.nan)
    rss = np.full(nvox, np.nan)
    n_b0 = np.zeros(nvox, dtype=int)
    n_dir = np.zeros(nvox, dtype=int)
    cond = np.full(nvox, np.nan)
    fitted = np.zeros(nvox, dtype=bool)
    physical = np.zeros(nvox, dtype=bool)
    metric_valid = np.zeros(nvox, dtype=bool)
    pdir_defined = np.zeros(nvox, dtype=bool)
    diagnostics: list[dict] = []

    for v in range(nvox):
        series = flat[v]
        voxel = np.unravel_index(v, spatial_shape)
        diag: dict = {
            "voxel_index": v,
            "voxel_ijk": list(int(c) for c in voxel),
            "excluded_volumes": {},
            "notes": [],
        }

        # --- b0: S0 from valid positive finite b0 measurements ----------
        b0_vals = series[b0_idx]
        b0_ok = np.isfinite(b0_vals) & (b0_vals > 0.0)
        for vol, ok in zip(b0_idx, b0_ok):
            if not ok:
                diag["excluded_volumes"][int(vol)] = EXCL_B0_NON_POSITIVE
        n_b0[v] = int(b0_ok.sum())
        if n_b0[v] < min_b0:
            diag["status"] = STATUS_INSUFFICIENT_B0
            diag["s0"] = None
            diag["n_valid_b0"] = n_b0[v]
            diag["n_valid_directions"] = 0
            diag["condition_number"] = None
            diag["residual_sum_sq"] = None
            diagnostics.append(diag)
            continue
        s0_v = float(np.mean(b0_vals[b0_ok]))
        s0[v] = s0_v

        # --- diffusion rows: keep positive finite measurements only -----
        diff_vals = series[diff_idx]
        d_ok = np.isfinite(diff_vals) & (diff_vals > 0.0)
        for vol, ok in zip(diff_idx, d_ok):
            if not ok:
                diag["excluded_volumes"][int(vol)] = EXCL_DWI_NON_POSITIVE
        kept = np.where(d_ok)[0]
        n_dir[v] = int(kept.size)
        diag["n_valid_b0"] = n_b0[v]
        diag["n_valid_directions"] = n_dir[v]
        diag["s0"] = s0_v

        if n_dir[v] < min_rows:
            diag["status"] = STATUS_INSUFFICIENT_ROWS
            diag["condition_number"] = None
            diag["residual_sum_sq"] = None
            diagnostics.append(diag)
            continue

        X = full_design[diff_idx[kept]]
        b_kept = bvals_diff[kept]
        s_kept = diff_vals[kept]
        y = -np.log(s_kept / s0_v) / b_kept
        if np.any(~np.isfinite(y)):
            # Defensive: positive S and S0 guarantee a finite real logarithm.
            diag["status"] = STATUS_RANK_DEFICIENT
            diag["condition_number"] = None
            diag["residual_sum_sq"] = None
            diagnostics.append(diag)
            continue

        # Rank and condition number from the *voxel's* surviving design.
        sv = np.linalg.svd(X, compute_uv=False)
        rank = int(np.count_nonzero(sv > RANK_SV_TOL * sv[0]))
        # Guard the ratio: with a zero singular value the condition number is
        # formally infinite; report rank first and keep the NaN convention used
        # for all unavailable per-voxel quantities.
        cond_v = np.inf
        if sv[-1] > 0.0:
            cond_v = float(sv[0] / sv[-1])
            cond[v] = cond_v
            diag["condition_number"] = cond_v
        else:
            diag["condition_number"] = None
        if rank < 6:
            diag["status"] = STATUS_RANK_DEFICIENT
            diag["residual_sum_sq"] = None
            diagnostics.append(diag)
            continue
        if cond_v > max_cond:
            diag["status"] = STATUS_ILL_CONDITIONED
            diag["residual_sum_sq"] = None
            diagnostics.append(diag)
            continue

        # --- ordinary least squares -------------------------------------
        d_hat, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
        y_hat = X @ d_hat
        rss_v = float(np.sum((y - y_hat) ** 2))
        rss[v] = rss_v
        diag["residual_sum_sq"] = rss_v
        tensor[v] = d_hat
        fitted[v] = True

        # --- symmetric tensor eigen-decomposition -----------------------
        Dxx, Dyy, Dzz, Dxy, Dxz, Dyz = d_hat
        D = np.array(
            [[Dxx, Dxy, Dxz], [Dxy, Dyy, Dyz], [Dxz, Dyz, Dzz]],
            dtype=float,
        )
        # np.linalg.eigh returns eigenvalues in ascending order; columns are
        # the corresponding eigenvectors of the symmetric tensor.
        eigvals, eigvecs = np.linalg.eigh(D)
        evals[v] = eigvals
        evecs[v] = eigvecs

        if np.any(eigvals < -eig_tol):
            # Significantly negative: non-physical. Never silently repair.
            physical[v] = False
            metric_valid[v] = False
            diag["status"] = STATUS_UNPHYSICAL
            diag["negative_eigenvalues"] = eigvals[eigvals < -eig_tol].tolist()
            diag["eigenvalues"] = eigvals.tolist()
            diag["eigenvalue_tolerance_mm2_s"] = eig_tol
            diagnostics.append(diag)
            continue

        physical[v] = True
        # Eigenvalues in [-tol, 0) are numerically zero within the declared
        # tolerance only; record the clipping rather than hiding it.
        clipped = eigvals[(eigvals < 0.0) & (eigvals >= -eig_tol)]
        if clipped.size:
            diag["notes"].append(
                "eigenvalue(s) within [-tol, 0) treated as zero: "
                f"{clipped.tolist()}"
            )
        eigvals_c = np.maximum(eigvals, 0.0)

        md_v = float(np.mean(eigvals_c))
        denom = float(np.sum(eigvals_c**2))
        md[v] = md_v
        if denom > 0.0:
            fa_v = float(
                np.sqrt(1.5 * np.sum((eigvals_c - md_v) ** 2) / denom)
            )
            fa[v] = fa_v
            metric_valid[v] = True
        else:
            # All eigenvalues zero: FA is 0/0, genuinely undefined, not FA=0.
            metric_valid[v] = False
            diag["notes"].append("all eigenvalues zero: FA undefined (0/0)")

        # --- unique principal direction? -------------------------------
        lam1, lam2 = eigvals_c[-1], eigvals_c[-2]
        scale = max(abs(lam1), 1e-300)
        if np.all(eigvals_c == 0.0):
            diag["status"] = STATUS_ISOTROPIC_DIR
        elif (lam1 - lam2) / scale < principal_gap:
            diag["status"] = STATUS_TIED_DIR
        else:
            v1 = eigvecs[:, -1].copy()
            # Public sign convention: component with the largest absolute
            # magnitude is positive, fixing the arbitrary eigenvector sign.
            v1 *= np.sign(v1[np.argmax(np.abs(v1))])
            pdir[v] = v1
            pdir_defined[v] = True
            diag["status"] = STATUS_OK
        diag["eigenvalues"] = eigvals.tolist()
        diag["md"] = float(md[v])
        diag["fa"] = float(fa[v]) if metric_valid[v] else None
        diagnostics.append(diag)

    def grid(a: np.ndarray) -> np.ndarray:
        return a.reshape(spatial_shape + a.shape[1:])

    return FitResult(
        case_name=case_name,
        affine=np.asarray(affine),
        header=header,
        spatial_shape=tuple(spatial_shape),
        rotation=rotation,
        gradients_world=gradients_world,
        table=table,
        data=data,
        tensor=grid(tensor),
        fa=grid(fa),
        md=grid(md),
        evals=grid(evals),
        evecs=grid(evecs),
        principal_dir=grid(pdir),
        s0=grid(s0),
        residual_sum_sq=grid(rss),
        n_valid_b0=grid(n_b0),
        n_valid_directions=grid(n_dir),
        condition_number=grid(cond),
        fitted=grid(fitted),
        physical=grid(physical),
        metric_valid=grid(metric_valid),
        principal_defined=grid(pdir_defined),
        diagnostics=diagnostics,
    )
