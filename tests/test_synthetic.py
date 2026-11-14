"""Formula checks on controlled synthetic tensors and rejection-path tests.

No brain imagery is involved: signals are generated directly from the
Stejskal-Tanner relation for known tensors, so the fitted six components, FA,
MD and principal direction must reproduce the constructed values to numerical
precision.  Runs as a plain script (assertions) and under pytest.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import nibabel as nib
import numpy as np

from dtifit import fit_case
from dtifit.geometry import FiducialError, image_to_world_rotation, validate_affine
from dtifit.gradients import GradientError, load_gradient_table
from dtifit.verify import verify_case

PLAN = {
    "gradient_frame": "voxel_axes",
    "b0_threshold": 50,
    "min_valid_b0": 2,
    "min_diffusion_rows": 6,
    "max_design_condition": 1_000_000,
    "positive_signal_only": True,
    "eigenvalue_tolerance_mm2_s": 1e-7,
    "principal_gap_relative": 0.02,
}

# Same 13-volume scheme as the fixtures (3 b0 + 10 unit directions).
GRAD_ROWS = [
    (0, 0, 0.0, 0.0, 0.0),
    (1, 0, 0.0, 0.0, 0.0),
    (2, 0, 0.0, 0.0, 0.0),
    (3, 1000, 1.0, 0.0, 0.0),
    (4, 1000, 0.0, 1.0, 0.0),
    (5, 1000, 0.0, 0.0, 1.0),
    (6, 1000, 0.7071067811865475, 0.7071067811865475, 0.0),
    (7, 1000, 0.7071067811865475, -0.7071067811865475, 0.0),
    (8, 1000, 0.7071067811865475, 0.0, 0.7071067811865475),
    (9, 1000, 0.7071067811865475, 0.0, -0.7071067811865475),
    (10, 1000, 0.0, 0.7071067811865475, 0.7071067811865475),
    (11, 1000, 0.0, 0.7071067811865475, -0.7071067811865475),
    (12, 1000, 0.5773502691896258, 0.5773502691896258, 0.5773502691896258),
]

# Non-axis-aligned orthogonal affine: spacing 2/3/4 plus an in-plane rotation.
ROT2D = np.array([[np.cos(0.3), -np.sin(0.3)], [np.sin(0.3), np.cos(0.3)]])
SYN_AFFINE = np.eye(4)
SYN_AFFINE[:2, :2] = ROT2D @ np.diag([2.0, 3.0])
SYN_AFFINE[2, 2] = 4.0
SYN_AFFINE[:3, 3] = [-5.0, 7.0, 2.0]


def _write_case(tmp: str, tensors6: np.ndarray, affine: np.ndarray,
                grad_rows=GRAD_ROWS, slope: float = 0.05,
                override_series=None) -> tuple:
    """Synthesize S = S0 exp(-b g^T D g) for one or more voxels.

    The physical signal is stored as an int16 payload with an explicit
    ``scl_slope`` (mirroring the fixtures).  A small slope keeps quantization
    far below the formula tolerances used in the assertions.
    """
    spatial = tensors6.shape[:3]
    n_vol = len(grad_rows)
    data = np.zeros(spatial + (n_vol,), dtype=float)
    grads = np.array([[r[2], r[3], r[4]] for r in grad_rows])
    bvals = np.array([r[1] for r in grad_rows])
    for ijk in np.ndindex(spatial):
        t6 = tensors6[ijk]
        D = np.array([
            [t6[0], t6[3], t6[4]],
            [t6[3], t6[1], t6[5]],
            [t6[4], t6[5], t6[2]],
        ])
        s0 = 1000.0
        for v, (b, g) in enumerate(zip(bvals, grads)):
            if b <= PLAN["b0_threshold"]:
                data[ijk + (v,)] = s0
            else:
                data[ijk + (v,)] = s0 * np.exp(-b * g @ D @ g)
    if override_series is not None:
        data = override_series

    # Store as int16 payload with a real scl_slope, like the fixtures.
    scaled = np.rint(data / slope).astype(np.int16)
    assert np.max(np.abs(scaled)) < np.iinfo(np.int16).max
    img = nib.Nifti1Image(scaled, affine)
    img.header.set_xyzt_units("mm", "sec")
    img.header.set_sform(affine, 1)
    img.header.set_qform(affine, 1)
    img.header["scl_slope"] = slope
    img.header["scl_inter"] = 0.0
    image_path = os.path.join(tmp, "syn.nii.gz")
    nib.save(img, image_path)

    grad_path = os.path.join(tmp, "syn.tsv")
    with open(grad_path, "w", encoding="utf-8") as fh:
        fh.write("volume\tb_s_mm2\tgx\tgy\tgz\n")
        for row in grad_rows:
            fh.write("\t".join(str(x) for x in row) + "\n")
    return image_path, grad_path


def expected_fa(eigvals: np.ndarray) -> float:
    md = float(np.mean(eigvals))
    return float(np.sqrt(1.5 * np.sum((eigvals - md) ** 2) / np.sum(eigvals**2)))


def test_isotropic_tensor():
    """Isotropic D = d I: FA must be exactly 0 and stay a *valid* value."""
    with tempfile.TemporaryDirectory() as tmp:
        d = 0.0007
        tensors = np.full((1, 1, 1, 6), 0.0)
        tensors[0, 0, 0, 0:3] = d
        image_path, grad_path = _write_case(tmp, tensors, SYN_AFFINE)
        result = fit_case(image_path, grad_path, PLAN, "syn_iso")

        assert result.metric_valid[0, 0, 0]
        # int16 payload (slope 0.05) sets the achievable numerical floor.
        assert np.isclose(result.fa[0, 0, 0], 0.0, atol=1e-7)
        assert abs(result.md[0, 0, 0] - d) < 1e-7
        assert not result.principal_defined[0, 0, 0]
        assert np.all(np.isnan(result.principal_dir[0, 0, 0]))
        diag = result.diagnostics[0]
        assert diag["status"] == "fitted_tied_principal_eigenvalues_direction_undefined"
        print("isotropic: FA=0 valid, MD recovered, no spurious principal direction")


def test_diagonal_anisotropic_tensor_with_rotation():
    """Known diagonal tensor under an oblique affine: world tensor recovered."""
    with tempfile.TemporaryDirectory() as tmp:
        eigvals = np.array([0.0003, 0.0005, 0.0015])
        tensors = np.zeros((1, 1, 1, 6))
        tensors[0, 0, 0, 0:3] = eigvals  # D diagonal in *image* axes
        image_path, grad_path = _write_case(tmp, tensors, SYN_AFFINE)
        result = fit_case(image_path, grad_path, PLAN, "syn_diag")

        # The fit is in world coordinates.  Rotate the known image-frame
        # diagonal tensor with the same R used for the gradients.
        R = image_to_world_rotation(SYN_AFFINE)
        D_image = np.diag(eigvals)
        D_world_expected = R @ D_image @ R.T
        t6 = result.tensor[0, 0, 0]
        D_world = np.array([
            [t6[0], t6[3], t6[4]],
            [t6[3], t6[1], t6[5]],
            [t6[4], t6[5], t6[2]],
        ])
        assert np.max(np.abs(D_world - D_world_expected)) < 1e-7
        # Eigenvalues are rotation invariant; FA/MD must match construction.
        assert np.allclose(np.sort(result.evals[0, 0, 0]), eigvals, rtol=1e-4, atol=1e-7)
        assert abs(result.md[0, 0, 0] - np.mean(eigvals)) < 1e-7
        assert abs(result.fa[0, 0, 0] - expected_fa(eigvals)) < 5e-5
        assert result.principal_defined[0, 0, 0]
        # Principal direction = R @ image z-axis, sign-normalized.
        expected_dir = R @ np.array([0.0, 0.0, 1.0])
        expected_dir *= np.sign(expected_dir[np.argmax(np.abs(expected_dir))])
        assert np.allclose(result.principal_dir[0, 0, 0], expected_dir, atol=1e-5)
        assert np.isclose(np.linalg.norm(result.principal_dir[0, 0, 0]), 1.0)
        print("diagonal tensor: world rotation, FA/MD and principal direction exact")


def test_negative_signal_excluded_per_voxel():
    """A zero diffusion measurement drops only that row for that voxel."""
    with tempfile.TemporaryDirectory() as tmp:
        d = 0.0007
        tensors = np.full((2, 1, 1, 6), 0.0)
        tensors[..., 0:3] = d
        image_path, grad_path = _write_case(tmp, tensors, np.eye(4))
        # Knock out one diffusion volume in voxel 0 only (9 rows remain).
        img = nib.load(image_path)
        raw = img.dataobj.get_unscaled().copy()
        raw[0, 0, 0, 7] = 0
        fixed = nib.Nifti1Image(raw, img.affine, img.header)
        nib.save(fixed, image_path)

        result = fit_case(image_path, grad_path, PLAN, "syn_missing")
        assert result.n_valid_directions[0, 0, 0] == 9
        assert result.fitted[0, 0, 0]
        assert result.n_valid_directions[1, 0, 0] == 10
        assert result.diagnostics[0]["excluded_volumes"] == {
            7: "dwi_non_positive_or_nonfinite"
        }
        print("per-voxel exclusion: one dropped row, voxel still fitted; neighbour intact")


def test_nifti_roundtrip_preserves_affine_and_scaling():
    """Written products must be readable with the exact affine and zooms."""
    with tempfile.TemporaryDirectory() as tmp:
        tensors = np.zeros((1, 1, 1, 6))
        tensors[0, 0, 0, 0:3] = 0.0007
        image_path, grad_path = _write_case(
            tmp, tensors, SYN_AFFINE, slope=0.5
        )
        # Source scaling must be applied on read.
        src = nib.load(image_path)
        assert src.dataobj.slope == 0.5
        assert np.isclose(src.get_fdata()[0, 0, 0, 0], 1000.0)

        result = fit_case(image_path, grad_path, PLAN, "syn_rt")
        from dtifit import write_case_outputs
        out_dir = os.path.join(tmp, "out")
        write_case_outputs(result, out_dir)

        t_img = nib.load(os.path.join(out_dir, "tensor.nii.gz"))
        # NIfTI-1 stores the affine in float32 sform fields, so exact float64
        # equality is impossible; the stored precision is the format limit.
        assert np.max(np.abs(t_img.affine - SYN_AFFINE)) < 1e-6
        assert tuple(t_img.header.get_zooms()[:3]) == (2.0, 3.0, 4.0)
        assert t_img.header.get_xyzt_units()[0] == "mm"
        # Independent verification passes on the round-tripped files.
        report = verify_case(out_dir, image_path, grad_path, PLAN)
        assert report["ok"], report["findings"]
        print("roundtrip: affine, zooms, units and scaling all preserved and verified")


def test_reject_sheared_affine():
    with tempfile.TemporaryDirectory() as tmp:
        tensors = np.zeros((1, 1, 1, 6))
        tensors[0, 0, 0, 0:3] = 0.0007
        sheared = SYN_AFFINE.copy()
        sheared[0, 1] = 1.2  # non-orthogonal column -> shear
        image_path, grad_path = _write_case(tmp, tensors, sheared)
        # Force sform and qform raw fields to the identical sheared matrix,
        # so the conflict check passes and the orthogonality check must fire.
        img = nib.load(image_path)
        hdr = img.header.copy()
        hdr.set_sform(sheared, code=1)
        hdr.set_qform(sheared, code=1)
        nib.save(nib.Nifti1Image(img.dataobj.get_unscaled(), sheared, hdr), image_path)
        try:
            fit_case(image_path, grad_path, PLAN, "syn_shear")
        except FiducialError as exc:
            assert "orthonormal" in str(exc) or "shear" in str(exc)
            print("rejected sheared affine:", str(exc)[:60])
            return
        raise AssertionError("sheared affine was not rejected")


def test_reject_sform_qform_conflict():
    with tempfile.TemporaryDirectory() as tmp:
        tensors = np.zeros((1, 1, 1, 6))
        tensors[0, 0, 0, 0:3] = 0.0007
        image_path, grad_path = _write_case(tmp, tensors, SYN_AFFINE)
        img = nib.load(image_path)
        bad_qform = SYN_AFFINE.copy()
        bad_qform[0, 3] += 0.01  # 1e-2 > 1e-5 tolerance
        hdr = img.header.copy()
        hdr.set_qform(bad_qform, 1)
        nib.save(nib.Nifti1Image(img.dataobj.get_unscaled(), SYN_AFFINE, hdr), image_path)
        try:
            fit_case(image_path, grad_path, PLAN, "syn_conflict")
        except FiducialError as exc:
            assert "sform/qform conflict" in str(exc)
            print("rejected sform/qform conflict")
            return
        raise AssertionError("sform/qform conflict was not rejected")


def test_reject_bad_gradient_table():
    with tempfile.TemporaryDirectory() as tmp:
        tensors = np.full((1, 1, 1, 6), 0.0)
        tensors[0, 0, 0, 0:3] = 0.0007
        image_path, grad_path = _write_case(tmp, tensors, np.eye(4))
        img = nib.load(image_path)

        # Non-unit diffusion direction.
        bad_rows = [list(r) for r in GRAD_ROWS]
        bad_rows[3] = (3, 1000, 0.9, 0.0, 0.0)
        bad_grad = os.path.join(tmp, "bad.tsv")
        with open(bad_grad, "w", encoding="utf-8") as fh:
            fh.write("volume\tb_s_mm2\tgx\tgy\tgz\n")
            for row in bad_rows:
                fh.write("\t".join(str(x) for x in row) + "\n")
        try:
            fit_case(image_path, bad_grad, PLAN, "syn_badgrad")
        except GradientError as exc:
            assert "unit length" in str(exc)
        else:
            raise AssertionError("non-unit gradient direction was not rejected")

        # Volume index out of range / not covering every volume.
        miss_rows = GRAD_ROWS[:-1]
        miss_grad = os.path.join(tmp, "miss.tsv")
        with open(miss_grad, "w", encoding="utf-8") as fh:
            fh.write("volume\tb_s_mm2\tgx\tgy\tgz\n")
            for row in miss_rows:
                fh.write("\t".join(str(x) for x in row) + "\n")
        try:
            load_gradient_table(miss_grad, img.shape[3], PLAN["b0_threshold"])
        except GradientError as exc:
            assert "13" in str(exc) or "cover" in str(exc)
            print("rejected bad/short gradient tables")
            return
        raise AssertionError("incomplete gradient table was not rejected")


def test_design_rank_check_is_per_voxel():
    """Even with a full-rank global table, a depleted voxel must not fit."""
    with tempfile.TemporaryDirectory() as tmp:
        tensors = np.full((1, 1, 1, 6), 0.0)
        tensors[0, 0, 0, 0:3] = 0.0007
        image_path, grad_path = _write_case(tmp, tensors, np.eye(4))
        img = nib.load(image_path)
        raw = img.dataobj.get_unscaled().copy()
        # Leave only 3 of 10 diffusion rows (< min_diffusion_rows = 6).
        raw[0, 0, 0, 6:13] = 0
        nib.save(nib.Nifti1Image(raw, img.affine, img.header), image_path)
        result = fit_case(image_path, grad_path, PLAN, "syn_depleted")
        assert not result.fitted[0, 0, 0]
        assert result.diagnostics[0]["status"] == "excluded_insufficient_diffusion_rows"
        assert np.all(np.isnan(result.tensor[0, 0, 0]))
        assert np.all(np.isnan(result.fa[0, 0, 0]))
        print("per-voxel rank/row-count gate rejects depleted voxel despite full-rank table")


def test_rank_deficient_design_with_enough_rows():
    """Six surviving rows are not sufficient when their directions are collinear."""
    with tempfile.TemporaryDirectory() as tmp:
        tensors = np.full((1, 1, 1, 6), 0.0)
        tensors[0, 0, 0, 0:3] = 0.0007
        # Six distinct volumes but every diffusion row points along image x.
        collinear = [
            (0, 0, 0.0, 0.0, 0.0),
            (1, 0, 0.0, 0.0, 0.0),
            (2, 0, 0.0, 0.0, 0.0),
            (3, 1000, 1.0, 0.0, 0.0),
            (4, 1000, 1.0, 0.0, 0.0),
            (5, 1000, 1.0, 0.0, 0.0),
            (6, 1000, 1.0, 0.0, 0.0),
            (7, 1000, 1.0, 0.0, 0.0),
            (8, 1000, 1.0, 0.0, 0.0),
            (9, 1000, 1.0, 0.0, 0.0),
            (10, 1000, 1.0, 0.0, 0.0),
            (11, 1000, 1.0, 0.0, 0.0),
            (12, 1000, 1.0, 0.0, 0.0),
        ]
        image_path, grad_path = _write_case(tmp, tensors, np.eye(4), grad_rows=collinear)
        result = fit_case(image_path, grad_path, PLAN, "syn_rankdef")
        assert result.n_valid_directions[0, 0, 0] == 10  # row-count gate passes
        assert not result.fitted[0, 0, 0]
        assert result.diagnostics[0]["status"] == "excluded_design_rank_deficient"
        assert np.all(np.isnan(result.tensor[0, 0, 0]))
        print("rank-deficient voxel rejected even with enough surviving rows")


def test_ill_conditioned_full_rank_design_rejected():
    """Full rank but condition number above plan max must also be rejected."""
    with tempfile.TemporaryDirectory() as tmp:
        tensors = np.full((1, 1, 1, 6), 0.0)
        tensors[0, 0, 0, 0:3] = 0.0007
        eps = 1e-7  # near-duplicate directions -> condition number > 1e6
        rows = [
            (0, 0, 0.0, 0.0, 0.0),
            (1, 0, 0.0, 0.0, 0.0),
            (2, 0, 0.0, 0.0, 0.0),
            (3, 1000, 1.0, 0.0, 0.0),
            (4, 1000, 0.0, 1.0, 0.0),
            (5, 1000, 0.0, 0.0, 1.0),
            (6, 1000, 1.0, eps, 0.0),
            (7, 1000, 1.0, -eps, 0.0),
            (8, 1000, 1.0, 0.0, eps),
            (9, 1000, 1.0, 0.0, -eps),
            (10, 1000, 0.0, 1.0, eps),
            (11, 1000, 0.0, 1.0, -eps),
            (12, 1000, 1.0 / np.sqrt(3), 1.0 / np.sqrt(3), 1.0 / np.sqrt(3)),
        ]
        rows = [
            (v, b, gx / np.linalg.norm([gx, gy, gz]) if b > 0 else gx,
             gy / np.linalg.norm([gx, gy, gz]) if b > 0 else gy,
             gz / np.linalg.norm([gx, gy, gz]) if b > 0 else gz)
            for (v, b, gx, gy, gz) in rows
        ]
        image_path, grad_path = _write_case(tmp, tensors, np.eye(4), grad_rows=rows)
        result = fit_case(image_path, grad_path, PLAN, "syn_illcond")
        diag = result.diagnostics[0]
        assert not result.fitted[0, 0, 0]
        assert diag["status"] == "excluded_design_condition_too_large"
        assert diag["condition_number"] > PLAN["max_design_condition"]
        print(f"ill-conditioned full-rank design rejected (cond={diag['condition_number']:.2e})")


def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} synthetic checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
