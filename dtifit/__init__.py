"""Minimal diffusion-tensor fit on small 4D NIfTI fixtures.

Implements the fixed linear model described in README.md:
world-RAS symmetric tensor via OLS on log-signal design rows,
FA/MD from eigenvalues, world-coordinate principal direction,
and per-voxel diagnostics with explicit exclusion reasons.
"""

from .geometry import FiducialError, image_to_world_rotation, validate_affine
from .gradients import GradientError, load_gradient_table
from .model import FitResult, fit_case
from .nifti_io import write_case_outputs

__all__ = [
    "FiducialError",
    "GradientError",
    "image_to_world_rotation",
    "validate_affine",
    "load_gradient_table",
    "FitResult",
    "fit_case",
    "write_case_outputs",
]
