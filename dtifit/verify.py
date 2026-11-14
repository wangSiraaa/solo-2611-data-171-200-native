"""Independent verification of the delivered NIfTI products.

Nothing here imports the fitting internals: outputs are re-read from disk as
NIfTI and the model is rebuilt from the README formulas.  Checks:

1. every product is readable and preserves the source affine/zooms;
2. FA/MD are independently recomputed from the tensor eigenvalues;
3. the signal is regenerated from the tensor and residuals compared with the
   reported RSS, using only the directions that voxel actually retained;
4. the two axis-swapped cases are matched by *world* voxel position, and
   scalars plus world-RAS tensors agree despite the different grids;
5. NaN/mask consistency: missing results have an independent mask and an
   isotropic FA = 0 remains a valid value rather than a missing one.
"""

from __future__ import annotations

import json
import os

import nibabel as nib
import numpy as np


def reconstruct_design(world_gradients: np.ndarray) -> np.ndarray:
    gx, gy, gz = world_gradients.T
    return np.column_stack(
        [gx * gx, gy * gy, gz * gz, 2 * gx * gy, 2 * gx * gz, 2 * gy * gz]
    )


def symmetric_matrix(t6: np.ndarray) -> np.ndarray:
    Dxx, Dyy, Dzz, Dxy, Dxz, Dyz = t6
    return np.array([[Dxx, Dxy, Dxz], [Dxy, Dyy, Dyz], [Dxz, Dyz, Dzz]])


