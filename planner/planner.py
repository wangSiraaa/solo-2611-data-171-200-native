"""Query binding, reachability checks, per-fact plan compilation and execution.

The central invariant: **no single SQL statement ever joins two facts**.  Each
fact is aggregated independently at its own declared grain, restricted by its
own local filters and by the shared entity-set semijoin.  The per-fact results
are then stitched together on the union of their group keys.  That is why
order amounts, line amounts and payments can never multiply each other.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from .errors import Unplannable, ValidationError
from .model import Model, quote_ident as q

OPS = ("eq", "in", "gte")


# --------------------------------------------------------------------------- #
# Query binding
# --------------------------------------------------------------------------- #
@dataclass
class Filter:
    scope: str              # "local" | "entity_exists"
    fact: str
    dimension: str
    op: str
    value: Any
    raw_index: int

    def describe(self) -> str:
        return (f"{self.scope}({self.fact}.{self.dimension} {self.op} "
                f"{self.value!r})")


@dataclass
class FactTask:
    fact: str
    metrics: list[str]                     # base metrics sourced from this fact
    group_dims: list[str]
    local_filters: list[Filter]
    dim_paths: dict[str, Any]              # dim -> DimensionPath used


@dataclass
class BoundQuery:
    dims: list[str]
    requested_metrics: list[str]
    leaf_metrics: list[str]
    ratio_metrics: list[str]
    filters: list[Filter]
    local_filters: list[Filter]
    entity_filters: list[Filter]
    tasks: list[FactTask]


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise ValidationError(message)


def _validate_value(op: str, value: Any, where: str) -> None:
    def scalar(v: Any) -> bool:
        return isinstance(v, (str, int, float)) and not isinstance(v, bool)

    if op == "in":
        _require(isinstance(value, list) and len(value) > 0,
                 f"{where}: op 'in' needs a non-empty list value")
        for v in value:
            _require(scalar(v), f"{where}: list has a non-scalar value {v!r}")
    elif op in ("eq", "gte"):
        _require(scalar(value),
                 f"{where}: op {op!r} needs a scalar (str/int/float) value, "
                 f"got {value!r}")


def bind_query(model: Model, query: dict) -> BoundQuery:
    if not isinstance(query, dict):
        raise ValidationError("query must be a JSON object")

    raw_dims = query.get("dimensions", [])
    raw_metrics = query.get("metrics", [])
    raw_filters = query.get("filters", [])

    _require(isinstance(raw_dims, list), "query.dimensions must be a list")
    _require(isinstance(raw_metrics, list), "query.metrics must be a list")
    _require(isinstance(raw_filters, list), "query.filters must be a list")
    _require(len(raw_metrics) > 0, "query must request at least one metric")

    dims: list[str] = []
    for name in raw_dims:
        _require(isinstance(name, str), "query.dimensions entries must be strings")
        _require(name in model.dimensions, f"unknown dimension {name!r}")
        if name not in dims:
            dims.append(name)

    metrics: list[str] = []
    for name in raw_metrics:
        _require(isinstance(name, str), "query.metrics entries must be strings")
        _require(name in model.metrics, f"unknown metric {name!r}")
        if name not in metrics:
            metrics.append(name)

    ratio_metrics = [m for m in metrics if model.metrics[m].kind == "ratio"]
    leaf_metrics = model.leaf_metrics(metrics)

    filters: list[Filter] = []
    for i, rf in enumerate(raw_filters):
        _require(isinstance(rf, dict), f"filter #{i} must be an object")
        scope = rf.get("scope")
        fact = rf.get("fact")
        dim = rf.get("dimension")
        op = rf.get("op")
        where = f"filter #{i}"
        _require(scope in ("local", "entity_exists"),
                 f"{where}: scope must be 'local' or 'entity_exists', got {scope!r}")
        _require(isinstance(fact, str) and fact in model.facts,
                 f"{where}: unknown fact {fact!r}")
        _require(isinstance(dim, str) and dim in model.dimensions,
                 f"{where}: unknown dimension {dim!r}")
        _require(op in OPS, f"{where}: unsupported op {op!r} (allowed: {list(OPS)})")
        _require("value" in rf, f"{where}: missing value")
        _validate_value(op, rf["value"], where)
        if model.path(fact, dim) is None:
            raise ValidationError(
                f"{where}: fact {fact!r} has no declared many_to_one path to "
                f"dimension {dim!r}; the filter cannot be evaluated on that fact"
            )
        filters.append(Filter(scope=scope, fact=fact, dimension=dim,
                              op=op, value=rf["value"], raw_index=i))

    local_filters = [f for f in filters if f.scope == "local"]
    entity_filters = [f for f in filters if f.scope == "entity_exists"]

    # ---- reachability of every group dimension from every metric's fact ----
    missing: list[dict] = []
    for metric in leaf_metrics:
        fact = model.metric_fact(metric)
        assert fact is not None
        for dim in dims:
            if model.path(fact, dim) is None:
                declared = [
                    {"dimension": p.dimension, "path": p.describe()}
                    for p in model.paths_from(fact)
                ]
                reaching = [
                    {"fact": p.fact, "path": p.describe()}
                    for p in model.paths_to(dim)
                ]
                reason = (
                    f"metric {metric!r} is sourced from fact {fact!r}; dimension "
                    f"{dim!r} is declared on table "
                    f"{model.dimensions[dim].table!r}, which is not reachable from "
                    f"{fact!r} by declared many_to_one relations. There is no "
                    f"declared allocation rule from a {fact}-grain measure to a "
                    f"{dim}-grain group, and joining back down a one-to-many edge "
                    f"is not a dimension path."
                )
                missing.append({
                    "metric": metric,
                    "fact": fact,
                    "dimension": dim,
                    "reason": reason,
                    "declared_paths_from_fact": declared,
                    "facts_that_can_reach_dimension": reaching,
                })

    if missing:
        names = ", ".join(f"{m['metric']}->{m['dimension']}" for m in missing)
        raise Unplannable(
            "request is not plannable: cannot group these facts by the requested "
            f"dimension(s) without inventing an allocation rule ({names}). "
            "Refusing to split a coarser-grain measure silently (e.g. average "
            "payment amounts across categories).",
            paths=missing,
        )

    tasks = []
    for fact_name in model.facts:
        fact_metrics = [m for m in leaf_metrics if model.metric_fact(m) == fact_name]
        if not fact_metrics:
            continue
        # dimensions this task performs: all group dims (validated reachable).
        tasks.append(FactTask(
            fact=fact_name,
            metrics=fact_metrics,
            group_dims=list(dims),
            local_filters=[f for f in local_filters if f.fact == fact_name],
            dim_paths={d: model.path(fact_name, d) for d in dims},
        ))

    return BoundQuery(
        dims=dims, requested_metrics=metrics, leaf_metrics=leaf_metrics,
        ratio_metrics=ratio_metrics, filters=filters,
        local_filters=local_filters, entity_filters=entity_filters,
        tasks=tasks,
    )


# --------------------------------------------------------------------------- #
# SQL construction
# --------------------------------------------------------------------------- #
def predicate_sql(dim_expr: str, flt: Filter) -> tuple[str, list[Any]]:
    if flt.op == "eq":
        return f"{dim_expr} = ?", [flt.value]
    if flt.op == "gte":
        return f"{dim_expr} >= ?", [flt.value]
    if flt.op == "in":
        placeholders = ", ".join("?" for _ in flt.value)
        return f"{dim_expr} IN ({placeholders})", list(flt.value)
    raise AssertionError(f"unexpected op {flt.op!r}")


def _dimension_join(
    model: Model,
    fact: str,
    dim: str,
    start_alias: str,
    alias_counter: list[int],
    join_parts: list[str],
    alias_cache: dict[tuple, str] | None = None,
) -> str:
    """Append any new join clauses to ``join_parts`` and return the dim expr.

    ``alias_cache`` deduplicates identical joins (same source alias, same
    destination table, same key pair) so a plan does not join the same
    many_to_one edge twice when two dimensions share a path prefix.
    """
    path = model.path(fact, dim)
    if path is None:
        raise AssertionError(f"no path {fact}->{dim}")
    prev = start_alias
    cache = alias_cache if alias_cache is not None else {}
    alias_prefix = "sj" if start_alias.startswith("s") else "j"
    for hop in path.hops:
        cache_key = (prev, hop.dst, hop.src_key, hop.dst_key)
        if cache_key in cache:
            prev = cache[cache_key]
            continue
        alias_counter[0] += 1
        alias = f"{alias_prefix}{alias_counter[0]}"
        cache[cache_key] = alias
        join_parts.append(
            f"JOIN {q(hop.dst)} AS {alias} "
            f"ON {prev}.{q(hop.src_key)} = {alias}.{q(hop.dst_key)}"
        )
        prev = alias
    return f"{prev}.{q(model.dimensions[dim].column)}"


def build_entity_semijoin(model: Model, filters: list[Filter]) -> tuple[str, list[Any]]:
    """SQL selecting the order ids that satisfy *all* entity_exists filters."""
    if not filters:
        return "", []
    selects: list[str] = []
    params: list[Any] = []
    for flt in filters:
        fact = model.facts[flt.fact]
        alias_counter = [0]
        join_parts: list[str] = []
        dim_expr = _dimension_join(model, flt.fact, flt.dimension,
                                   "s0", alias_counter, join_parts)
        joins = "\n  ".join(join_parts)
        pred, pred_params = predicate_sql(dim_expr, flt)
        sql = (f"SELECT DISTINCT s0.{q(fact.entity_key)} "
               f"FROM {q(flt.fact)} AS s0\n  {joins}\n"
               f"WHERE {pred}")
        selects.append(sql)
        params.extend(pred_params)
    if len(selects) == 1:
        return selects[0], params
    # SQLite does not accept parenthesised SELECTs here; INTERSECT has
    # higher precedence than the surrounding comparison context, so the plain
    # compound form is safe when wrapped by the caller as `key IN (...)`.
    return "\nINTERSECT\n".join(selects), params


def compile_task(model: Model, task: FactTask, semijoin_sql: str,
                 semijoin_params: list[Any]) -> dict:
    fact = model.facts[task.fact]
    alias_counter = [0]
    alias_cache: dict[tuple, str] = {}
    joins: list[str] = []
    dim_exprs: dict[str, str] = {}

    needed_dims = list(task.group_dims)
    for flt in task.local_filters:
        if flt.dimension not in needed_dims:
            needed_dims.append(flt.dimension)
    for dim in needed_dims:
        expr = _dimension_join(model, task.fact, dim, "t0",
                               alias_counter, joins, alias_cache)
        dim_exprs[dim] = expr

    group_exprs = [dim_exprs[d] for d in task.group_dims]

    agg_items: list[dict] = []
    params: list[Any] = []
    for idx, metric_name in enumerate(task.metrics):
        metric = model.metrics[metric_name]
        out_alias = f"m{idx}"
        if metric.kind == "sum":
            expr = f"SUM(t0.{q(metric.column)})"
            op_desc = f"SUM({task.fact}.{metric.column})"
        else:
            expr = f"COUNT(t0.{q(metric.column)})" if metric.column else "COUNT(*)"
            op_desc = expr.replace("t0.", task.fact + ".")
        agg_items.append({
            "metric": metric_name, "alias": out_alias,
            "kind": metric.kind, "expression": expr, "description": op_desc,
        })

    local_preds: list[str] = []
    for flt in task.local_filters:
        pred, vals = predicate_sql(dim_exprs[flt.dimension], flt)
        local_preds.append(pred)
        params.extend(vals)

    where_bits: list[str] = list(local_preds)
    if semijoin_sql:
        where_bits.append(
            f"t0.{q(fact.entity_key)} IN (\n{semijoin_sql}\n)"
        )

    aggregate_exprs = ", ".join(
        f"{item['expression']} AS {item['alias']}" for item in agg_items
    )
    select_bits = []
    if group_exprs:
        select_bits.append(", ".join(group_exprs))
    select_bits.append(aggregate_exprs)
    select_bits.append("COUNT(*) AS n_rows")

    sql = f"SELECT {', '.join(select_bits)}\nFROM {q(task.fact)} AS t0"
    if joins:
        sql += "\n  " + "\n  ".join(joins)
    if where_bits:
        sql += "\nWHERE " + "\n  AND ".join(where_bits)
    if group_exprs:
        sql += "\nGROUP BY " + ", ".join(group_exprs)

    return {
        "fact": task.fact,
        "groups": task.group_dims,
        "group_exprs": group_exprs,
        "metrics": agg_items,
        "joins": joins,
        "dim_exprs": dim_exprs,
        "local_filters": task.local_filters,
        "where": where_bits,
        "sql": sql,
        "params": params + semijoin_params,
        "semijoin_used": bool(semijoin_sql),
    }


def display_sql(sql: str, params: list[Any]) -> str:
    """Inline bound parameters *for human display only* (never executed)."""
    pieces = sql.split("?")
    if len(pieces) - 1 != len(params):
        return sql
    out = [pieces[0]]
    for value, tail in zip(params, pieces[1:]):
        if isinstance(value, str):
            rendered = "'" + value.replace("'", "''") + "'"
        else:
            rendered = repr(value)
        out.append(rendered)
        out.append(tail)
    return "".join(out)


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #
@dataclass
class PlanResult:
    plan: dict
    rows: list[dict]
    columns: list[str]
    ratio_fields: dict[str, dict]
    fact_row_columns: dict[str, str]
    bound: BoundQuery


def _sort_key(key: tuple) -> tuple:
    parts = []
    for v in key:
        if v is None:
            parts.append((2, ""))
        elif isinstance(v, bool):
            parts.append((0, int(v)))
        elif isinstance(v, (int, float)):
            parts.append((0, v))
        else:
            parts.append((1, str(v)))
    return tuple(parts)


def compile_plan(connection: sqlite3.Connection, model: Model,
                 bound: BoundQuery) -> dict:
    """Compile every fact task into SQL and assemble the plan (no aggregation)."""
    semijoin_sql, semijoin_params = build_entity_semijoin(model, bound.entity_filters)
    semijoin_ids: list[Any] | None = None
    if semijoin_sql:
        semijoin_ids = [r[0] for r in connection.execute(
            semijoin_sql + " ORDER BY 1", semijoin_params).fetchall()]
    compiled = [compile_task(model, task, semijoin_sql, semijoin_params)
                for task in bound.tasks]
    ratio_fields = _ratio_fields(model, bound)
    fact_row_columns = {task.fact: f"__rows_{task.fact}" for task in bound.tasks}
    return _build_plan(model, bound, compiled, semijoin_sql, semijoin_params,
                       semijoin_ids, ratio_fields, fact_row_columns)


def _ratio_fields(model: Model, bound: BoundQuery) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for rname in bound.ratio_metrics:
        metric = model.metrics[rname]
        out[rname] = {
            "numerator": metric.numerator,
            "denominator": metric.denominator,
            "numerator_column": f"{rname}__numerator",
            "denominator_column": f"{rname}__denominator",
        }
    return out


def execute(connection: sqlite3.Connection, model: Model,
            bound: BoundQuery) -> PlanResult:
    semijoin_sql, semijoin_params = build_entity_semijoin(model, bound.entity_filters)

    semijoin_ids: list[Any] | None = None
    if semijoin_sql:
        semijoin_ids = [r[0] for r in connection.execute(
            semijoin_sql + " ORDER BY 1", semijoin_params).fetchall()]

    task_rows: dict[str, dict[tuple, dict]] = {}
    compiled: list[dict] = []

    for task in bound.tasks:
        plan = compile_task(model, task, semijoin_sql, semijoin_params)
        compiled.append(plan)
        groups: dict[tuple, dict] = {}
        cursor = connection.execute(plan["sql"], plan["params"])
        for row in cursor.fetchall():
            n_rows = row["n_rows"]
            if n_rows == 0:
                # An ungrouped aggregate over zero rows must not invent a group.
                continue
            key = tuple(row[i] for i in range(len(task.group_dims)))
            values = {}
            for item in plan["metrics"]:
                raw = row[item["alias"]]
                values[item["metric"]] = (model.semantics.empty_measure
                                          if raw is None else raw)
            groups[key] = {"values": values, "n_rows": n_rows}
        task_rows[task.fact] = groups

    keys = sorted({k for groups in task_rows.values() for k in groups},
                  key=_sort_key)

    ratio_fields = _ratio_fields(model, bound)

    fact_row_columns = {
        fact: f"__rows_{fact}" for fact in task_rows
    }

    fact_row_columns = {
        fact: f"__rows_{fact}" for fact in task_rows
    }

    rows_out: list[dict] = []
    for key in keys:
        metric_values: dict[str, Any] = {}
        for m in bound.leaf_metrics:
            fact = model.metric_fact(m)
            cell = task_rows[fact].get(key)
            metric_values[m] = (cell["values"][m] if cell else
                                model.semantics.empty_measure)
        # evaluated metrics in requested order (ratios recomputed on this group)
        evaluated: dict[str, Any] = {}
        for m in bound.requested_metrics:
            metric = model.metrics[m]
            if metric.kind == "ratio":
                num = metric_values[metric.numerator]
                den = metric_values[metric.denominator]
                evaluated[m] = (None if den == 0
                                else num / den)
            else:
                evaluated[m] = metric_values[m]

        group = {d: key[i] for i, d in enumerate(bound.dims)}
        fact_rows = {
            fact: (task_rows[fact][key]["n_rows"] if key in task_rows[fact] else 0)
            for fact in task_rows
        }
        ratios = {}
        for rname, info in ratio_fields.items():
            ratios[rname] = {
                info["numerator"]: metric_values[info["numerator"]],
                info["denominator"]: metric_values[info["denominator"]],
            }
        rows_out.append({
            "group": group,
            "metrics": evaluated,
            "base": {m: metric_values[m] for m in bound.leaf_metrics},
            "ratios": ratios,
            "fact_rows": fact_rows,
        })

    columns = list(bound.dims) + list(bound.requested_metrics)
    for info in ratio_fields.values():
        columns.append(info["numerator_column"])
        columns.append(info["denominator_column"])
    for fact in task_rows:
        columns.append(fact_row_columns[fact])

    plan = _build_plan(model, bound, compiled, semijoin_sql, semijoin_params,
                       semijoin_ids, ratio_fields, fact_row_columns)
    return PlanResult(plan=plan, rows=rows_out, columns=columns,
                      ratio_fields=ratio_fields,
                      fact_row_columns=fact_row_columns, bound=bound)


# --------------------------------------------------------------------------- #
# Plan description
# --------------------------------------------------------------------------- #
def _filters_descr(filters: list[Filter]) -> list[dict]:
    return [
        {"scope": f.scope, "fact": f.fact, "dimension": f.dimension,
         "op": f.op, "value": f.value}
        for f in filters
    ]


def _build_plan(model: Model, bound: BoundQuery, compiled: list[dict],
                semijoin_sql: str, semijoin_params: list[Any],
                semijoin_ids: list[Any] | None,
                ratio_fields: dict, fact_row_columns: dict) -> dict:
    tasks = []
    for plan in compiled:
        fact_decl = model.facts[plan["fact"]]
        tasks.append({
            "fact": plan["fact"],
            "grain": {
                "row_identity": list(fact_decl.key),
                "entity_key": fact_decl.entity_key,
                "how": (f"each row of {plan['fact']} is used exactly once; "
                        f"aggregation happens per {', '.join(plan['groups']) or 'the whole fact set'}"
                        f" only after grouping"),
            },
            "metrics": [
                {"name": item["metric"], "kind": item["kind"],
                 "sql_expression": item["description"]}
                for item in plan["metrics"]
            ],
            "group_dimensions": [
                {"dimension": d,
                 "path": model.path(plan["fact"], d).describe(),
                 "sql_expression": plan["dim_exprs"][d]}
                for d in plan["groups"]
            ],
            "local_filters": _filters_descr(plan["local_filters"]),
            "entity_exists_restriction": {
                "applied": plan["semijoin_used"],
                "detail": ("entity_key IN (order-id semijoin)" if plan["semijoin_used"]
                           else "none"),
            },
            "join_clauses": plan["joins"],
            "sql": display_sql(plan["sql"], plan["params"]),
            "notes": _task_notes(model, plan),
        })

    return {
        "dimensions": bound.dims,
        "requested_metrics": bound.requested_metrics,
        "leaf_metrics": bound.leaf_metrics,
        "ratio_metrics": ratio_fields,
        "filters": _filters_descr(bound.filters),
        "inert_local_filters": [
            {"filter": f.describe(),
             "reason": (f"no requested base metric is sourced from fact "
                        f"{f.fact!r}, so this local filter has no participating "
                        f"rows to restrict and does not change any output")}
            for f in bound.local_filters
            if f.fact not in {t["fact"] for t in compiled}
        ],        "filter_scopes": {
            "local": _filters_descr(bound.local_filters),
            "entity_exists": _filters_descr(bound.entity_filters),
            "entity_exists_sql": display_sql(semijoin_sql, semijoin_params)
            if semijoin_sql else None,
            "entity_exists_orders": semijoin_ids,
            "entity_exists_order_count": (len(semijoin_ids)
                                          if semijoin_ids is not None else None),
        },
        "tasks": tasks,
        "combination": {
            "group_key": bound.dims,
            "group_domain": model.semantics.group_domain,
            "merge": ("per-fact results are merged on the group key; each fact "
                      "is aggregated in its own SQL statement, so no fact row "
                      "is ever counted through another fact"),
            "empty_measure": model.semantics.empty_measure,
            "zero_denominator": model.semantics.zero_denominator,
            "absent_all_metrics": model.semantics.absent_all_metrics,
            "ratios": [
                {"metric": r, "numerator": info["numerator"],
                 "denominator": info["denominator"],
                 "rule": "recomputed per output group: numerator/denominator; "
                         "denominator == 0 yields the model's zero_denominator"}
                for r, info in ratio_fields.items()
            ],
        },
        "support_columns": {
            "ratio_numerators_denominators": [
                {"metric": r, "numerator_column": info["numerator_column"],
                 "denominator_column": info["denominator_column"]}
                for r, info in ratio_fields.items()
            ],
            "fact_row_counts": fact_row_columns,
        },
        "why_no_double_count": [
            "Each fact is read by its own single-fact aggregation statement; "
            "orders/lines/payments are never in the same FROM clause.",
            "Every dimension join follows only declared many_to_one edges, so "
            "joining a dimension cannot multiply fact rows.",
            "entity_exists is computed once as an order-id set (intersected "
            "across entity filters) and applied as a key restriction; it does "
            "not copy filter predicates onto unrelated facts.",
            "local filters appear only in the WHERE clause of their own fact's "
            "aggregation.",
            "The final combination is a keyed union: a group missing from a "
            "fact receives the model's empty_measure (0), never a duplicate row.",
        ],
    }


def _task_notes(model: Model, plan: dict) -> list[str]:
    notes = []
    count_metrics = [i["metric"] for i in plan["metrics"] if i["kind"] == "count"]
    sum_metrics = [i["metric"] for i in plan["metrics"] if i["kind"] == "sum"]
    if count_metrics:
        notes.append(f"counting metrics {count_metrics} use COUNT over this fact's "
                     f"rows; each fact row contributes exactly 1")
    if sum_metrics:
        notes.append(f"summing metrics {sum_metrics} sum a column of this fact; "
                     f"adding rows of another fact cannot change them")
    if plan["local_filters"]:
        notes.append("local filters restrict only this fact's participating rows; "
                     "they are not propagated to other facts")
    if plan["semijoin_used"]:
        notes.append("the shared entity-key semijoin restricts this fact to the "
                     "same order set as every other metric")
    return notes


def explain_text(model: Model, bound: BoundQuery, plan: dict) -> str:
    lines: list[str] = []
    lines.append("=== 指标查询执行计划 / Metric query plan ===")
    lines.append(f"维度 dimensions: {bound.dims or '(无/none)'}")
    lines.append(f"请求指标 requested metrics: {bound.requested_metrics}")
    lines.append(f"基础指标 leaf metrics (planning grain): {bound.leaf_metrics}")
    lines.append("")

    lines.append("[过滤 Filters]")
    if not bound.filters:
        lines.append("  (none)")
    for f in bound.filters:
        lines.append(f"  - {f.scope}: fact={f.fact} {f.dimension} {f.op} {f.value!r}")
    if bound.entity_filters:
        info = plan["filter_scopes"]
        lines.append(f"  entity_exists 订单集合 (intersection across entity filters): "
                     f"{info['entity_exists_order_count']} order(s) "
                     f"{info['entity_exists_orders']}")
        lines.append(f"  semijoin SQL: {info['entity_exists_sql']}")
        lines.append("  说明: 该集合作为 entity_key 限制应用到所有指标; "
                     "local 过滤不会传播到它。")
    for inert in plan.get("inert_local_filters", []):
        lines.append(f"  [无效果 inert local filter] {inert['filter']}: "
                     f"{inert['reason']}")
    lines.append("")

    for t in plan["tasks"]:
        lines.append(f"[事实 Task: {t['fact']}]")
        g = t["grain"]
        lines.append(f"  行身份 row identity: {g['row_identity']}  "
                     f"entity_key: {g['entity_key']}")
        lines.append(f"  聚集粒度/aggregation: {g['how']}")
        lines.append("  指标 metrics:")
        for m in t["metrics"]:
            lines.append(f"    - {m['name']} ({m['kind']}) = {m['sql_expression']}")
        if t["group_dimensions"]:
            lines.append("  维度路径 dimension paths:")
            for d in t["group_dimensions"]:
                lines.append(f"    - {d['dimension']}: {d['path']} -> "
                             f"{d['sql_expression']}")
        lines.append(f"  局部过滤 local filters: {t['local_filters'] or '(none)'}")
        lines.append(f"  实体限制 entity restriction: "
                     f"{t['entity_exists_restriction']['detail']}")
        if t["join_clauses"]:
            lines.append("  joins:")
            for j in t["join_clauses"]:
                lines.append(f"    {j.replace(chr(10), ' ')}")
        lines.append("  SQL:")
        for sql_line in t["sql"].splitlines():
            lines.append(f"    {sql_line}")
        for note in t["notes"]:
            lines.append(f"  note: {note}")
        lines.append("")

    c = plan["combination"]
    lines.append("[组合 Combination]")
    lines.append(f"  分组键 group key: {c['group_key'] or '(none)'}")
    lines.append(f"  分组域: {c['group_domain']}")
    lines.append(f"  合并: {c['merge']}")
    lines.append(f"  empty_measure={c['empty_measure']}  "
                 f"zero_denominator={c['zero_denominator']}  "
                 f"absent_all_metrics={c['absent_all_metrics']}")
    for r in c["ratios"]:
        lines.append(f"  比率 {r['metric']} = {r['numerator']} / {r['denominator']}"
                     f"  ({r['rule']})")
    lines.append("")
    lines.append("[为什么不会重复计量 Why no double counting]")
    for i, reason in enumerate(plan["why_no_double_count"], 1):
        lines.append(f"  {i}. {reason}")
    return "\n".join(lines)
