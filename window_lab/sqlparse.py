"""Strict parser for the supported single-SELECT subset.

Only the grammar described by ``fixtures/schema.json`` is accepted:

* one SELECT from exactly one table,
* projections of the identity column and supported window functions
  (``SUM``, ``COUNT``, ``FIRST_VALUE``, ``LAST_VALUE``),
* optional named ``WINDOW`` definitions with legal inheritance,
* explicit ``ROWS``/``GROUPS`` frames or a ``RANGE`` frame (offsets, when
  present, use exactly one integer ordering key),
* no JOIN/WHERE/GROUP BY/HAVING/DISTINCT/LIMIT/set operation/subquery/EXCLUDE.

Anything outside that envelope raises :class:`ParseError` rather than being
silently coerced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import sqlglot
from sqlglot import exp
from sqlglot.dialects.postgres import Postgres
from sqlglot.tokens import TokenType


class ParseError(Exception):
    """Raised when input SQL falls outside the supported subset."""


# sqlglot 25-29 knows ROWS/RANGE but not the GROUPS keyword.  Mapping GROUPS
# onto the ROWS token makes it parseable; sqlglot records the *original token
# text* in ``WindowSpec.kind``, so the true frame mode survives as "GROUPS".
class _GroupsTokenizer(Postgres.Tokenizer):
    KEYWORDS = {**Postgres.Tokenizer.KEYWORDS, "GROUPS": TokenType.ROWS}


class _GroupsDialect(Postgres):
    Tokenizer = _GroupsTokenizer


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderKey:
    column: str
    descending: bool
    nulls_first: bool


@dataclass(frozen=True)
class FrameBound:
    # kind: UNBOUNDED / CURRENT_ROW / OFFSET
    kind: str
    offset: Optional[int] = None
    direction: Optional[str] = None  # PRECEDING / FOLLOWING for OFFSET

    def describe(self) -> str:
        if self.kind == "UNBOUNDED":
            return f"UNBOUNDED {self.direction}"
        if self.kind == "CURRENT_ROW":
            return "CURRENT ROW"
        return f"{self.offset} {self.direction}"


@dataclass(frozen=True)
class FrameSpec:
    mode: str  # ROWS / GROUPS / RANGE
    start: FrameBound
    end: FrameBound
    explicit: bool = True

    def describe(self) -> str:
        return f"{self.mode} BETWEEN {self.start.describe()} AND {self.end.describe()}"


@dataclass
class WindowDefinition:
    name: Optional[str]
    partition_by: list[str]
    order_by: list[OrderKey]
    frame: Optional[FrameSpec]
    referenced: Optional[str] = None


@dataclass
class WindowExpression:
    output_name: str
    function: str  # SUM / COUNT / COUNT_STAR / FIRST_VALUE / LAST_VALUE
    argument: Optional[str]
    window: WindowDefinition
    raw_sql: str


@dataclass
class Query:
    query_id: str
    sql: str
    table: str
    identity: str
    select_identity: bool = False
    windows: list[WindowExpression] = field(default_factory=list)
    trailing_order: list[OrderKey] = field(default_factory=list)


_SUPPORTED = (exp.Sum, exp.Count, exp.FirstValue, exp.LastValue)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def parse_query(query: dict, schema: dict) -> Query:
    query_id = query["query_id"]
    sql = query["sql"]
    table = schema["table"]
    identity = schema["identity"]
    types = schema.get("types", {})

    try:
        ast = sqlglot.parse_one(sql, read=_GroupsDialect)
    except Exception as exc:
        raise ParseError(f"[{query_id}] cannot parse SQL: {exc}") from exc

    if not isinstance(ast, exp.Select):
        raise ParseError(f"[{query_id}] only a single SELECT statement is supported")
    if ast.args.get("with_"):
        raise ParseError(f"[{query_id}] WITH/CTE is not supported")
    if ast.args.get("distinct"):
        raise ParseError(f"[{query_id}] SELECT DISTINCT is not supported")
    for key in ("where", "group", "having", "qualify", "limit", "offset", "sample"):
        if ast.args.get(key):
            raise ParseError(f"[{query_id}] {key.upper()} is not supported")
    if ast.find(exp.Join):
        raise ParseError(f"[{query_id}] JOIN is not supported")
    if ast.find(exp.Subquery):
        raise ParseError(f"[{query_id}] subqueries are not supported")
    if ast.find(exp.SetOperation):
        raise ParseError(f"[{query_id}] set operations are not supported")

    from_clause = ast.args.get("from_") or ast.args.get("from")
    if not from_clause or from_clause.this is None:
        raise ParseError(f"[{query_id}] SELECT must read exactly one table")
    table_node = from_clause.this
    if not isinstance(table_node, exp.Table):
        raise ParseError(f"[{query_id}] only a plain table in FROM is supported")
    if table_node.args.get("alias"):
        raise ParseError(f"[{query_id}] table aliases are not supported")
    if table_node.db or table_node.catalog:
        raise ParseError(f"[{query_id}] schema-qualified tables are not supported")
    if table_node.name != table:
        raise ParseError(f"[{query_id}] expected table {table!r}, got {table_node.name!r}")

    definitions = _parse_window_clause(ast, query_id)
    _validate_inheritance(definitions, query_id)

    select_identity = False
    windows: list[WindowExpression] = []
    index = 0
    for projection in ast.expressions:
        item = projection.this if isinstance(projection, exp.Alias) else projection
        alias = projection.alias_or_name if isinstance(projection, exp.Alias) else None
        if isinstance(item, exp.Column):
            if item.name != identity:
                raise ParseError(
                    f"[{query_id}] only the identity column {identity!r} may be "
                    "projected as a plain column"
                )
            if select_identity:
                raise ParseError(f"[{query_id}] {identity!r} projected more than once")
            select_identity = True
            continue
        if not isinstance(item, exp.Window):
            raise ParseError(
                f"[{query_id}] unsupported projection: {item.sql(dialect='postgres')}"
            )
        windows.append(
            _parse_window_expression(item, alias, index, definitions, types, query_id)
        )
        index += 1

    if not windows:
        raise ParseError(f"[{query_id}] at least one supported window function is required")
    if not select_identity:
        raise ParseError(
            f"[{query_id}] the identity column {identity!r} must be projected so every "
            "result row can be tied back to its source row"
        )

    trailing = _order_keys(ast.args.get("order"), query_id)

    resolved = Query(
        query_id=query_id,
        sql=sql,
        table=table,
        identity=identity,
        select_identity=select_identity,
        windows=windows,
        trailing_order=trailing,
    )
    _validate_frames(resolved, types, query_id)
    return resolved


# --------------------------------------------------------------------------
# WINDOW clause and inheritance
# --------------------------------------------------------------------------


def _parse_window_clause(ast: exp.Select, query_id: str) -> dict[str, WindowDefinition]:
    definitions: dict[str, WindowDefinition] = {}
    for node in ast.args.get("windows") or []:
        if not isinstance(node, exp.Window):
            raise ParseError(f"[{query_id}] malformed WINDOW definition")
        name = _ident_name(node.this)
        if name in definitions:
            raise ParseError(f"[{query_id}] window {name!r} is already defined")
        ref = node.args.get("alias")
        definitions[name] = WindowDefinition(
            name=name,
            partition_by=[
                _column_name(col, query_id) for col in (node.args.get("partition_by") or [])
            ],
            order_by=_order_keys(node.args.get("order"), query_id),
            frame=_frame_spec(node.args.get("spec")),
            referenced=_ident_name(ref) if ref is not None else None,
        )
    return definitions


def _validate_inheritance(
    definitions: dict[str, WindowDefinition], query_id: str
) -> None:
    """Apply PostgreSQL's SQL:2008 inheritance rules, rejecting violations."""
    resolved: dict[str, WindowDefinition] = {}
    for name, definition in definitions.items():
        ref = definition.referenced
        if ref is None:
            resolved[name] = definition
            continue
        if ref not in resolved:
            raise ParseError(
                f"[{query_id}] window {ref!r} does not exist; a WINDOW definition "
                "may only reference an earlier definition"
            )
        base = resolved[ref]
        if definition.partition_by:
            raise ParseError(
                f"[{query_id}] cannot override PARTITION BY clause of window {ref!r}"
            )
        if definition.order_by and base.order_by:
            raise ParseError(
                f"[{query_id}] cannot override ORDER BY clause of window {ref!r}"
            )
        if base.frame is not None:
            raise ParseError(
                f"[{query_id}] cannot copy window {ref!r} because it has a frame clause"
            )
        resolved[name] = WindowDefinition(
            name=name,
            partition_by=list(base.partition_by),
            order_by=definition.order_by or list(base.order_by),
            frame=definition.frame,
            referenced=ref,
        )
    definitions.update(resolved)


