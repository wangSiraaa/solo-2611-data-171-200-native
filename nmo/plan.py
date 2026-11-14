"""Processing-plan loading (fixtures/plan.json) with validation."""

from __future__ import annotations

import json

from .errors import PlanError
from .model import Plan


def load_plan(path: str) -> Plan:
    with open(path) as fh:
        raw = json.load(fh)
    try:
        plan = Plan(
            t0_start_s=float(raw["output_t0_start_s"]),
            t0_step_s=float(raw["output_t0_step_s"]),
            output_count=int(raw["output_count"]),
            stretch_limit=float(raw["stretch_limit"]),
            live_trace_code=int(raw.get("live_trace_code", 1)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PlanError(f"{path}: malformed plan ({exc})") from exc
    if plan.t0_step_s <= 0:
        raise PlanError(f"{path}: output_t0_step_s must be > 0")
    if plan.output_count <= 0:
        raise PlanError(f"{path}: output_count must be > 0")
    if plan.t0_start_s < 0:
        raise PlanError(f"{path}: output_t0_start_s must be >= 0")
    if plan.stretch_limit < 0:
        raise PlanError(f"{path}: stretch_limit must be >= 0")
    interp = raw.get("interpolation", "")
    if "linear" not in interp:
        raise PlanError(
            f"{path}: unsupported interpolation {interp!r}; this pipeline "
            f"implements linear interpolation on recorded trace time only"
        )
    vel_interp = raw.get("velocity_interpolation", "")
    if "linear" not in vel_interp or "no extrapolation" not in vel_interp:
        raise PlanError(
            f"{path}: unsupported velocity_interpolation {vel_interp!r}; "
            f"expected linear in t0 with no extrapolation"
        )
    if raw.get("offset_unit", "metre") not in ("metre", "meter", "m"):
        raise PlanError(
            f"{path}: unsupported offset_unit {raw['offset_unit']!r}"
        )
    return plan
