"""Diagnostic exceptions for the NMO correction pipeline.

Every abnormal situation (unknown format, contradictory headers,
unsupported trace content, velocity table defects) raises one of these
with a message that names the offending file/field/value, so operators
get a precise diagnosis instead of silently wrong geophysics.
"""


class NmoError(Exception):
    """Base class for all pipeline diagnostics."""


class UnsupportedFormatError(NmoError):
    """SEG-Y sample format / revision this pipeline does not support."""


class HeaderContradictionError(NmoError):
    """Binary header, trace headers or actual data disagree."""


class VelocityTableError(NmoError):
    """Velocity table is unusable: foldback, non-positive velocity,
    missing CDP, or output times outside table coverage."""


class PlanError(NmoError):
    """The processing plan (output grid / limits) is invalid."""


class NonMonotonicMappingError(NmoError):
    """tx(t0) is not strictly increasing for some trace (velocity
    foldback); the one-to-one time remapping assumption is violated."""
