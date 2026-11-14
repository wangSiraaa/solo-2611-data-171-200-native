"""OpenAPI 3.0 subset: local $ref resolution and operation compilation.

Only what the task supports is understood:
  * operationId lookup across paths
  * local JSON Pointer $ref (#/...) with cycle + missing-target detection
  * path/query parameters with styles: path simple, query form, flat query deepObject
  * JSON requestBody with a local-schema reference
Anything else is rejected at compile time -- never guessed.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

SUPPORTED_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")

SUPPORTED_PARAM_STYLES = {
    "path": {"simple"},
    "query": {"form", "deepObject"},
}
SUPPORTED_SCHEMA_TYPES = {"string", "integer", "number", "boolean", "object", "array"}
# JSON Schema keywords that alter structure; outside this task's scope.
UNSUPPORTED_SCHEMA_KEYWORDS = ("allOf", "anyOf", "oneOf", "not", "if", "then", "else")


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class PlanError(Exception):
    """A problem found while compiling the specification."""


class CompileError(PlanError):
    """Specification cannot be compiled (bad ref, cycle, unsupported style...)."""

    def __init__(self, message: str, spec_location: str = ""):
        self.spec_location = spec_location
        super().__init__(f"{message} [{spec_location}]" if spec_location else message)


class InputError(PlanError):
    """A concrete call input violates the compiled plan."""

    def __init__(self, message: str, input_location: str = "", spec_location: str = ""):
        self.input_location = input_location
        self.spec_location = spec_location
        parts = [message]
        if input_location:
            parts.append(f"input={input_location}")
        if spec_location:
            parts.append(f"spec={spec_location}")
        super().__init__(" | ".join(parts))


# --------------------------------------------------------------------------- #
# Spec loading and $ref resolution
# --------------------------------------------------------------------------- #


def load_spec(source: Any) -> Dict[str, Any]:
    """Load a spec from a dict, a JSON string, or a filesystem path."""
    if isinstance(source, dict):
        return copy.deepcopy(source)
    if isinstance(source, str):
        text = source
        stripped = source.lstrip()
        if not stripped.startswith("{"):
            with open(source, "r", encoding="utf-8") as fh:
                text = fh.read()
        data = json.loads(text)
        if not isinstance(data, dict):
            raise CompileError("OpenAPI document must be a JSON object", "#")
        return data
    raise TypeError("spec must be a dict, JSON string, or path")


def _escape_pointer_token(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _unescape_pointer_token(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


class LocatedDict(dict):
    """A resolved mapping that remembers the spec pointer it came from."""

    origin: Optional[str] = None


class RefResolver:
    """Resolve local JSON Pointer refs, catching missing targets and cycles."""

    def __init__(self, root: Dict[str, Any]):
        self.root = root

    def resolve(self, node: Any, location: str, stack: Tuple[str, ...] = ()):
        """Recursively materialise local $refs inside ``node``.

        ``location`` is a displayable spec pointer for error messages.
        """
        if isinstance(node, list):
            return [
                self.resolve(item, f"{location}/{i}", stack)
                for i, item in enumerate(node)
            ]
        if not isinstance(node, dict):
            return node

        if "$ref" in node:
            ref = node["$ref"]
            if not isinstance(ref, str):
                raise CompileError("$ref must be a string", location)
            if not ref.startswith("#"):
                raise CompileError(
                    "only local JSON Pointer refs are supported", f"{location}/$ref"
                )
            if len(node) > 1:
                # Siblings of $ref are ignored by OpenAPI 3.0; do not guess.
                raise CompileError(
                    "$ref must stand alone (no sibling keys)", location
                )
            if ref in stack:
                cycle = " -> ".join(stack + (ref,))
                raise CompileError(f"cyclic $ref: {cycle}", f"{location}/$ref")
            target = self._lookup(ref, f"{location}/$ref")
            resolved = self.resolve(target, ref, stack + (ref,))
            if isinstance(resolved, dict):
                located = LocatedDict(resolved)
                located.origin = f"{location}/$ref -> {ref}"
                return located
            return resolved

        return {
            key: self.resolve(value, f"{location}/{_escape_pointer_token(key)}", stack)
            for key, value in node.items()
        }

    def _lookup(self, ref: str, location: str) -> Any:
        pointer = ref[1:]
        if pointer == "":
            return self.root
        if not pointer.startswith("/"):
            raise CompileError(f"invalid JSON Pointer: {ref}", location)
        current: Any = self.root
        walked = ""
        for raw in pointer.split("/")[1:]:
            token = _unescape_pointer_token(raw)
            walked += "/" + _escape_pointer_token(token)
            if isinstance(current, dict):
                if token not in current:
                    raise CompileError(
                        f"unresolvable $ref target {ref!r} (missing {walked!r})", location
                    )
                current = current[token]
            elif isinstance(current, list):
                try:
                    index = int(token)
                except ValueError:
                    raise CompileError(
                        f"unresolvable $ref target {ref!r}: bad index {token!r}", location
                    )
                if index < 0 or index >= len(current):
                    raise CompileError(
                        f"unresolvable $ref target {ref!r}: index out of range", location
                    )
                current = current[index]
            else:
                raise CompileError(
                    f"unresolvable $ref target {ref!r}: cannot descend into scalar",
                    location,
                )
        return current


# --------------------------------------------------------------------------- #
# Plan model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ParameterPlan:
    name: str
    location: str  # "path" | "query"
    style: str
    explode: bool
    required: bool
    schema: Dict[str, Any]
    spec_location: str


@dataclass(frozen=True)
class BodyPlan:
    required: bool
    schema: Dict[str, Any]
    spec_location: str


@dataclass(frozen=True)
class OperationPlan:
    operation_id: str
    method: str
    path_template: str
    path_names: Tuple[str, ...]
    parameters: Tuple[ParameterPlan, ...]
    body: Optional[BodyPlan]
    default_base_url: Optional[str]
    spec_location: str

    # -- helpers ---------------------------------------------------------- #

    def parameter(self, name: str, location: str) -> Optional[ParameterPlan]:
        for param in self.parameters:
            if param.name == name and param.location == location:
                return param
        return None


# --------------------------------------------------------------------------- #
# Compilation
# --------------------------------------------------------------------------- #


def _spec_type(schema: Dict[str, Any]) -> str:
    if not isinstance(schema, dict):
        raise CompileError("schema must be an object")
    if "$ref" in schema:
        raise CompileError("unresolved $ref in schema")
    typ = schema.get("type")
    if typ is None:
        # OpenAPI 3.0 allows type to be inferred from properties/items.
        if "properties" in schema:
            return "object"
        if "items" in schema:
            return "array"
        raise CompileError("schema is missing an explicit type")
    if typ not in SUPPORTED_SCHEMA_TYPES:
        raise CompileError(f"unsupported schema type {typ!r}")
    return typ


def _check_schema_supported(schema: Dict[str, Any], location: str) -> None:
    """Reject schema constructs outside the supported scope."""
    if not isinstance(schema, dict):
        raise CompileError("schema must be an object", location)
    if "$ref" in schema:
        raise CompileError("unresolved $ref inside schema", location)
    for keyword in UNSUPPORTED_SCHEMA_KEYWORDS:
        if keyword in schema:
            raise CompileError(
                f"schema composition ({keyword}) is outside the supported scope",
                f"{location}/{keyword}",
            )
    typ = _spec_type(schema)
    if typ == "object":
        props = schema.get("properties", {})
        if not isinstance(props, dict):
            raise CompileError("object schema properties must be an object", location)
        for name, prop in props.items():
            _check_schema_supported(prop, f"{location}/properties/{_escape_pointer_token(name)}")
        if schema.get("additionalProperties") not in (None, False, True):
            raise CompileError(
                "only boolean additionalProperties is supported", location
            )
    elif typ == "array":
        items = schema.get("items")
        if items is None:
            raise CompileError("array schema requires items", location)
        _check_schema_supported(items, f"{location}/items")


def _normalise_parameters(
    raw_params: List[Dict[str, Any]],
    provenance: Dict[Tuple[str, str], Tuple[Dict[str, Any], str]],
    spec_location: str,
) -> List[ParameterPlan]:
    plans: List[ParameterPlan] = []
    seen: Dict[Tuple[str, str], str] = {}
    for idx, raw in enumerate(raw_params):
        # raw already had $refs resolved by RefResolver on the whole document
        name = raw.get("name")
        where = raw.get("in")
        key = (name, where)
        origin = getattr(raw, "origin", None)
        if origin:
            loc = origin
        else:
            loc = provenance.get(key, (raw, f"{spec_location}/parameters/{idx}"))[1]
        if not isinstance(name, str) or not name:
            raise CompileError("parameter is missing name", loc)
        if where not in ("path", "query"):
            raise CompileError(f"unsupported parameter location {where!r}", f"{loc}/in")

        style = raw.get("style", "simple" if where == "path" else "form")
        if style not in SUPPORTED_PARAM_STYLES[where]:
            raise CompileError(
                f"unsupported style {style!r} for {where} parameter", f"{loc}/style"
            )

        explode = raw.get("explode")
        if explode is None:
            explode = style == "form"  # OpenAPI default
        if not isinstance(explode, bool):
            raise CompileError("explode must be a boolean", f"{loc}/explode")

        schema = raw.get("schema")
        if not isinstance(schema, dict):
            raise CompileError("parameter requires a schema object", f"{loc}/schema")
        _check_schema_supported(schema, f"{loc}/schema")

        typ = _spec_type(schema)
        if "type" not in schema:
            # Normalise inferred types so downstream code sees an explicit one.
            schema = dict(schema)
            schema["type"] = typ
        if where == "path" and typ not in ("string", "integer", "number", "boolean"):
            raise CompileError(
                "path simple only supports primitive schemas in this scope",
                f"{loc}/schema",
            )
        if where == "query" and style == "deepObject":
            if typ != "object":
                raise CompileError(
                    "deepObject requires an object schema", f"{loc}/schema"
                )
            if not explode:
                raise CompileError(
                    "deepObject requires explode=true", f"{loc}/explode"
                )
            if schema.get("additionalProperties") is not False:
                # Unknown keys would have to be guessed; refuse anything that
                # does not explicitly close the object.
                raise CompileError(
                    "deepObject requires declared properties "
                    "(additionalProperties: false)",
                    f"{loc}/schema/additionalProperties",
                )
            # Only flat objects: every declared property must be a primitive.
            for prop_name, prop_schema in schema.get("properties", {}).items():
                if _spec_type(prop_schema) not in ("string", "integer", "number", "boolean"):
                    raise CompileError(
                        "deepObject only supports flat primitive properties",
                        f"{loc}/schema/properties/{_escape_pointer_token(prop_name)}",
                    )
        if where == "query" and style == "form":
            if typ == "object":
                raise CompileError(
                    "flat form objects are outside the supported scope", f"{loc}/style"
                )
            if typ == "array" and _spec_type(schema.get("items", {})) not in (
                "string",
                "integer",
                "number",
                "boolean",
            ):
                raise CompileError(
                    "form arrays only support primitive items", f"{loc}/schema/items"
                )

        required = bool(raw.get("required", False))
        if where == "path" and not required:
            # Path parameters are always required by OpenAPI; enforce it.
            required = True

        key = (name, where)
        if key in seen:
            raise CompileError(
                f"conflicting parameter definition {name!r} in {where} "
                f"(also declared at {seen[key]})",
                loc,
            )
        seen[key] = loc

        plans.append(
            ParameterPlan(
                name=name,
                location=where,
                style=style,
                explode=explode,
                required=required,
                schema=schema,
                spec_location=loc,
            )
        )
    return plans


def _path_names(template: str, location: str) -> Tuple[str, ...]:
    names: List[str] = []
    i = 0
    while i < len(template):
        ch = template[i]
        if ch == "}":
            raise CompileError("unbalanced '}' in path template", location)
        if ch == "{":
            end = template.find("}", i + 1)
            if end == -1:
                raise CompileError("unbalanced '{' in path template", location)
            name = template[i + 1 : end]
            if not name or "{" in name:
                raise CompileError("invalid path template parameter", location)
            if name in names:
                raise CompileError(f"duplicate path placeholder {name!r}", location)
            names.append(name)
            i = end + 1
        else:
            i += 1
    return tuple(names)


def compile_operation(
    spec: Any, operation_id: str, base_url: Optional[str] = None
) -> OperationPlan:
    """Compile ``operation_id`` into an OperationPlan, or raise CompileError."""
    root = load_spec(spec)
    resolved = RefResolver(root).resolve(root, "#")
    if resolved.get("openapi", "").split(".")[0] != "3":
        raise CompileError("only OpenAPI 3.0 documents are supported", "#/openapi")

    paths = resolved.get("paths")
    if not isinstance(paths, dict):
        raise CompileError("document has no paths object", "#/paths")

    matches: List[Tuple[str, str, Dict[str, Any]]] = []
    for path_template, item in paths.items():
        if not isinstance(item, dict):
            raise CompileError("path item must be an object", f"#/paths/{path_template}")
        for method in SUPPORTED_METHODS:
            operation = item.get(method)
            if isinstance(operation, dict) and operation.get("operationId") == operation_id:
                matches.append((path_template, method, operation))

    if not matches:
        raise CompileError(f"operationId {operation_id!r} not found", "#/paths")
    if len(matches) > 1:
        where = ", ".join(f"{m.upper()} {p}" for p, m, _ in matches)
        raise CompileError(
            f"operationId {operation_id!r} is ambiguous: {where}", "#/paths"
        )

    path_template, method, operation = matches[0]
    spec_location = f"#/paths/{_escape_pointer_token(path_template)}/{method}"
    path_names = _path_names(path_template, spec_location)

    # Path-level parameters merge with operation-level, operation wins.
    path_item = paths[path_template]
    merged: Dict[Tuple[str, str], Dict[str, Any]] = {}
    ordered_keys: List[Tuple[str, str]] = []
    for source, base_loc in (
        (path_item.get("parameters", []), f"#/paths/{_escape_pointer_token(path_template)}/parameters"),
        (operation.get("parameters", []), f"{spec_location}/parameters"),
    ):
        if not isinstance(source, list):
            raise CompileError("parameters must be an array", base_loc)
        local_seen: Dict[Tuple[str, str], str] = {}
        for idx, raw in enumerate(source):
            if not isinstance(raw, dict):
                raise CompileError("parameter must be an object", f"{base_loc}/{idx}")
            name = raw.get("name")
            where = raw.get("in")
            if not isinstance(name, str) or not name:
                raise CompileError("parameter is missing a string name", f"{base_loc}/{idx}")
            if where not in ("path", "query"):
                raise CompileError(
                    f"unsupported parameter location {where!r}", f"{base_loc}/{idx}/in"
                )
            key = (name, where)
            if key in local_seen:
                raise CompileError(
                    f"conflicting duplicate parameter {name!r} in {where} "
                    f"(already defined at {local_seen[key]})",
                    f"{base_loc}/{idx}",
                )
            local_seen[key] = f"{base_loc}/{idx}"
            # An operation-level parameter may override the path-level one
            # with the same (name, in) -- that is defined behaviour, not a
            # conflict.
            if key not in merged:
                ordered_keys.append(key)
            merged[key] = (raw, f"{base_loc}/{idx}")

    combined: List[Dict[str, Any]] = [merged[key][0] for key in ordered_keys]
    # Pass the per-source provenance map so real spec pointers are reported.
    plans = _normalise_parameters(combined, merged, f"{spec_location}/parameters")

    # _normalise_parameters already reports the real spec pointer for each
    # parameter (including a $ref origin when one was used).
    replanned = plans

    declared_path = {p.name for p in replanned if p.location == "path"}
    missing = [n for n in path_names if n not in declared_path]
    extra = [n for n in declared_path if n not in path_names]
    if missing:
        raise CompileError(
            f"path template placeholders without a parameter: {missing}", spec_location
        )
    if extra:
        raise CompileError(
            f"path parameters not present in template: {extra}", spec_location
        )

    body_plan: Optional[BodyPlan] = None
    if "requestBody" in operation:
        body_plan = _compile_body(operation["requestBody"], spec_location)

    default_base = None
    servers = resolved.get("servers")
    if isinstance(servers, list) and servers:
        first = servers[0]
        if isinstance(first, dict) and isinstance(first.get("url"), str):
            default_base = first["url"]
    if base_url is not None:
        # A caller-supplied base URL only replaces the server address; the
        # path template and operation semantics come from the spec.
        default_base = base_url

    return OperationPlan(
        operation_id=operation_id,
        method=method.upper(),
        path_template=path_template,
        path_names=path_names,
        parameters=tuple(replanned),
        body=body_plan,
        default_base_url=default_base,
        spec_location=spec_location,
    )


def _compile_body(raw: Any, spec_location: str) -> BodyPlan:
    loc = f"{spec_location}/requestBody"
    if not isinstance(raw, dict):
        raise CompileError("requestBody must be an object", loc)
    if "$ref" in raw:
        raise CompileError("unresolved requestBody $ref", loc)
    required = bool(raw.get("required", False))
    content = raw.get("content")
    if not isinstance(content, dict):
        raise CompileError("requestBody requires content", loc)
    if "application/json" not in content:
        raise CompileError(
            "only application/json request bodies are supported", f"{loc}/content"
        )
    media = content["application/json"]
    if not isinstance(media, dict) or not isinstance(media.get("schema"), dict):
        raise CompileError("JSON media type requires a schema", f"{loc}/content/application~1json")
    schema = media["schema"]
    if "$ref" in schema:
        raise CompileError("unresolved $ref in body schema", f"{loc}/content/application~1json/schema")
    _check_schema_supported(schema, f"{loc}/content/application~1json/schema")
    return BodyPlan(
        required=required,
        schema=schema,
        spec_location=f"{loc}/content/application~1json/schema",
    )
