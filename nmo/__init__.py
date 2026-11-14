"""Textbook NMO correction and stacking for small CMP gathers.

Public API:

    plan   = nmo.load_plan("fixtures/plan.json")
    result = nmo.correct("fixtures/gathers.sgy", "fixtures/velocity.csv", plan)
    info   = nmo.explain_sample(result, cdp=101, file_index=7, t0_index=200)
"""

from .core import correct, correct_cdp, is_stretch_muted, nmo_tx, sample_at, stretch_factor
from .errors import (
    HeaderContradictionError,
    NmoError,
    NonMonotonicMappingError,
    PlanError,
    UnsupportedFormatError,
    VelocityTableError,
)
from .explain import explain_sample, format_explanation
from .model import CorrectResult, CdpResult, Plan, Reason, TraceRecord, VelocityFunction
from .plan import load_plan
from .segy_reader import read_segy
from .velocity import load_velocity

__all__ = [
    "correct",
    "correct_cdp",
    "explain_sample",
    "format_explanation",
    "is_stretch_muted",
    "load_plan",
    "load_velocity",
    "nmo_tx",
    "read_segy",
    "sample_at",
    "stretch_factor",
    "CorrectResult",
    "CdpResult",
    "Plan",
    "Reason",
    "TraceRecord",
    "VelocityFunction",
    "NmoError",
    "UnsupportedFormatError",
    "HeaderContradictionError",
    "VelocityTableError",
    "PlanError",
    "NonMonotonicMappingError",
]
