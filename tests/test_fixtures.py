"""End-to-end checks on the two shipped real 4D NIfTI fixtures.

The second fixture is the same physical data with two spatial axes swapped,
an improper (det=-1) basis and a correspondingly re-expressed gradient table.
These tests pin down the behaviours required by the README rather than any
particular numeric ranking.
"""

from __future__ import annotations

import json
import os

import nibabel as nib
import numpy as np
import pytest

from dtifit import fit_case
from dtifit.geometry import FiducialError, image_to_world_rotation
from dtifit.gradients import GradientError
from dtifit.verify import verify_case, verify_cross_case, world_center_mapping

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "fixtures")


@pytest.fixture(scope="module")
def plan() -> dict:
    with open(os.path.join(FIXTURES, "plan.json"), encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def fits(plan) -> dict:
    out = {}
    for case in plan["cases"]:
        name = os.path.splitext(case["image"].replace(".nii", ""))[0]
        out[name] = fit_case(
            os.path.join(FIXTURES, case["image"]),
            os.path.join(FIXTURES, case["gradients"]),
            plan,
            name,
        )
    return out


def test_scaled_signal_is_read_in_physical_units(fits):
    # int16 payload 2000 * scl_slope 0.5 = 1000 physical at b0.
    result = fits["dwi"]
    assert result.data[0, 0, 0, 0] == 1000.0
    raw = nib.load(os.path.join(FIXTURES, "dwi.nii.gz")).dataobj.get_unscaled()
    assert raw[0, 0, 0, 0] == 2000


def test_affine_columns_match_spacings_and_gradient_rotation(fits):
    for name, result in fits.items():
        img = nib.load(os.path.join(FIXTURES, f"{name}.nii.gz"))
        linear = img.get_sform()[:3, :3]
        norms = np.linalg.norm(linear, axis=0)
        np.testing.assert_allclose(norms, img.header.get_zooms()[:3])
        R = image_to_world_rotation(img.get_sform())
        np.testing.assert_allclose(R, result.rotation, atol=1e-15)
        np.testing.assert_allclose(R.T @ R, np.eye(3), atol=1e-6)
        # Directions keep unit length even under the improper basis.
        diff = result.gradients_world[~result.table.b0_mask]
        np.testing.assert_allclose(np.linalg.norm(diff, axis=1), 1.0, atol=1e-6)


def test_permuted_case_uses_improper_basis(fits):
    assert abs(np.linalg.det(fits["dwi"].rotation) - 1.0) < 1e-9
    assert abs(np.linalg.det(fits["dwi-permuted"].rotation) + 1.0) < 1e-9


def test_world_gradients_are_identical_across_cases(fits):
    a = fits["dwi"].gradients_world
    b = fits["dwi-permuted"].gradients_world
    np.testing.assert_allclose(a, b, atol=1e-12)


def test_volume_dimension_matches_gradient_rows(plan):
    from dtifit.gradients import load_gradient_table

    for case in plan["cases"]:
        img = nib.load(os.path.join(FIXTURES, case["image"]))
        grad_path = os.path.join(FIXTURES, case["gradients"])
        # Declaring fewer volumes than the 4th dimension must be refused.
        with pytest.raises(GradientError):
            load_gradient_table(
                grad_path, img.shape[3] - 1, plan["b0_threshold"]
            )
        # The real table matches exactly.
        table = load_gradient_table(grad_path, img.shape[3], plan["b0_threshold"])
        assert table.n_volumes == img.shape[3]
        assert np.count_nonzero(~table.b0_mask) == 10


def test_voxel_exclusion_states_are_preserved(fits):
    result = fits["dwi"]
    statuses = {tuple(d["voxel_ijk"]): d["status"] for d in result.diagnostics}

    # 7 of 10 diffusion volumes missing -> below min_diffusion_rows (6).
    assert statuses[(1, 0, 1)] == "excluded_insufficient_diffusion_rows"
    assert result.n_valid_directions[1, 0, 1] == 3
    assert np.all(np.isnan(result.tensor[1, 0, 1]))

    # Only one valid b0 -> below min_valid_b0 (2).
    assert statuses[(2, 0, 0)] == "excluded_insufficient_valid_b0"
    assert result.n_valid_b0[2, 0, 0] == 1
    assert np.isnan(result.s0[2, 0, 0])

    # One missing diffusion row still leaves 9 >= 6: fitted, reason retained.
    assert statuses[(0, 0, 0)] == "fitted"
    assert result.n_valid_directions[0, 0, 0] == 9
    diag0 = next(d for d in result.diagnostics if d["voxel_ijk"] == [0, 0, 0])
    assert diag0["excluded_volumes"] == {
        5: "dwi_non_positive_or_nonfinite"
    }


def test_negative_eigenvalue_is_flagged_not_repaired(fits, plan):
    result = fits["dwi"]
    ijk = (2, 1, 1)
    assert result.fitted[ijk]
    assert not result.physical[ijk]
    assert not result.metric_valid[ijk]
    assert np.min(result.evals[ijk]) < -plan["eigenvalue_tolerance_mm2_s"]
    assert np.isnan(result.fa[ijk]) and np.isnan(result.md[ijk])
    diag = next(d for d in result.diagnostics if d["voxel_ijk"] == list(ijk))
    assert diag["status"] == "fitted_unphysical_negative_eigenvalue"


def test_isotropic_fa_zero_is_a_valid_value(fits):
    result = fits["dwi"]
    ijk = (0, 0, 1)  # constructed isotropic voxel
    assert result.metric_valid[ijk]
    assert abs(result.fa[ijk]) < 1e-6
    assert not result.principal_defined[ijk]
    assert np.all(np.isnan(result.principal_dir[ijk]))


def test_principal_direction_sign_convention(fits):
    result = fits["dwi"]
    for ijk in np.ndindex(result.spatial_shape):
        if result.principal_defined[ijk]:
            p = result.principal_dir[ijk]
            assert np.isclose(np.linalg.norm(p), 1.0)
            assert p[np.argmax(np.abs(p))] > 0.0


def test_cross_case_world_correspondence(plan, tmp_path):
    out_dir = str(tmp_path)
    for case in plan["cases"]:
        name = os.path.splitext(case["image"].replace(".nii", ""))[0]
        result = fit_case(
            os.path.join(FIXTURES, case["image"]),
            os.path.join(FIXTURES, case["gradients"]),
            plan,
            name,
        )
        from dtifit import write_case_outputs

        write_case_outputs(result, os.path.join(out_dir, name))
        report = verify_case(
            os.path.join(out_dir, name),
            os.path.join(FIXTURES, case["image"]),
            os.path.join(FIXTURES, case["gradients"]),
            plan,
        )
        assert report["ok"], report["findings"]

    cross = verify_cross_case(out_dir, plan, FIXTURES)
    assert cross["ok"], cross["findings"]
    assert cross["n_voxels_matched"] == 12


def test_world_center_mapping_swaps_axes():
    mapping = world_center_mapping(
        os.path.join(FIXTURES, "dwi.nii.gz"),
        os.path.join(FIXTURES, "dwi-permuted.nii.gz"),
    )
    # x/y swap: A voxel (0,1,0) shares its world centre with B (1,0,0).
    assert mapping[(0, 1, 0)] == (1, 0, 0)
    assert mapping[(1, 0, 0)] == (0, 1, 0)


def test_sform_qform_conflict_is_rejected(plan, tmp_path):
    img = nib.load(os.path.join(FIXTURES, "dwi.nii.gz"))
    hdr = img.header.copy()
    bad = img.get_sform()
    bad[0, 3] += 0.01  # 1e-2 > 1e-5 tolerance
    hdr.set_qform(bad, code=1)
    path = str(tmp_path / "conflict.nii.gz")
    nib.save(nib.Nifti1Image(img.dataobj.get_unscaled(), img.affine, hdr), path)
    with pytest.raises(FiducialError, match="sform/qform conflict"):
        fit_case(path, os.path.join(FIXTURES, "gradients.tsv"), plan, "conflict")
