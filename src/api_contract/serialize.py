"""Turn concrete call inputs into an exact request target.

Rule of thumb used everywhere below: percent-encode each *data atom* first,
then join atoms with the structural separators of its style.  A comma inside
an array element therefore stays ``%2C`` and can never be mistaken for the
form separator, and ``/``/space/``#`` in a path atom can never alter the URL
structure.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlsplit

from jsonschema import Draft7Validator

from .spec import InputError, OperationPlan, ParameterPlan, PlanError

_UNSET = object()


# --------------------------------------------------------------------------- #
# Encoding helpers
# --------------------------------------------------------------------------- #


def encode_atom(text: str) -> str:
    """UTF-8 percent-encoding for a single data atom, structural chars escaped."""
    return quote(text, safe="", encoding="utf-8", errors="strict")


def _atom_text(value: Any, schema: Dict[str, Any], input_loc: str, spec_loc: str) -> str:
    """Render a validated primitive as its textual atom, preserving type."""
    typ = schema.get("type")
    if typ == "boolean":
        if not isinstance(value, bool):
            raise InputError("expected boolean", input_loc, spec_loc)
        return "true" if value else "false"
    if typ == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise InputError("expected integer", input_loc, spec_loc)
        return str(value)
    if typ == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise InputError("expected number", input_loc, spec_loc)
        return json.dumps(value)
    if typ == "string":
        if not isinstance(value, str):
            raise InputError("expected string", input_loc, spec_loc)
        return value
    raise InputError(
        f"cannot serialize schema type {typ!r} in path/query", input_loc, spec_loc
    )


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def openapi_schema_for_jsonschema(schema: Any) -> Any:
    """Deep-copy a schema, translating OpenAPI ``nullable`` for Draft 7.

    OpenAPI 3.0 has no JSON-Schema unions, so ``nullable: true`` beside
    ``type: X`` becomes ``type: [X, "null"]``.
    """
    if isinstance(schema, list):
        return [openapi_schema_for_jsonschema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    out = {k: openapi_schema_for_jsonschema(v) for k, v in schema.items()}
    if out.pop("nullable", False) is True:
        typ = out.get("type")
        if isinstance(typ, str):
            out["type"] = [typ, "null"]
        elif isinstance(typ, list):
            if "null" not in typ:
                out["type"] = typ + ["null"]
    return out


def _first_error(validator: Draft7Validator, value: Any) -> Optional[Any]:
    # Normalise tokens to strings so errors with mixed path token types remain
    # sortable (deterministic first error, never a TypeError).
    errors = sorted(
        validator.iter_errors(value),
        key=lambda e: [str(token) for token in e.absolute_path]
        + [str(token) for token in e.schema_path],
    )
    return errors[0] if errors else None


def _escape_pointer_token(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _error_locations(error: Any, input_loc: str, spec_loc: str) -> Tuple[str, str]:
    """Map a jsonschema error onto an input pointer and a spec pointer.

    Most errors carry a useful ``absolute_path``; ``additionalProperties``
    and ``required`` are reported at the parent object, so we recover the
    specific key from the instance/schema to keep the location actionable.
    """
    input_path = list(error.absolute_path)
    spec_path = list(error.schema_path)

    if error.validator == "additionalProperties":
        schema = error.schema
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        extras = sorted(
            key
            for key in (error.instance or {})
            if key not in properties
        )
        if extras:
            input_path = input_path + [extras[0]]
            # Point at the ``additionalProperties`` restriction, not at a
            # property that the spec never declares.
            if "additionalProperties" not in spec_path:
                spec_path = spec_path + ["additionalProperties"]
    elif error.validator == "required":
        required = error.validator_value or []
        present = set(error.instance or {}) if isinstance(error.instance, dict) else set()
        missing = sorted(name for name in required if name not in present)
        if missing:
            input_path = input_path + [missing[0]]
            spec_path = spec_path + [missing[0]]

    input_pointer = input_loc + "".join(
        f"/{_escape_pointer_token(str(token))}" for token in input_path
    )
    spec_pointer = spec_loc + "".join(
        f"/{_escape_pointer_token(str(token))}" for token in spec_path
    )
    return input_pointer, spec_pointer


def _validate_param(param: ParameterPlan, value: Any, input_loc: str) -> None:
    _reject_null(value, input_loc, param.spec_location)
    transformed = openapi_schema_for_jsonschema(param.schema)
    err = _first_error(Draft7Validator(transformed), value)
    if err is not None:
        input_pointer, spec_pointer = _error_locations(
            err, input_loc, param.spec_location + "/schema"
        )
        raise InputError(err.message, input_pointer, spec_pointer)


def _reject_null(value: Any, input_loc: str, spec_loc: str) -> None:
    if value is None:
        raise InputError("null is not supported in path/query", input_loc, spec_loc)
    if isinstance(value, dict):
        for key, child in value.items():
            _reject_null(child, f"{input_loc}/{key}", spec_loc)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_null(child, f"{input_loc}/{index}", spec_loc)


def _validate_body(plan_body: Any, value: Any, input_loc: str) -> None:
    transformed = openapi_schema_for_jsonschema(plan_body.schema)
    err = _first_error(Draft7Validator(transformed), value)
    if err is not None:
        input_pointer, spec_pointer = _error_locations(
            err, input_loc, plan_body.spec_location
        )
        raise InputError(err.message, input_pointer, spec_pointer)


# --------------------------------------------------------------------------- #
# Request plan
# --------------------------------------------------------------------------- #


@dataclass
class QueryPair:
    key: str              # percent-encoded key as it appears in the target
    value: str            # percent-encoded value as it appears in the target
    parameter: str        # OpenAPI parameter name
    logical_key: str      # e.g. "filter[min_age]"
    atom: Optional[str]   # undecoded textual atom (None for an empty value pair)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "parameter": self.parameter,
            "logical_key": self.logical_key,
            "atom": self.atom,
        }


@dataclass
class ParameterProvenance:
    name: str
    location: str
    source: str
    provided: bool
    omitted: Optional[str]
    style: Optional[str]
    explode: Optional[bool]
    required: bool
    spec_location: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "in": self.location,
            "source": self.source,
            "provided": self.provided,
            "omitted": self.omitted,
            "style": self.style,
            "explode": self.explode,
            "required": self.required,
            "spec_location": self.spec_location,
        }


@dataclass
class RequestPlan:
    operation_id: str
    method: str
    path_template: str
    base_url: Optional[str]
    path: str
    query: str
    target: str
    url: Optional[str]
    query_pairs: List[QueryPair]
    parameters: List[ParameterProvenance]
    body: Optional[Dict[str, Any]]
    headers: Dict[str, str]
    body_bytes: Optional[bytes]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "operation": {
                "operationId": self.operation_id,
                "method": self.method,
                "path_template": self.path_template,
            },
            "base_url": self.base_url,
            "target": self.target,
            "url": self.url,
            "path": self.path,
            "query": self.query,
            "parameters": [p.to_dict() for p in self.parameters],
            "query_pairs": [p.to_dict() for p in self.query_pairs],
            "body": (
                None
                if self.body is None
                else {
                    "source": "body",
                    "present": True,
                    "json": self.body["json"],
                    "bytes": self.body["bytes"],
                    "content_type": self.body["content_type"],
                    "spec_location": self.body["spec_location"],
                }
            ),
            "headers": dict(self.headers),
        }


# --------------------------------------------------------------------------- #
# Serialisation of individual parameters
# --------------------------------------------------------------------------- #


def _primitive_schema(schema: Dict[str, Any], location: str) -> Dict[str, Any]:
    typ = schema.get("type")
    if typ not in ("string", "integer", "number", "boolean"):
        raise PlanError(f"non-primitive schema cannot be serialized [{location}]")
    return schema


def _render_query_parameter(
    param: ParameterPlan, value: Any
) -> List[Tuple[str, str, str, Optional[str]]]:
    """Return (encoded_key, encoded_value, logical_key, atom) tuples."""
    loc = f"parameters/{param.name}"
    base_key = encode_atom(param.name)
    typ = param.schema["type"]

    if typ == "array":
        items_schema = _primitive_schema(
            param.schema["items"], f"{param.spec_location}/schema/items"
        )
        atoms = [
            _atom_text(
                item,
                items_schema,
                f"{loc}/{index}",
                f"{param.spec_location}/schema/items",
            )
            for index, item in enumerate(value)
        ]
        if not atoms:
            return []  # empty query array: omitted
        if param.explode:
            return [
                (base_key, encode_atom(atom), param.name, atom) for atom in atoms
            ]
        # Structural comma stays literal, atoms stay individually escaped.
        joined = ",".join(encode_atom(atom) for atom in atoms)
        return [(base_key, joined, param.name, None)]

    if typ == "object":  # deepObject
        properties: Dict[str, Any] = param.schema.get("properties", {})
        ordered = [name for name in properties if name in value]
        extras = sorted(name for name in value if name not in properties)
        if param.schema.get("additionalProperties") is not True and extras:
            # Already caught by validation; keep this as a belt-and-braces check.
            raise InputError(
                "unknown object property",
                f"{loc}/{extras[0]}",
                f"{param.spec_location}/schema",
            )
        pairs: List[Tuple[str, str, str, Optional[str]]] = []
        for name in ordered + extras:
            child_value = value[name]
            if name in properties:
                child_schema = _primitive_schema(
                    properties[name],
                    f"{param.spec_location}/schema/properties/{name}",
                )
                child_spec = f"{param.spec_location}/schema/properties/{name}"
            else:
                child_schema = {}  # additionalProperties: true -> infer from Python
                child_spec = f"{param.spec_location}/schema/additionalProperties"
            atom = _atom_text(
                child_value, child_schema, f"{loc}/{name}", child_spec
            )
            logical = f"{param.name}[{name}]"
            pairs.append(
                (
                    base_key + "[" + encode_atom(name) + "]",
                    encode_atom(atom),
                    logical,
                    atom,
                )
            )
        return pairs

    # primitive
    atom = _atom_text(
        value,
        _primitive_schema(param.schema, f"{param.spec_location}/schema"),
        loc,
        f"{param.spec_location}/schema",
    )
    return [(base_key, encode_atom(atom), param.name, atom)]


def _render_path(plan: OperationPlan, parameters: Dict[str, Any]) -> Tuple[str, List[ParameterProvenance]]:
    path = plan.path_template
    provenance: List[ParameterProvenance] = []
    for name in plan.path_names:
        param = plan.parameter(name, "path")
        assert param is not None  # guaranteed by compile
        provided = name in parameters
        if not provided:
            raise InputError(
                "missing required path parameter",
                f"parameters/{name}",
                param.spec_location,
            )
        value = parameters[name]
        _validate_param(param, value, f"parameters/{name}")
        atom = _atom_text(
            value, param.schema, f"parameters/{name}", param.spec_location + "/schema"
        )
        encoded = encode_atom(atom)
        path = path.replace("{" + name + "}", encoded)
        provenance.append(
            ParameterProvenance(
                name=name,
                location="path",
                source="parameters",
                provided=True,
                omitted=None,
                style=param.style,
                explode=param.explode,
                required=True,
                spec_location=param.spec_location,
            )
        )
    return path, provenance


def _render_query(
    plan: OperationPlan, parameters: Dict[str, Any]
) -> Tuple[List[QueryPair], List[ParameterProvenance]]:
    pairs: List[QueryPair] = []
    provenance: List[ParameterProvenance] = []
    for param in (p for p in plan.parameters if p.location == "query"):
        provided = param.name in parameters
        if not provided:
            if param.required:
                raise InputError(
                    "missing required query parameter",
                    f"parameters/{param.name}",
                    param.spec_location,
                )
            provenance.append(
                ParameterProvenance(
                    name=param.name,
                    location="query",
                    source="parameters",
                    provided=False,
                    omitted="not provided",
                    style=param.style,
                    explode=param.explode,
                    required=param.required,
                    spec_location=param.spec_location,
                )
            )
            continue

        value = parameters[param.name]
        _validate_param(param, value, f"parameters/{param.name}")

        if param.schema["type"] == "array" and not value:
            if param.required:
                raise InputError(
                    "required query array cannot be omitted (empty array)",
                    f"parameters/{param.name}",
                    param.spec_location,
                )
            provenance.append(
                ParameterProvenance(
                    name=param.name,
                    location="query",
                    source="parameters",
                    provided=True,
                    omitted="empty array",
                    style=param.style,
                    explode=param.explode,
                    required=param.required,
                    spec_location=param.spec_location,
                )
            )
            continue

        rendered = _render_query_parameter(param, value)
        if not rendered and param.schema["type"] == "object":
            if param.required:
                raise InputError(
                    "required deepObject cannot be omitted (empty object)",
                    f"parameters/{param.name}",
                    param.spec_location,
                )
            provenance.append(
                ParameterProvenance(
                    name=param.name,
                    location="query",
                    source="parameters",
                    provided=True,
                    omitted="empty object",
                    style=param.style,
                    explode=param.explode,
                    required=param.required,
                    spec_location=param.spec_location,
                )
            )
            continue
        for key, val, logical, atom in rendered:
            pairs.append(
                QueryPair(
                    key=key,
                    value=val,
                    parameter=param.name,
                    logical_key=logical,
                    atom=atom,
                )
            )
        provenance.append(
            ParameterProvenance(
                name=param.name,
                location="query",
                source="parameters",
                provided=True,
                omitted=None,
                style=param.style,
                explode=param.explode,
                required=param.required,
                spec_location=param.spec_location,
            )
        )
    return pairs, provenance


# --------------------------------------------------------------------------- #
# Base URL
# --------------------------------------------------------------------------- #


def compose_base_url(base_url: str) -> Tuple[str, str]:
    """Return (origin_with_prefix, url_path_prefix) for an override base URL."""
    parts = urlsplit(base_url)
    if parts.scheme not in ("http", "https"):
        raise PlanError(f"base URL must use http/https: {base_url!r}")
    if not parts.netloc:
        raise PlanError(f"base URL has no host: {base_url!r}")
    if parts.query or parts.fragment:
        raise PlanError(f"base URL must not contain query or fragment: {base_url!r}")
    prefix = parts.path.rstrip("/")
    origin = f"{parts.scheme}://{parts.netloc}"
    return origin, prefix


# --------------------------------------------------------------------------- #
# Public builder
# --------------------------------------------------------------------------- #


def build_request(
    plan: OperationPlan,
    parameters: Optional[Dict[str, Any]] = None,
    body: Any = _UNSET,
    base_url: Optional[str] = None,
) -> RequestPlan:
    """Validate inputs and build the exact request, without touching the network."""
    if parameters is None:
        parameters = {}
    if not isinstance(parameters, dict):
        raise InputError("parameters must be an object", "parameters", plan.spec_location)

    declared = {p.name for p in plan.parameters}
    for key in parameters:
        if key not in declared:
            raise InputError(
                f"unknown parameter {key!r}",
                f"parameters/{key}",
                plan.spec_location + "/parameters",
            )

    path, path_provenance = _render_path(plan, parameters)
    query_pairs, query_provenance = _render_query(plan, parameters)
    query = "&".join(f"{pair.key}={pair.value}" for pair in query_pairs)

    resolved_base = base_url if base_url is not None else plan.default_base_url
    origin = prefix = None
    if resolved_base is not None:
        origin, prefix = compose_base_url(resolved_base)
        prefix = prefix or ""

    # ``target`` is exactly what goes on the request line after the origin:
    # optional base path prefix + the spec path + query string.
    target = (prefix or "") + path + (f"?{query}" if query else "")
    url = (origin + target) if origin is not None else None

    body_info: Optional[Dict[str, Any]] = None
    body_bytes: Optional[bytes] = None
    headers: Dict[str, str] = {}

    if plan.body is None:
        if body is not _UNSET:
            raise InputError(
                "operation does not accept a request body",
                "body",
                plan.spec_location,
            )
    else:
        if body is _UNSET:
            if plan.body.required:
                raise InputError("missing required request body", "body", plan.body.spec_location)
        else:
            _validate_body(plan.body, body, "body")
            body_bytes = json.dumps(
                body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
            body_info = {
                "json": body,
                "bytes": len(body_bytes),
                "content_type": "application/json",
                "spec_location": plan.body.spec_location,
            }

    provenance = path_provenance + query_provenance
    if plan.body is not None:
        provenance.append(
            ParameterProvenance(
                name="<requestBody>",
                location="body",
                source="body",
                provided=body is not _UNSET,
                omitted=None if body is not _UNSET else "not provided",
                style=None,
                explode=None,
                required=plan.body.required,
                spec_location=plan.body.spec_location,
            )
        )

    return RequestPlan(
        operation_id=plan.operation_id,
        method=plan.method,
        path_template=plan.path_template,
        base_url=resolved_base,
        path=prefix + path if prefix else path,
        query=query,
        target=target,
        url=url,
        query_pairs=query_pairs,
        parameters=provenance,
        body=body_info,
        headers=headers,
        body_bytes=body_bytes,
    )
