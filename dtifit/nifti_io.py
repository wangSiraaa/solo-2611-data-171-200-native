"""NIfTI output writers and per-voxel diagnostic export.

All image products keep the *input* spatial grid: same affine, same shape and
the same millimetre units.  Missing voxels are stored as NaN (float32) with a
dedicated validity mask so that a genuine isotropic FA = 0 is never confused
with "not computed".
"""

from __future__ import annotations

import csv
import json
import os

import nibabel as nib
import numpy as np

from .model import FitResult

# Last-axis component order for the 6-parameter symmetric tensor.
TENSOR_COMPONENTS = ("Dxx", "Dyy", "Dzz", "Dxy", "Dxz", "Dyz")


def _to_jsonable(obj):
    """Recursively convert numpy scalars/arrays to plain JSON types."""
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, np.generic):
        return obj.item()
    return obj


def _spatial_like(result: FitResult, data: np.ndarray, descrip: str) -> nib.Nifti1Image:
    """Create a NIfTI preserving the input affine and spatial header fields."""
    img = nib.Nifti1Image(np.asarray(data, dtype=np.float32), result.affine)
    in_hdr = result.header
    out = img.header
    # Spatial frame (mm) is inherited; these products have no time axis.
    out.set_xyzt_units(in_hdr.get_xyzt_units()[0], "unknown")
    out.set_qform(result.affine, int(in_hdr["qform_code"]))
    out.set_sform(result.affine, int(in_hdr["sform_code"]))
    out["descrip"] = descrip
    return img


def write_case_outputs(result: FitResult, out_dir: str) -> dict:
    """Write tensor/FA/MD/direction/mask NIfTIs and diagnostics for one case."""
    os.makedirs(out_dir, exist_ok=True)
    paths: dict = {}

    products = {
        "tensor": (
            result.tensor,
            "world-RAS symmetric tensor [Dxx,Dyy,Dzz,Dxy,Dxz,Dyz], mm^2/s",
        ),
        "fa": (result.fa, "fractional anisotropy, dimensionless, NaN=missing"),
        "md": (result.md, "mean diffusivity, mm^2/s, NaN=missing"),
        "eigenvalues": (
            result.evals,
            "ascending eigenvalues lambda1<lambda2<lambda3, mm^2/s",
        ),
        "eigenvectors": (
            result.evecs,
            "columns eigenvectors in world RAS, ascending eigenvalue order",
        ),
        "principal_direction": (
            result.principal_dir,
            "world-RAS principal eigenvector, sign: max|component| positive",
        ),
        "s0": (result.s0, "mean valid b0 signal, source physical units"),
        "residual_sum_sq": (
            result.residual_sum_sq,
            "OLS residual sum of squares over retained rows",
        ),
        "condition_number": (
            result.condition_number,
            "SVD condition number of the per-voxel design matrix",
        ),
    }

    for stem, (arr, descrip) in products.items():
        path = os.path.join(out_dir, f"{stem}.nii.gz")
        nib.save(_spatial_like(result, arr, descrip), path)
        paths[stem] = path

    # Validity masks: float NaN carries the missing state visually; these
    # uint8 masks carry it unambiguously.
    masks = {
        "fitted_mask": result.fitted,
        "physical_mask": result.physical,
        "metric_valid_mask": result.metric_valid,
        "principal_direction_defined_mask": result.principal_defined,
        "missing_results_mask": ~result.metric_valid,
    }
    for stem, arr in masks.items():
        path = os.path.join(out_dir, f"{stem}.nii.gz")
        img = nib.Nifti1Image(arr.astype(np.uint8), result.affine)
        hdr = img.header
        hdr.set_xyzt_units(result.header.get_xyzt_units()[0], "unknown")
        hdr.set_qform(result.affine, int(result.header["qform_code"]))
        hdr.set_sform(result.affine, int(result.header["sform_code"]))
        nib.save(img, path)
        paths[stem] = path

    # Integer counts (no NaN ambiguity).
    for stem, arr in (
        ("n_valid_b0", result.n_valid_b0),
        ("n_valid_directions", result.n_valid_directions),
    ):
        path = os.path.join(out_dir, f"{stem}.nii.gz")
        img = nib.Nifti1Image(arr.astype(np.int16), result.affine)
        hdr = img.header
        hdr.set_xyzt_units(result.header.get_xyzt_units()[0], "unknown")
        hdr.set_qform(result.affine, int(result.header["qform_code"]))
        hdr.set_sform(result.affine, int(result.header["sform_code"]))
        nib.save(img, path)
        paths[stem] = path

    diag_path = os.path.join(out_dir, "voxel_diagnostics.json")
    payload = {
        "case": result.case_name,
        "coordinate_frame": "world RAS; tensor component order "
        + str(list(TENSOR_COMPONENTS)),
        "sign_convention": "principal vector sign fixed so its largest "
        "absolute component is positive",
        "affine": result.affine.tolist(),
        "rotation_voxel_to_world_ras": result.rotation.tolist(),
        "det_rotation": float(np.linalg.det(result.rotation)),
        "world_gradients": [
            {
                "volume": int(result.table.volume[i]),
                "b": float(result.table.bvals[i]),
                "g_world_ras": [float(x) for x in result.gradients_world[i]],
            }
            for i in range(result.table.n_volumes)
        ],
        "voxels": result.diagnostics,
    }
    with open(diag_path, "w", encoding="utf-8") as fh:
        json.dump(_to_jsonable(payload), fh, indent=2, ensure_ascii=False)
    paths["diagnostics_json"] = diag_path

    csv_path = os.path.join(out_dir, "voxel_diagnostics.csv")
    fieldnames = [
        "voxel_index", "voxel_ijk", "status", "n_valid_b0",
        "n_valid_directions", "s0", "condition_number", "residual_sum_sq",
        "fa", "md", "eigenvalues", "excluded_volumes", "notes",
    ]
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        json_keys = ("voxel_ijk", "eigenvalues", "notes", "excluded_volumes")
        for diag in result.diagnostics:
            row = {key: diag.get(key, "") for key in fieldnames}
            for key in json_keys:
                if row[key] != "":
                    row[key] = json.dumps(_to_jsonable(row[key]), ensure_ascii=False)
            writer.writerow(row)
    paths["diagnostics_csv"] = csv_path
    return paths
