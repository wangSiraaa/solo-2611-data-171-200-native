"""Exception types shared by the rechunk tool.

Every failure that aborts a conversion is represented by one of these.  The
CLI turns them into a JSON error report and a non-zero exit status.
"""


class RechunkError(Exception):
    """Base class.  ``report`` is filled in by the executor on failure."""

    report = None

    def __init__(self, message, report=None):
        super().__init__(message)
        if report is not None:
            self.report = report


class MetadataError(RechunkError):
    """Source array metadata (.zarray) or layout is not supported."""


class PlanError(RechunkError):
    """The conversion plan is malformed or cannot be satisfied."""


class BudgetExceeded(RechunkError):
    """A live data buffer would exceed the configured budget."""


class CorruptSource(RechunkError):
    """An existing source chunk is truncated, undecodable or the wrong size."""


class SpoolError(RechunkError):
    """A temporary fragment file is missing, truncated or corrupt."""


class VerificationError(RechunkError):
    """An integrity check on the staged target failed before publication."""