def verify_case(case_dir: str, image_path: str, gradient_path: str, plan: dict) -> dict:
    """Re-read all outputs and independently recompute every reported metric."""
    findings: list[str] = []

    source = nib.load(image_path)
    tensor_img = nib.load(os.path.join(case_dir, "tensor.nii.gz"))
    fa_img = nib.load(os.path.join(case_dir, "fa.nii.gz"))
    md_img = nib.load(os.path.join(case_dir, "md.nii.gz"))
    pdir_img = nib.load(os.path.join(case_dir, "principal_direction.nii.gz"))
    missing_img = nib.load(os.path.join(case_dir, "missing_results_mask.nii.gz"))
    metric_img = nib.load(os.path.join(case_dir, "metric_valid_mask.nii.gz"))
    rss_img = nib.load(os.path.join(case_dir, "residual_sum_sq.nii.gz"))
    diag = json.load(open(os.path.join(case_dir, "voxel_diagnostics.json")))

    # 1. grid / affine / zoom preservation --------------------------------
    src_affine = source.get_sform()
    if tensor_img.shape[:3] != source.shape[:3]:
        findings.append("tensor grid shape differs from source image")
    # Written NIfTI sform fields are float32, so compare at that precision.
    for img, name in (
        (tensor_img, "tensor"), (fa_img, "fa"), (md_img, "md"),
        (pdir_img, "principal_direction"),
    ):
        if np.max(np.abs(img.affine - src_affine)) > 1e-6:
            findings.append(f"{name}: written affine differs from source")
    if tuple(tensor_img.header.get_zooms()[:3]) != tuple(
        source.header.get_zooms()[:3]
    ):
        findings.append("tensor spatial zooms differ from source spacings")

    tensors = np.asarray(tensor_img.dataobj)
    fa = np.asarray(fa_img.dataobj)
    md = np.asarray(md_img.dataobj)
    missing = np.asarray(missing_img.dataobj).astype(bool)
    metric_mask = np.asarray(metric_img.dataobj).astype(bool)
    rss = np.asarray(rss_img.dataobj)
    data = np.asarray(source.get_fdata())

    if np.any(missing != ~metric_mask):
        findings.append("missing_results_mask is not the complement of metric_valid_mask")
    if np.any(np.isnan(fa[metric_mask])) or np.any(~np.isnan(fa[~metric_mask])):
        findings.append("FA NaN pattern does not match metric validity mask")

    # Rebuild world gradients directly from the source affine -------------
    linear = src_affine[:3, :3]
    R = linear / np.linalg.norm(linear, axis=0)
    table = _read_gradients(gradient_path, data.shape[3])
    g_world = table["grad"] @ R.T
    stored_g = np.array([w["g_world_ras"] for w in diag["world_gradients"]])
    if np.max(np.abs(g_world - stored_g)) > 1e-12:
        findings.append("stored world gradients disagree with affine recomputation")
    full_X = reconstruct_design(g_world)
    bvals = table["bvals"]
    b0_idx = np.where(bvals <= plan["b0_threshold"])[0]
    diff_idx = np.where(bvals > plan["b0_threshold"])[0]

    spatial = data.shape[:3]
    nvox = int(np.prod(spatial))
    flat_data = data.reshape(nvox, -1)
    flat_T = tensors.reshape(nvox, 6)
    flat_fa = fa.reshape(nvox)
    flat_md = md.reshape(nvox)
    flat_missing = missing.reshape(nvox)
    flat_metric = metric_mask.reshape(nvox)
    flat_rss = rss.reshape(nvox)

    recomputed = 0
    for v in range(nvox):
        series = flat_data[v]
        vdiag = diag["voxels"][v]
        t6 = flat_T[v]
        if not vdiag["status"].startswith("fitted"):
            if not flat_missing[v]:
                findings.append(f"voxel {v}: unfitted but not in missing mask")
            if not np.all(np.isnan(t6)):
                findings.append(f"voxel {v}: unfitted but tensor contains values")
            continue
        if np.any(~np.isfinite(t6)):
            findings.append(f"voxel {v}: fitted status but tensor has NaN")
            continue

        # independent RSS from retained directions -------------------------
        b0_ok = np.isfinite(series[b0_idx]) & (series[b0_idx] > 0)
        s0 = float(np.mean(series[b0_idx][b0_ok]))
        excluded = {int(k) for k in vdiag["excluded_volumes"].keys()}
        kept_vols = [
            int(vol) for vol in diff_idx
            if vol not in excluded and np.isfinite(series[vol]) and series[vol] > 0
        ]
        rows = [list(diff_idx).index(vol) for vol in kept_vols]
        X = full_X[np.asarray(kept_vols)]
        y = -np.log(series[np.asarray(kept_vols)] / s0) / bvals[np.asarray(kept_vols)]
        y_pred = X @ t6
        rss_ind = float(np.sum((y - y_pred) ** 2))
        if not np.isclose(rss_ind, flat_rss[v], rtol=1e-6, atol=1e-18):
            findings.append(
                f"voxel {v}: independent RSS {rss_ind:.3e} != reported "
                f"{flat_rss[v]:.3e}"
            )

        # independent eigenvalues / FA / MD --------------------------------
        D = symmetric_matrix(t6)
        eigvals = np.linalg.eigvalsh(D)
        eig_tol = plan["eigenvalue_tolerance_mm2_s"]
        if np.any(eigvals < -eig_tol):
            if flat_metric[v]:
                findings.append(f"voxel {v}: negative eigenvalue but metrics marked valid")
            continue
        eig_c = np.maximum(eigvals, 0.0)
        md_ind = float(np.mean(eig_c))
        denom = float(np.sum(eig_c**2))
        fa_ind = float(np.sqrt(1.5 * np.sum((eig_c - md_ind) ** 2) / denom)) if denom > 0 else np.nan
        if not np.isclose(md_ind, flat_md[v], rtol=1e-6, atol=1e-15):
            findings.append(f"voxel {v}: independent MD {md_ind} != {flat_md[v]}")
        if np.isnan(fa_ind):
            if flat_metric[v]:
                findings.append(f"voxel {v}: FA undefined but metric mask says valid")
        elif not np.isclose(fa_ind, flat_fa[v], rtol=1e-5, atol=1e-7):
            findings.append(f"voxel {v}: independent FA {fa_ind} != {flat_fa[v]}")
        recomputed += 1

    return {
        "case": diag["case"],
        "voxels_recomputed": recomputed,
        "findings": findings,
        "ok": not findings,
    }


def _read_gradients(path: str, n_volumes: int) -> dict:
    raw = np.genfromtxt(path, delimiter="\t", names=True, dtype=None, encoding="utf-8")
    rows = np.atleast_1d(raw)
    volume = np.array([int(r["volume"]) for r in rows])
    order = np.argsort(volume)
    assert np.array_equal(volume[order], np.arange(n_volumes))
    return {
        "bvals": np.array([float(r["b_s_mm2"]) for r in rows])[order],
        "grad": np.array(
            [[float(r["gx"]), float(r["gy"]), float(r["gz"])] for r in rows]
        )[order],
    }


def world_center_mapping(image_a: str, image_b: str) -> tuple:
    """For every voxel of A find the voxel of B sharing its world centre."""
    img_a, img_b = nib.load(image_a), nib.load(image_b)
    aff_a, aff_b = img_a.get_sform(), img_b.get_sform()
    shape_a, shape_b = img_a.shape[:3], img_b.shape[:3]
    inv_b = np.linalg.inv(aff_b)
    mapping = {}
    for ijk in np.ndindex(shape_a):
        world = aff_a @ np.array(list(ijk) + [1.0])
        ijk_b = inv_b @ world
        nearest = tuple(int(round(c)) for c in ijk_b[:3])
        if not all(0 <= c < n for c, n in zip(nearest, shape_b)):
            raise AssertionError(f"world centre {world[:3]} maps outside case B")
        # confirm shared centre to floating precision
        world_b = aff_b @ np.array(list(nearest) + [1.0])
        if np.max(np.abs(world - world_b)) > 1e-9:
            raise AssertionError("no exact world-centre counterpart voxel")
        mapping[ijk] = nearest
    return mapping


