"""Command-line interface: build / verify / run / explain / inspect.

Examples
--------
python -m planner build   --sql fixtures/schema_and_data.sql --db .runtime/task.db
python -m planner verify  --db .runtime/task.db --model fixtures/model.json
python -m planner run     --db .runtime/task.db --model fixtures/model.json \
                          --query fixtures/queries/region.json --out results/region
python -m planner explain --db .runtime/task.db --model fixtures/model.json \
                          --query fixtures/queries/combined.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .errors import CardinalityError, PlannerError, Unplannable
from .loader import build_database, open_database
from .model import Model
from .output import as_json_payload, flatten_rows, write_csv, write_json
from .api import Planner
from .planner import explain_text


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def load_model(args) -> Model:
    return Model.load(args.model)


def open_conn(args, need_db: bool = True):
    if need_db:
        return open_database(args.db)
    return None


def read_query(path: str):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PlannerError(f"query file not found: {path}")
    except json.JSONDecodeError as exc:
        raise PlannerError(f"query file is not valid JSON: {path}: {exc}")


def _emit_error(err: PlannerError, as_json: bool) -> int:
    payload = {"ok": False, "error": type(err).__name__, "message": str(err)}
    if isinstance(err, Unplannable):
        payload["category"] = "unplannable"
        payload["paths"] = err.paths
    elif isinstance(err, CardinalityError):
        payload["category"] = "cardinality_violation"
        payload["violations"] = err.violations
    else:
        payload["category"] = "validation"
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"ERROR [{payload['category']}]: {err}", file=sys.stderr)
        if isinstance(err, Unplannable):
            for p in err.paths:
                print(f"  - metric {p['metric']} / fact {p['fact']} / "
                      f"dimension {p['dimension']}: {p['reason']}", file=sys.stderr)
        if isinstance(err, CardinalityError):
            for v in err.violations:
                print(f"  - {v['relation']} [{v['kind']}] key={v['key']!r} "
                      f"count={v['count']}", file=sys.stderr)
    return 2


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_build(args) -> int:
    path = build_database(args.sql, args.db)
    print(f"built database: {path}")
    return 0


def cmd_verify(args) -> int:
    model = load_model(args)
    conn = open_conn(args)
    try:
        model.validate_against_db(conn)
    except CardinalityError as err:
        return _emit_error(err, args.json)
    except PlannerError as err:
        return _emit_error(err, args.json)
    finally:
        conn.close()
    print("model OK: tables/columns exist and all declared cardinalities hold")
    return 0


def cmd_run(args) -> int:
    model = load_model(args)
    query = read_query(args.query)
    conn = open_conn(args)
    try:
        planner = Planner(model)
        result = planner.run(conn, query)
        columns, rows = flatten_rows(result)

        out_dir = Path(args.out) if args.out else None
        payload = as_json_payload(columns, rows, result.plan)
        if args.json and out_dir is None:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if out_dir is not None:
            write_csv(out_dir.with_suffix(".csv"), columns, rows)
            write_json(out_dir.with_suffix(".json"), payload)
            write_json(Path(str(out_dir) + ".plan.json"), result.plan)
            text = explain_text(model, result.bound, result.plan)
            plan_txt = Path(str(out_dir) + ".plan.txt")
            plan_txt.parent.mkdir(parents=True, exist_ok=True)
            plan_txt.write_text(text + "\n", encoding="utf-8")
            print(f"wrote {out_dir.with_suffix('.csv')}")
            print(f"wrote {out_dir.with_suffix('.json')}")
            print(f"wrote {Path(str(out_dir) + '.plan.json')}")
            print(f"wrote {plan_txt}")
        if args.show:
            from .output import print_table
            print_table(columns, rows)
        return 0
    except PlannerError as err:
        return _emit_error(err, args.json)
    finally:
        conn.close()


def cmd_explain(args) -> int:
    model = load_model(args)
    query = read_query(args.query)
    conn = open_conn(args)
    try:
        planner = Planner(model)
        result = planner.plan_only(conn, query)
        text = explain_text(model, result.bound, result.plan)
        if args.json:
            rendered = json.dumps(result.plan, ensure_ascii=False, indent=2)
            print(rendered)
        else:
            rendered = text
            print(rendered)
        if args.out:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(rendered + "\n", encoding="utf-8")
        return 0
    except PlannerError as err:
        return _emit_error(err, args.json)
    finally:
        conn.close()


def cmd_inspect(args) -> int:
    """Print the reachability matrix (useful for reviewers)."""
    model = load_model(args)
    conn = open_conn(args, need_db=bool(args.db))
    try:
        if conn is not None:
            model.validate_against_db(conn)
        print("fact -> dimension reachability (declared many_to_one paths):")
        for fact in model.facts:
            reachable = [d for d in model.dimensions if model.path(fact, d)]
            print(f"  {fact:<12} {reachable}")
        print("\nmetrics:")
        for name, m in model.metrics.items():
            if m.kind == "ratio":
                print(f"  {name:<14} ratio {m.numerator}/{m.denominator}")
            else:
                col = m.column or "*"
                print(f"  {name:<14} {m.kind} {m.fact}.{col}")
        return 0
    except PlannerError as err:
        return _emit_error(err, args.json)
    finally:
        if conn is not None:
            conn.close()


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="planner",
                                description="Small metric query planner (SQLite)")
    sub = p.add_subparsers(dest="command", required=True)

    def add_common(sp):
        sp.add_argument("--model", default="fixtures/model.json",
                        help="model declaration JSON (default: %(default)s)")
        sp.add_argument("--json", action="store_true",
                        help="machine-readable output")

    b = sub.add_parser("build", help="create the SQLite database from a SQL script")
    b.add_argument("--sql", default="fixtures/schema_and_data.sql")
    b.add_argument("--db", default=".runtime/task.db")
    b.set_defaults(func=cmd_build)

    v = sub.add_parser("verify", help="validate model + declared cardinalities")
    v.add_argument("--db", default=".runtime/task.db")
    add_common(v)
    v.set_defaults(func=cmd_verify)

    r = sub.add_parser("run", help="execute a query and write CSV/JSON/plan")
    r.add_argument("--db", default=".runtime/task.db")
    r.add_argument("--query", required=True)
    r.add_argument("--out", default=None,
                   help="output prefix; writes <out>.csv/.json/.plan.json/.plan.txt")
    r.add_argument("--show", action="store_true", help="print the table too")
    add_common(r)
    r.set_defaults(func=cmd_run)

    e = sub.add_parser("explain", help="print the execution plan without running")
    e.add_argument("--db", default=".runtime/task.db")
    e.add_argument("--query", required=True)
    e.add_argument("--out", default=None, help="also write the plan to this file")
    add_common(e)
    e.set_defaults(func=cmd_explain)

    i = sub.add_parser("inspect", help="show reachability matrix and metric catalog")
    i.add_argument("--db", default=None, help="optional: verify against this db")
    add_common(i)
    i.set_defaults(func=cmd_inspect)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except PlannerError as err:
        return _emit_error(err, getattr(args, "json", False))
    except FileNotFoundError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
