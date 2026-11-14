"""Small metric query planner over SQLite (SUM / COUNT / base-quantity ratios).

Pipeline: load model -> validate model against the database -> bind query
(dimensions, metrics, filters) -> compile a per-fact aggregation plan ->
execute -> stitch facts on the union group key -> emit CSV / JSON / plan.
"""

from .errors import PlannerError, Unplannable, CardinalityError, ValidationError
from .model import Model
from .loader import build_database, open_database
from .api import Planner

__all__ = [
    "PlannerError",
    "Unplannable",
    "CardinalityError",
    "ValidationError",
    "Model",
    "build_database",
    "open_database",
    "Planner",
]