def verify_cross_case(out_dir: str, plan: dict, fixtures: str) -> dict:
    """Match the two axis-swapped cases by world position and compare."""
    findings: list[str] = []
    case_a, case_b = plan["cases"][0], plan["cases"][1]
    img_a_path = os.path.join(fixtures, case_a["image"])
    img_b_path = os.path.join(fixtures, case_b["image"])
    dir_a = os.path.join(out_dir, os.path.splitext(case_a["image"].replace(".nii", ""))[0])
    dir_b = os.path.join(out_dir, os.path.splitext(case_b["image"].replace(".nii", ""))[0])

    T_a = np.asarray(nib.load(os.path.join(dir_a, "tensor.nii.gz")).dataobj)
    T_b = np.asarray(nib.load(os.path.join(dir_b, "tensor.nii.gz")).dataobj)
    FA_a = np.asarray(nib.load(os.path.join(dir_a, "fa.nii.gz")).dataobj)
    FA_b = np.asarray(nib.load(os.path.join(dir_b, "fa.nii.gz")).dataobj)
    MD_a = np.asarray(nib.load(os.path.join(dir_a, "md.nii.gz")).dataobj)
    MD_b = np.asarray(nib.load(os.path.join(dir_b, "md.nii.gz")).dataobj)
    P_a = np.asarray(nib.load(os.path.join(dir_a, "principal_direction.nii.gz")).dataobj)
    P_b = np.asarray(nib.load(os.path.join(dir_b, "principal_direction.nii.gz")).dataobj)
    mask_a = np.asarray(nib.load(os.path.join(dir_a, "metric_valid_mask.nii.gz")).dataobj).astype(bool)
    mask_b = np.asarray(nib.load(os.path.join(dir_b, "metric_valid_mask.nii.gz")).dataobj).astype(bool)

    mapping = world_center_mapping(img_a_path, img_b_path)
    for ijk_a, ijk_b in mapping.items():
        if mask_a[ijk_a] != mask_b[ijk_b]:
            findings.append(f"{ijk_a}: metric-valid mask mismatch across cases")
            continue
        if not mask_a[ijk_a]:
            # Both sides missing: NaN patterns must agree (fitted-but-unphysical
            # voxels still carry a tensor; fully excluded voxels carry all NaN).
            nan_a = np.isnan(T_a[ijk_a])
            nan_b = np.isnan(T_b[ijk_b])
            if np.any(nan_a != nan_b):
                findings.append(f"{ijk_a}: missing-voxel NaN patterns differ across cases")
            if not np.all(nan_a) and np.max(
                np.abs(np.nan_to_num(T_a[ijk_a]) - np.nan_to_num(T_b[ijk_b]))
            ) > 1e-6:
                findings.append(
                    f"{ijk_a}: unphysical world tensor mismatch "
                    f"{T_a[ijk_a]} vs {T_b[ijk_b]}"
                )
            continue
        if np.max(np.abs(T_a[ijk_a] - T_b[ijk_b])) > 1e-6:
            findings.append(
                f"{ijk_a}: world tensor mismatch "
                f"{T_a[ijk_a]} vs {T_b[ijk_b]}"
            )
        if not np.isclose(FA_a[ijk_a], FA_b[ijk_b], rtol=1e-5, atol=1e-7):
            findings.append(f"{ijk_a}: FA mismatch across world-matched voxels")
        if not np.isclose(MD_a[ijk_a], MD_b[ijk_b], rtol=1e-5, atol=1e-12):
            findings.append(f"{ijk_a}: MD mismatch across world-matched voxels")
        p_a, p_b = P_a[ijk_a], P_b[ijk_b]
        if np.all(np.isnan(p_a)) != np.all(np.isnan(p_b)):
            findings.append(f"{ijk_a}: principal-direction definedness mismatch")
        elif np.all(np.isfinite(p_a)) and np.max(np.abs(p_a - p_b)) > 1e-6:
            findings.append(f"{ijk_a}: world principal direction mismatch")

    return {
        "check": "world-position cross-case correspondence",
        "n_voxels_matched": len(mapping),
        "findings": findings,
        "ok": not findings,
    }
