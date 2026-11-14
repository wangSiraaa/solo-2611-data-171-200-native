"""Spec-driven local API client (OpenAPI 3.0 subset).

Public entry points:
    compile_operation(spec, operation_id, base_url=None) -> OperationPlan
    Client(...).compile(...) / Client.plan(...) / Client.call(...)
"""

from .client import CallResult, Client, compile_operation
from .spec import CompileError, InputError, OperationPlan, PlanError

__all__ = [
    "CallResult",
    "Client",
    "CompileError",
    "InputError",
    "OperationPlan",
    "PlanError",
    "compile_operation",
]