def _resolve_window(
    node: exp.Window,
    definitions: dict[str, WindowDefinition],
    query_id: str,
) -> WindowDefinition:
    ref = node.args.get("alias")
    referenced = _ident_name(ref) if ref is not None else None
    inline_partition = [
        _column_name(col, query_id) for col in (node.args.get("partition_by") or [])
    ]
    inline_order = _order_keys(node.args.get("order"), query_id)
    inline_frame = _frame_spec(node.args.get("spec"))

    if referenced is None:
        return WindowDefinition(
            name=None,
            partition_by=inline_partition,
            order_by=inline_order,
            frame=inline_frame,
        )

    if referenced not in definitions:
        raise ParseError(f"[{query_id}] window {referenced!r} does not exist")
    base = definitions[referenced]

    # A bare ``OVER name`` copies everything, including an existing frame.
    # sqlglot omits partition_by/order/spec keys for that form; ``OVER (name)``
    # includes them (possibly empty) and must obey the copy restrictions.
    bare_reference = (
        "partition_by" not in node.args
        and "order" not in node.args
        and "spec" not in node.args
    )
    if bare_reference:
        return WindowDefinition(
            name=referenced,
            partition_by=list(base.partition_by),
            order_by=list(base.order_by),
            frame=base.frame,
            referenced=referenced,
        )

    if inline_partition:
        raise ParseError(
            f"[{query_id}] cannot override PARTITION BY clause of window {referenced!r}"
        )
    if inline_order and base.order_by:
        raise ParseError(
            f"[{query_id}] cannot override ORDER BY clause of window {referenced!r}"
        )
    if base.frame is not None:
        raise ParseError(
            f"[{query_id}] cannot copy window {referenced!r} because it has a frame clause"
        )
    return WindowDefinition(
        name=referenced,
        partition_by=list(base.partition_by),
        order_by=inline_order or list(base.order_by),
        frame=inline_frame,
        referenced=referenced,
    )


