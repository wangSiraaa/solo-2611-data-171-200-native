"""compile / call entry points and the HTTP boundary."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx

from .serialize import RequestPlan, build_request, _UNSET
from .spec import (
    OperationPlan,
    PlanError,
    compile_operation as _compile_operation,
)


@dataclass
class CallResult:
    """Outcome of an actual HTTP call -- never a wrapped success object."""

    operation_id: str
    method: str
    base_url: Optional[str]
    target: str
    url: str
    status_code: int
    content_type: Optional[str]
    response_text: str
    json: Any
    is_json: bool
    request_headers: Dict[str, str]
    response_headers: Dict[str, str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "method": self.method,
            "base_url": self.base_url,
            "target": self.target,
            "url": self.url,
            "status_code": self.status_code,
            "content_type": self.content_type,
            "is_json": self.is_json,
            "json": self.json,
            "response_text": self.response_text,
            "request_headers": dict(self.request_headers),
            "response_headers": dict(self.response_headers),
        }


def compile_operation(
    spec: Any, operation_id: str, base_url: Optional[str] = None
) -> OperationPlan:
    """Compile an operation plan from an OpenAPI 3.0 document."""
    return _compile_operation(spec, operation_id, base_url)


class Client:
    """Spec-driven client: compile plans first, then call them."""

    def __init__(
        self,
        spec: Any,
        base_url: Optional[str] = None,
        *,
        timeout: float = 10.0,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        self.spec = spec
        self.base_url = base_url
        self.timeout = timeout
        self._transport = transport
        self._plans: Dict[str, OperationPlan] = {}

    # -- compilation ------------------------------------------------------ #

    def compile(self, operation_id: str) -> OperationPlan:
        plan = compile_operation(self.spec, operation_id, self.base_url)
        self._plans[operation_id] = plan
        return plan

    def plan(
        self,
        operation_id: str,
        parameters: Optional[Dict[str, Any]] = None,
        body: Any = _UNSET,
        *,
        base_url: Optional[str] = None,
    ) -> RequestPlan:
        """Build a RequestPlan. Raises on any invalid input (no network)."""
        plan = self._plans.get(operation_id) or self.compile(operation_id)
        return build_request(
            plan,
            parameters=parameters,
            body=body,
            base_url=base_url,
        )

    # -- network ---------------------------------------------------------- #

    def call(
        self,
        operation_id: str,
        parameters: Optional[Dict[str, Any]] = None,
        body: Any = _UNSET,
        *,
        base_url: Optional[str] = None,
    ) -> CallResult:
        """Build the request, then issue it. Validation happens before I/O."""
        request = self.plan(
            operation_id,
            parameters=parameters,
            body=body,
            base_url=base_url,
        )
        return self.execute(request)

    def execute(self, request: RequestPlan) -> CallResult:
        if request.url is None:
            raise PlanError(
                "no base URL available: pass base_url (spec has no usable servers)"
            )

        # Send exactly the pre-encoded target. httpx does not re-quote an
        # absolute URL given as str, so reserved characters survive intact.
        with httpx.Client(
            timeout=self.timeout, transport=self._transport, follow_redirects=False
        ) as client:
            response = client.request(
                request.method,
                request.url,
                content=request.body_bytes,
                headers=request.headers,
            )

        content_type = response.headers.get("content-type")
        text = response.text
        is_json = False
        parsed: Any = None
        if content_type is not None and "application/json" in content_type.lower():
            try:
                parsed = json.loads(text)
                is_json = True
            except json.JSONDecodeError:
                is_json = False
        return CallResult(
            operation_id=request.operation_id,
            method=request.method,
            base_url=request.base_url,
            target=request.target,
            url=request.url,
            status_code=response.status_code,
            content_type=content_type,
            response_text=text,
            json=parsed,
            is_json=is_json,
            request_headers=dict(request.headers),
            response_headers=dict(response.headers),
        )
