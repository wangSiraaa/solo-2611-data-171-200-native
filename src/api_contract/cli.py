"""Command-line entry points.

    python -m api_contract compile --spec ... [--operation ...]
    python -m api_contract plan    --spec ... --requests ...
    python -m api_contract call    --spec ... --requests ... [--base-url ...]
    python -m api_contract run     --spec ... --requests ... [--base-url ...]

``plan`` writes the request plan and never opens a socket.
``call`` first records the plan, then issues exactly one request per accepted
case; rejected cases contribute zero network requests.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from .client import CallResult, Client
from .serialize import RequestPlan, _UNSET
from .spec import OperationPlan, PlanError


def _load_json(path: Optional[str]) -> Any:
    if path is None:
        return None
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _dump(value: Any) -> None:
    json.dump(value, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def _plan_summary(plan: OperationPlan) -> Dict[str, Any]:
    return {
        "operationId": plan.operation_id,
        "method": plan.method,
        "path_template": plan.path_template,
        "path_parameters": list(plan.path_names),
        "default_base_url": plan.default_base_url,
        "spec_location": plan.spec_location,
        "parameters": [
            {
                "name": p.name,
                "in": p.location,
                "style": p.style,
                "explode": p.explode,
                "required": p.required,
                "schema": p.schema,
                "spec_location": p.spec_location,
            }
            for p in plan.parameters
        ],
        "requestBody": None
        if plan.body is None
        else {
            "required": plan.body.required,
            "schema": plan.body.schema,
            "spec_location": plan.body.spec_location,
        },
    }


def _error_record(exc: PlanError) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "error_type": type(exc).__name__,
        "message": str(exc),
    }
    for attr in ("input_location", "spec_location"):
        value = getattr(exc, attr, None)
        if value:
            record[attr] = value
    return record


def _process_case(
    client: Client,
    case: Dict[str, Any],
    *,
    base_url: Optional[str],
    execute: bool,
) -> Dict[str, Any]:
    case_id = case.get("case_id")
    operation_id = case.get("operationId")
    parameters = case.get("parameters", {})
    # ``body`` is only meaningful when the case explicitly contains the key,
    # so a JSON ``null`` body stays distinguishable from "not supplied".
    body = case["body"] if "body" in case else _UNSET

    record: Dict[str, Any] = {
        "case_id": case_id,
        "operationId": operation_id,
        "network_requests": 0,
        "status": "rejected",
    }
    try:
        request: RequestPlan = client.plan(
            operation_id,
            parameters=parameters,
            body=body,
            base_url=base_url,
        )
        record["status"] = "planned"
        record["request"] = request.to_dict()
        if execute:
            record["network_requests"] = 1
            try:
                result: CallResult = client.execute(request)
            except httpx.HTTPError as exc:
                # The request was attempted but no response was obtained.
                # Record it as such -- never turn it into a fake success.
                record["status"] = "transport_error"
                record["error"] = {
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
                return record
            record["status"] = "called"
            record["result"] = result.to_dict()
    except PlanError as exc:
        record["status"] = "rejected"
        record["rejection"] = _error_record(exc)
    return record


def cmd_compile(args: argparse.Namespace) -> int:
    spec = _load_json(args.spec)
    operation_ids: List[str]
    if args.operation:
        operation_ids = list(args.operation)
    else:
        operation_ids = sorted(
            {
                op.get("operationId")
                for item in spec.get("paths", {}).values()
                if isinstance(item, dict)
                for method, op in item.items()
                if method in ("get", "post", "put", "delete", "patch", "head", "options", "trace")
                and isinstance(op, dict)
                and op.get("operationId")
            }
        )

    plans: List[Dict[str, Any]] = []
    failures = 0
    for operation_id in operation_ids:
        try:
            plan = Client(spec, base_url=args.base_url).compile(operation_id)
            plans.append({"status": "compiled", "plan": _plan_summary(plan)})
        except PlanError as exc:
            failures += 1
            plans.append(
                {"status": "rejected", "operationId": operation_id, "rejection": _error_record(exc)}
            )
    _dump({"plans": plans})
    return 1 if failures else 0


def cmd_run(args: argparse.Namespace, *, execute: bool) -> int:
    spec = _load_json(args.spec)
    cases = _load_json(args.requests)

    # Compile every operation the input mentions, so compile errors surface
    # before any case is planned or sent.
    operation_ids = sorted({c.get("operationId") for c in cases})
    client = Client(spec, base_url=args.base_url)
    compile_records: Dict[str, Dict[str, Any]] = {}
    for operation_id in operation_ids:
        try:
            compile_records[operation_id] = {
                "status": "compiled",
                "plan": _plan_summary(client.compile(operation_id)),
            }
        except PlanError as exc:
            compile_records[operation_id] = {
                "status": "rejected",
                "rejection": _error_record(exc),
            }

    case_records: List[Dict[str, Any]] = []
    for case in cases:
        if compile_records.get(case.get("operationId"), {}).get("status") != "compiled":
            case_records.append(
                {
                    "case_id": case.get("case_id"),
                    "operationId": case.get("operationId"),
                    "status": "rejected",
                    "network_requests": 0,
                    "rejection": compile_records.get(case.get("operationId"), {}).get(
                        "rejection", {"error_type": "CompileError", "message": "not compiled"}
                    ),
                }
            )
            continue
        case_records.append(
            _process_case(client, case, base_url=args.base_url, execute=execute)
        )

    payload = {
        "mode": "call" if execute else "plan",
        "plans": compile_records,
        "cases": case_records,
        "summary": {
            "cases": len(case_records),
            "called": sum(1 for r in case_records if r["status"] == "called"),
            "planned": sum(1 for r in case_records if r["status"] == "planned"),
            "rejected": sum(1 for r in case_records if r["status"] == "rejected"),
            "transport_errors": sum(
                1 for r in case_records if r["status"] == "transport_error"
            ),
            "network_requests": sum(r["network_requests"] for r in case_records),
        },
    }
    _dump(payload)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    # Exit non-zero only if a case was attempted but no response arrived; a
    # rejection is a successful detection, not a failure.
    if execute:
        return 1 if any(
            r["status"] == "transport_error" for r in case_records
        ) else 0
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    return cmd_run(args, execute=False)


def cmd_call(args: argparse.Namespace) -> int:
    return cmd_run(args, execute=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="api_contract")
    sub = parser.add_subparsers(dest="command", required=True)

    compile_p = sub.add_parser("compile", help="compile operation plans")
    compile_p.add_argument("--spec", required=True, help="OpenAPI 3.0 JSON file")
    compile_p.add_argument("--operation", action="append", help="operationId (repeatable)")
    compile_p.add_argument("--base-url", default=None)
    compile_p.set_defaults(func=cmd_compile)

    for name, func in (("plan", cmd_plan), ("call", cmd_call), ("run", cmd_call)):
        p = sub.add_parser(name, help="process request cases")
        p.add_argument("--spec", required=True, help="OpenAPI 3.0 JSON file")
        p.add_argument("--requests", required=True, help="cases JSON file")
        p.add_argument("--base-url", default=None, help="override server base URL")
        p.add_argument("--out", default=None, help="write the report to this path")
        p.set_defaults(func=func)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