# --------------------------------------------------------------------------
# Window function projections
# --------------------------------------------------------------------------


def _parse_window_expression(
    node: exp.Window,
    alias: Optional[str],
    index: int,
    definitions: dict[str, WindowDefinition],
    types: dict,
    query_id: str,
) -> WindowExpression:
    func = node.this
    if not isinstance(func, _SUPPORTED):
        rendered = (
            func.sql(dialect="postgres")
            if isinstance(func, exp.Expression)
            else repr(func)
        )
        raise ParseError(f"[{query_id}] unsupported window function: {rendered}")

    function = func.sql_name()
    argument: Optional[str] = None
    if isinstance(func, exp.Count):
        if isinstance(func.this, exp.Star):
            function = "COUNT_STAR"
        else:
            argument = _column_name(func.this, query_id)
    else:
        argument = _column_name(func.this, query_id)

    if argument is not None and argument not in types:
        raise ParseError(f"[{query_id}] unknown column {argument!r}")

    window = _resolve_window(node, definitions, query_id)
    for column in window.partition_by:
        if column not in types:
            raise ParseError(f"[{query_id}] unknown partition column {column!r}")
    for key in window.order_by:
        if key.column not in types:
            raise ParseError(f"[{query_id}] unknown order column {key.column!r}")

    return WindowExpression(
        output_name=alias or f"expr{index}",
        function=function,
        argument=argument,
        window=window,
        raw_sql=node.sql(dialect="postgres"),
    )


# --------------------------------------------------------------------------
# Frame parsing and static validation
# --------------------------------------------------------------------------


def _frame_spec(spec: Optional[exp.WindowSpec]) -> Optional[FrameSpec]:
    if spec is None:
        return None
    if spec.args.get("exclude"):
        raise ParseError("EXCLUDE is not supported")
    mode = (spec.args.get("kind") or "RANGE").upper()
    if mode not in ("ROWS", "RANGE", "GROUPS"):
        raise ParseError(f"unsupported frame mode {mode!r}")
    start = _frame_bound(spec.args.get("start"), spec.args.get("start_side"))
    end_raw = spec.args.get("end")
    if end_raw is None:
        # schema.json only admits explicit ``BETWEEN ... AND ...`` frames.
        raise ParseError(
            "frame shorthand (e.g. ROWS 1 PRECEDING) is not supported; write an "
            "explicit BETWEEN ... AND ... frame"
        )
    end = _frame_bound(end_raw, spec.args.get("end_side"))
    return FrameSpec(mode=mode, start=start, end=end)


