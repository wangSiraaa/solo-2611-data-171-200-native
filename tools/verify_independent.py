"""Independent per-order verifier.

This tool deliberately does NOT reuse the planner's SQL or stitching.  It reads
the raw fact rows and, one order at a time, accumulates each base metric by
hand:

* entity_exists filters -> the order passes only if *every* entity filter has a
  matching fact row for that order (intersection of order ids);
* local filters -> only rows of the filter's own fact are discarded;
* every base metric is summed/counted over rows of its own fact only, so no
  fact can inflate another.

If the hand accumulation disagrees with the planner output, the pair is
reported.  This is the audit path referenced by the README.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

from planner.api import Planner
from planner.loader import open_database
from planner.model import Model, quote_ident


def _rows(connection, table: str) -> list[dict]:
    return [dict(r) for r in connection.execute(f"SELECT * FROM {quote_ident(table)}")]


def _load(connection, model: Model) -> dict[str, list[dict]]:
    tables: set[str] = set(model.facts) | {d.table for d in model.dimensions.values()}
    tables |= {r.dst for r in model.relations}
    return {t: _rows(connection, t) for t in tables}


def _indexes(model: Model, tables: dict[str, list[dict]]) -> dict[tuple[str, str], dict]:
    """Map (table, key) -> {key_value: row}; cardinality is verified separately."""
    idx: dict[tuple[str, str], dict] = {}
    for rel in model.relations:
        if (rel.dst, rel.dst_key) in idx:
            continue
        mapping = {}
        for row in tables[rel.dst]:
            mapping.setdefault(row[rel.dst_key], row)
        idx[(rel.dst, rel.dst_key)] = mapping
    return idx


def _resolve(model: Model, row: dict, fact: str, dim: str,
             idx: dict[tuple[str, str], dict]) -> object:
    path = model.path(fact, dim)
    if path is None:
        return None
    cur = row
    for hop in path.hops:
        nxt = idx[(hop.dst, hop.dst_key)].get(cur[hop.src_key])
        if nxt is None:
            return None
        cur = nxt
    return cur[model.dimensions[dim].column]


def _pred(value, op: str, target) -> bool:
    if value is None:
        return False
    if op == "eq":
        return value == target
    if op == "in":
        return value in target
    if op == "gte":
        return value >= target
    raise ValueError(op)


def independent_result(connection, model: Model, query: dict) -> tuple[dict, list[str]]:
    """Return ({group_key: {metric: value}}, group_order) using per-order sums."""
    tables = _load(connection, model)
    idx = _indexes(model, tables)

    dims = list(dict.fromkeys(query.get("dimensions", [])))
    metrics = list(dict.fromkeys(query.get("metrics", [])))
    filters = query.get("filters", [])
    entity_filters = [f for f in filters if f["scope"] == "entity_exists"]
    local_by_fact: dict[str, list[dict]] = defaultdict(list)
    for f in filters:
        if f["scope"] == "local":
            local_by_fact[f["fact"]].append(f)

    leaf = model.leaf_metrics(metrics)

    # group fact rows by entity key (one order's contribution, accumulated alone)
    ent = model.entity
    fact_rows: dict[str, dict[str, list[dict]]] = {}
    for fact in model.facts.values():
        by_order: dict[str, list[dict]] = defaultdict(list)
        for row in tables[fact.name]:
            by_order[row[fact.entity_key]].append(row)
        fact_rows[fact.name] = by_order

    acc: dict[tuple, dict] = {}
    for order in tables[ent.name]:
        oid = order[ent.entity_key]

        # --- entity_exists: every entity filter must find a matching row ---
        passes = True
        for ef in entity_filters:
            matched = any(
                _pred(_resolve(model, r, ef["fact"], ef["dimension"], idx),
                      ef["op"], ef["value"])
                for r in fact_rows[ef["fact"]].get(oid, [])
            )
            if not matched:
                passes = False
                break
        if entity_filters and not passes:
            continue

        # --- per fact, per row accumulation (no cross-fact multiplication) ---
        for fact_name, fact in model.facts.items():
            fact_metrics = [m for m in leaf if model.metric_fact(m) == fact_name]
            if not fact_metrics:
                continue
            for row in fact_rows[fact_name].get(oid, []):
                if any(
                    not _pred(_resolve(model, row, lf["fact"], lf["dimension"], idx),
                              lf["op"], lf["value"])
                    for lf in local_by_fact.get(fact_name, [])
                ):
                    continue
                key_values = []
                missing = False
                for d in dims:
                    v = _resolve(model, row, fact_name, d, idx)
                    if v is None:
                        missing = True
                        break
                    key_values.append(v)
                if missing:
                    continue
                key = tuple(key_values)
                cell = acc.setdefault(key, {"base": {}, "rows": {}})
                for m in fact_metrics:
                    metric = model.metrics[m]
                    if metric.kind == "sum":
                        cell["base"][m] = cell["base"].get(m, 0) + row[metric.column]
                    else:  # count
                        cell["base"][m] = cell["base"].get(m, 0) + 1
                cell["rows"][fact_name] = cell["rows"].get(fact_name, 0) + 1

    # evaluated metrics (ratios recomputed on this group)
    groups: dict[tuple, dict] = {}
    for key, cell in acc.items():
        values = {}
        for m in metrics:
            metric = model.metrics[m]
            if metric.kind == "ratio":
                num = cell["base"].get(metric.numerator, 0)
                den = cell["base"].get(metric.denominator, 0)
                values[m] = None if den == 0 else num / den
            else:
                values[m] = cell["base"].get(m, 0)
        groups[key] = {"metrics": values, "base": cell["base"], "rows": cell["rows"]}

    def sort_key(k):
        return tuple((str(v) if not isinstance(v, (int, float)) else v) for v in k)

    ordered = sorted(groups, key=sort_key)
    return groups, ordered


def compare(connection, model: Model, query: dict) -> list[str]:
    expected, order = independent_result(connection, model, query)
    result = Planner(model).run(connection, query)
    problems: list[str] = []

    actual_keys = [tuple(row["group"][d] for d in query.get("dimensions", []))
                   for row in result.rows]
    n_dims = len(query.get("dimensions", []))
    # rebuild actual keyed by the query's dimension order
    actual = {}
    for row in result.rows:
        key = tuple(row["group"][d] for d in query.get("dimensions", []))
        actual[key] = row

    if set(expected) != set(actual):
        only_exp = sorted(set(expected) - set(actual), key=str)
        only_act = sorted(set(actual) - set(expected), key=str)
        if only_exp:
            problems.append(f"groups missing from planner output: {only_exp}")
        if only_act:
            problems.append(f"extra groups in planner output: {only_act}")

    for key in sorted(set(expected) & set(actual), key=str):
        row = actual[key]
        got_m = row["metrics"]
        exp_m = expected[key]["metrics"]
        for m in query.get("metrics", []):
            g, e = got_m.get(m), exp_m.get(m)
            if isinstance(g, float) or isinstance(e, float):
                if g is None and e is None:
                    continue
                if g is None or e is None or abs(g - e) > 1e-12:
                    problems.append(f"group {key} metric {m}: planner={g!r} "
                                    f"independent={e!r}")
            elif g != e:
                problems.append(f"group {key} metric {m}: planner={g!r} "
                                f"independent={e!r}")
        for m in result.bound.leaf_metrics:
            g = row["base"][m]
            e = expected[key]["base"].get(m, model.semantics.empty_measure)
            if g != e:
                problems.append(f"group {key} base {m}: planner={g!r} "
                                f"independent={e!r}")
        for fact in row["fact_rows"]:
            g = row["fact_rows"][fact]
            e = expected[key]["rows"].get(fact, 0)
            if g != e:
                problems.append(f"group {key} rows[{fact}]: planner={g} "
                                f"independent={e}")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="per-order independent verifier")
    ap.add_argument("--db", default=".runtime/task.db")
    ap.add_argument("--model", default="fixtures/model.json")
    ap.add_argument("--query", required=True)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    model = Model.load(args.model)
    query = json.loads(Path(args.query).read_text(encoding="utf-8"))
    conn = open_database(args.db)
    try:
        problems = compare(conn, model, query)
    finally:
        conn.close()
    if args.json:
        print(json.dumps({"ok": not problems, "problems": problems},
                         ensure_ascii=False, indent=2))
    else:
        if problems:
            print(f"FAIL: {len(problems)} mismatch(es)")
            for p in problems:
                print("  -", p)
        else:
            print("OK: planner output matches independent per-order accumulation")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
