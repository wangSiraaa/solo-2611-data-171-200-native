"""Affine / direction-frame geometry.

The gradient frame is declared as ``voxel_axes``: each gradient vector is
expressed in the basis of the *image* axes.  Mapping it to world RAS must use
only the normalized (spacing-stripped) affine columns, never the voxel
spacings and never the translation.  A flipped/handedness-reversed basis
(``det R = -1``) is explicitly allowed; an oblique non-orthogonal basis
(shear) is not.
"""

from __future__ import annotations

import numpy as np

#: max element-wise distance allowed between the authoritative sform and qform
QFORM_TOL = 1e-5
#: column norms must match the header zooms this closely
SPACING_TOL = 1e-6
#: R^T R must be the identity to this tolerance for an orthogonal basis
ORTHO_TOL = 1e-6


class FiducialError(ValueError):
    """Raised when an image/gradient fiducial check fails."""


def validate_affine(header, affine: np.ndarray) -> np.ndarray:
    """Validate the spatial transform of a loaded NIfTI.

    Rules (from README/plan):

    * spatial unit must be millimetres;
    * a positive sform_code is authoritative; qform, when coded, must agree
      element-wise within ``QFORM_TOL``.  A quaternion qform can only encode
      an orthogonal (shear-free) linear part, so on a sheared sform the qform
      comparison is skipped in favour of the explicit shear rejection below;
    * the three spatial affine columns must have non-zero norms equal to the
      header zooms (spacings) -- those norms are the voxel spacings;
    * the normalized columns must form an orthonormal basis (rotation,
      possibly improper, ``det = +/-1``); any shear is rejected.

    Returns the validated 3x3 linear part ``A``.
    """
    spatial_unit, _ = header.get_xyzt_units()
    if spatial_unit != "mm":
        raise FiducialError(
            f"spatial unit must be 'mm', got {spatial_unit!r}; direction and "
            "tensor conventions assume millimetre world coordinates"
        )

    if int(header["sform_code"]) <= 0:
        raise FiducialError(
            f"sform_code must be positive and authoritative, got "
            f"{int(header['sform_code'])}"
        )

    linear = np.asarray(affine[:3, :3], dtype=float)
    if linear.shape != (3, 3):
        raise FiducialError(f"affine linear part must be 3x3, got {linear.shape}")

    zooms = np.asarray(header.get_zooms()[:3], dtype=float)
    col_norms = np.linalg.norm(linear, axis=0)
    if np.any(col_norms <= 0.0):
        raise FiducialError(f"affine has zero-length column(s): norms={col_norms}")
    if np.max(np.abs(col_norms - zooms)) > SPACING_TOL:
        raise FiducialError(
            "affine column norms do not match header zooms/spacings: "
            f"norms={col_norms}, zooms={zooms}"
        )

    rotation = linear / col_norms  # strip voxel spacing from each axis vector
    gram = rotation.T @ rotation
    gram_err = float(np.max(np.abs(gram - np.eye(3))))
    is_sheared = gram_err > ORTHO_TOL

    qform_code = int(header["qform_code"])
    if qform_code > 0 and not is_sheared:
        # An orthogonal sform is exactly representable as a quaternion qform;
        # any element-wise disagreement beyond tolerance is a real conflict.
        try:
            qform = header.get_qform()
        except Exception as exc:  # pragma: no cover - nibabel guarantees shape
            raise FiducialError(
                f"qform declared (code {qform_code}) but unreadable: {exc}"
            )
        diff = float(np.max(np.abs(qform - affine)))
        if diff > QFORM_TOL:
            raise FiducialError(
                f"sform/qform conflict: max element difference {diff:.3e} "
                f"exceeds tolerance {QFORM_TOL:.0e}"
            )

    if is_sheared:
        raise FiducialError(
            "normalized affine columns are not orthonormal (shear or "
            f"non-orthogonal axes): max |R^T R - I| = {gram_err:.3e} > "
            f"{ORTHO_TOL:.0e}"
        )

    det = float(np.linalg.det(rotation))
    if abs(abs(det) - 1.0) > ORTHO_TOL:
        raise FiducialError(f"normalized basis determinant is not +/-1: {det}")
    return linear


def image_to_world_rotation(affine: np.ndarray) -> np.ndarray:
    """Return the spacing-stripped orthogonal matrix R with g_world = R g_input.

    The input gradients live on image axes.  Affine column *j* is the world
    vector travelled per image-axis step; dividing by its norm (the spacing)
    gives the unit axis, so the matrix of normalized columns maps a
    unit-length image-axis direction to a unit-length world-RAS direction.
    Spacings therefore never enter the direction length.
    """
    linear = np.asarray(affine[:3, :3], dtype=float)
    col_norms = np.linalg.norm(linear, axis=0)
    return linear / col_norms