def _frame_bound(value, side) -> FrameBound:
    if value is None:
        raise ParseError("malformed frame bound")
    if isinstance(value, str):
        text = value.upper()
        if text == "UNBOUNDED":
            return FrameBound(kind="UNBOUNDED", direction=_side(side))
        if text in ("CURRENT ROW", "CURRENT"):
            return FrameBound(kind="CURRENT_ROW")
        raise ParseError(f"unsupported frame bound: {value!r}")
    if isinstance(value, exp.Literal):
        if value.is_string:
            raise ParseError("frame offsets must be integer literals")
        try:
            offset = int(value.this)
        except ValueError as exc:
            raise ParseError(f"frame offset is not an integer: {value.this!r}") from exc
        if offset < 0:
            raise ParseError("frame offsets must be non-negative")
        return FrameBound(kind="OFFSET", offset=offset, direction=_side(side))
    raise ParseError(f"unsupported frame bound expression: {type(value).__name__}")


def _side(side) -> str:
    if isinstance(side, str) and side.upper() in ("PRECEDING", "FOLLOWING"):
        return side.upper()
    raise ParseError(f"frame bound is missing a PRECEDING/FOLLOWING side: {side!r}")


def _validate_frames(query: Query, types: dict, query_id: str) -> None:
    for window in query.windows:
        definition = window.window
        frame = definition.frame
        if frame is None:
            continue
        _validate_bound_order(frame, query_id)
        if frame.mode == "GROUPS" and not definition.order_by:
            raise ParseError(
                f"[{query_id}] GROUPS mode requires an ORDER BY clause"
            )
        offset_used = (
            frame.start.kind == "OFFSET" or frame.end.kind == "OFFSET"
        )
        if frame.mode == "RANGE" and offset_used:
            if len(definition.order_by) != 1:
                raise ParseError(
                    f"[{query_id}] RANGE with offset PRECEDING/FOLLOWING requires "
                    "exactly one ORDER BY column"
                )
            key_type = str(types.get(definition.order_by[0].column, "")).lower()
            # schema types carry modifiers such as "integer nullable".
            base_type = key_type.split()[0] if key_type else ""
            # The engine mirrors PostgreSQL's in_range_int4_int4 (int32) math.
            if base_type not in ("integer", "int", "int4"):
                raise ParseError(
                    f"[{query_id}] RANGE with offset requires an integer (int4) "
                    f"ordering key, got {types.get(definition.order_by[0].column)!r}"
                )


def _validate_bound_order(frame: FrameSpec, query_id: str) -> None:
    start, end = frame.start, frame.end
    if start.kind == "UNBOUNDED" and start.direction == "FOLLOWING":
        raise ParseError(f"[{query_id}] frame start cannot be UNBOUNDED FOLLOWING")
    if end.kind == "UNBOUNDED" and end.direction == "PRECEDING":
        raise ParseError(f"[{query_id}] frame end cannot be UNBOUNDED PRECEDING")
    if start.kind == "CURRENT_ROW" and end.kind == "OFFSET" and end.direction == "PRECEDING":
        raise ParseError(
            f"[{query_id}] frame starting from current row cannot have preceding rows"
        )
    if start.kind == "OFFSET" and start.direction == "FOLLOWING":
        if end.kind == "CURRENT_ROW":
            raise ParseError(
                f"[{query_id}] frame starting from following row cannot have "
                "preceding rows"
            )
        if end.kind == "OFFSET" and end.direction == "PRECEDING":
            raise ParseError(
                f"[{query_id}] frame starting from following row cannot have "
                "preceding rows"
            )


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _order_keys(order: Optional[exp.Order], query_id: str) -> list[OrderKey]:
    if order is None:
        return []
    keys: list[OrderKey] = []
    for ordered in order.expressions:
        if not isinstance(ordered, exp.Ordered):
            raise ParseError(f"[{query_id}] malformed ORDER BY element")
        column = _column_name(ordered.this, query_id)
        descending = bool(ordered.args.get("desc"))
        # sqlglot resolves the dialect default: ASC -> NULLS LAST,
        # DESC -> NULLS FIRST.
        nulls_first = bool(ordered.args.get("nulls_first"))
        keys.append(OrderKey(column, descending, nulls_first))
    return keys


def _ident_name(node: exp.Expression) -> str:
    if isinstance(node, exp.Identifier):
        return node.this
    if isinstance(node, exp.Column):
        return node.name
    raise ParseError(f"unsupported identifier node: {type(node).__name__}")


def _column_name(node, query_id: str) -> str:
    if not isinstance(node, exp.Column):
        rendered = (
            node.sql(dialect="postgres")
            if isinstance(node, exp.Expression)
            else repr(node)
        )
        raise ParseError(f"[{query_id}] expected a plain column, got {rendered}")
    if node.table:
        raise ParseError(f"[{query_id}] qualified column names are not supported")
    return node.name
