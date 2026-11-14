"""High-level planner facade used by the CLI and tests."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .model import Model
from .planner import (BoundQuery, PlanResult, bind_query, compile_plan,
                      execute)


@dataclass
class PlanBundle:
    """A compiled plan plus the binding, without executing aggregates."""

    bound: BoundQuery
    plan: dict


class Planner:
    def __init__(self, model: Model):
        self.model = model

    def _prepare(self, connection: sqlite3.Connection, query: dict) -> BoundQuery:
        # Validate structure + declared cardinalities *before* planning/executing.
        self.model.validate_against_db(connection)
        return bind_query(self.model, query)

    def run(self, connection: sqlite3.Connection, query: dict) -> PlanResult:
        bound = self._prepare(connection, query)
        return execute(connection, self.model, bound)

    def compile(self, connection: sqlite3.Connection, query: dict) -> PlanBundle:
        """Bind + compile only; performs the semijoin read but no aggregation."""
        bound = self._prepare(connection, query)
        plan = compile_plan(connection, self.model, bound)
        return PlanBundle(bound=bound, plan=plan)

    def plan_only(self, connection: sqlite3.Connection, query: dict) -> PlanBundle:
        return self.compile(connection, query)
