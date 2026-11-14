from __future__ import annotations


class PlannerError(Exception):
    """Base class for all planner errors (all are user-facing and descriptive)."""


class ValidationError(PlannerError):
    """Structural problem: unknown metric/dimension/fact, malformed model or query."""


class Unplannable(PlannerError):
    """Request cannot be answered without inventing semantics (e.g. allocation).

    ``paths`` carries the concrete attempted paths so a reviewer can see *why*
    the request is impossible, not just that it was refused.
    """

    def __init__(self, message: str, paths: list[dict] | None = None):
        super().__init__(message)
        self.paths = paths or []


class CardinalityError(PlannerError):
    """A declared cardinality does not hold on the actual data.

    ``violations`` holds the critical records (offending keys and counts) found
    during the pre-execution verification.
    """

    def __init__(self, message: str, violations: list[dict] | None = None):
        super().__init__(message)
        self.violations = violations or []
