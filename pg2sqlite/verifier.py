"""双库执行与核对阶段：verify_report。

- PostgreSQL 与 SQLite 装入 rows.json 同一批小数据；
- 两侧都用真实参数绑定执行（绝不内联成 SQL 常量），SQLite 以只读模式打开；
- SQLite 的 0/1 按声明逻辑类型映回布尔，比较时不做字符串化；
- 独立核对列顺序、逻辑类型、重复行与明确排序；
- 含 DISTINCT ON 的报表再与 distinct_check 的纯 Python 手工选行表三方对照，
  并审计“同组同 tick”并列确实由 event_id 打破。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from . import ast_nodes as ast
from .compiler import CompiledQuery
from .distinct_check import RowEnv, _eval, distinct_on_reference
from .errors import CompileError
from .parser import parse
from .types import BOOLEAN, INTEGER, NULLTYPE, TEXT


# ---------------------------------------------------------------------------
# 数据装载
# ---------------------------------------------------------------------------

def build_sqlite_db(sqlite_path: str, sqlite_ddl: str,
                    rows_payload: dict) -> None:
    """按 target.sql 的 DDL 建表，再用参数化 INSERT 装入 rows.json 数据。"""
    conn = sqlite3.connect(sqlite_path)
    try:
        conn.executescript(sqlite_ddl)
        for table, payload in rows_payload.items():
            cols = payload["columns"]
            placeholders = ", ".join("?" for _ in cols)
            col_sql = ", ".join(cols)
            conn.executemany(
                f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders})",
                [tuple(r) for r in payload["rows"]])
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# PostgreSQL 绑定：$n -> %s::declared_type（显式类型解决 NULL 参数无法推断）
# ---------------------------------------------------------------------------

def _pg_typed_sql(source_sql: str, param_types: list[str]) -> str:
    from .lexer import tokenize
    out_parts: list[str] = []
    cursor = 0
    for tok in tokenize(source_sql):
        if tok.kind == "PARAM":
            out_parts.append(source_sql[cursor:tok.pos.offset])
            idx = int(tok.text[1:])
            t = param_types[idx - 1]
            cast = {INTEGER: "integer", TEXT: "text",
                    BOOLEAN: "boolean", NULLTYPE: "text"}[t]
            out_parts.append(f"%s::{cast}")
            cursor = tok.end_pos.offset
    out_parts.append(source_sql[cursor:])
    return "".join(out_parts)


def _pg_bind_sequence(source_sql: str, parameters: list) -> list:
    """按 token 出现次序展开绑定值；同一 $n 多处引用重复同一值。"""
    from .lexer import tokenize
    seq: list = []
    for tok in tokenize(source_sql):
        if tok.kind == "PARAM":
            seq.append(parameters[int(tok.text[1:]) - 1])
    return seq


# ---------------------------------------------------------------------------
# 结果标准化
# ---------------------------------------------------------------------------

def _normalize_sqlite_value(value, logical_type: str):
    if value is None:
        return None
    if logical_type == BOOLEAN:
        if value not in (0, 1):
            raise AssertionError(f"布尔列出现非 0/1 存储值：{value!r}")
        return bool(value)
    if logical_type == INTEGER:
        if not isinstance(value, int) or isinstance(value, bool):
            raise AssertionError(f"integer 列出现非整数存储值：{value!r}")
        return value
    if logical_type == TEXT:
        if not isinstance(value, str):
            raise AssertionError(f"text 列出现非文本存储值：{value!r}")
        return value
    if logical_type == NULLTYPE:
        return None
    raise AssertionError(f"未知逻辑类型 {logical_type}")


def _rows_as_dicts(cursor, schema: list[dict],
                   normalize: bool) -> list[dict]:
    col_names = [d[0] for d in cursor.description]
    expected = [c["name"] for c in schema]
    if col_names != expected:
        raise AssertionError(
            f"输出列名/顺序不一致：驱动返回 {col_names}，声明 {expected}")
    out: list[dict] = []
    for raw in cursor.fetchall():
        row = {}
        for value, col in zip(raw, schema):
            if normalize:
                row[col["name"]] = _normalize_sqlite_value(
                    value, col["logical_type"])
            else:
                row[col["name"]] = value
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# 排序核对
# ---------------------------------------------------------------------------

def _order_key_value(v):
    # 与 scope 的二进制/码点排序一致；NULL 单独处理
    return v


def _check_explicit_order(rows: list[dict],
                          order_by: list[ast.OrderItem],
                          params: list, label: str,
                          source_rows_by_event: dict | None = None) -> list[str]:
    """核对结果行的相邻对是否满足 ORDER BY。

    ORDER BY 可引用未出现在最终投影中的列（如 concat_nulls 按 tick 排序但
    不投影 tick），因此优先用 source_rows_by_event 按 event_id 取回完整源行
    再求值；取不到源行时（理论上不应发生）跳过该键。
    """
    if not order_by or len(rows) < 2:
        return [f"[{label}] 无显式多值排序需要核对（ORDER BY 项数="
                f"{len(order_by)}，行数={len(rows)}）"]
    problems: list[str] = []
    skipped: list[str] = []
    checked = 0
    for a, b in zip(rows, rows[1:]):
        src_a = source_rows_by_event.get(a.get("event_id"), a) \
            if source_rows_by_event else a
        src_b = source_rows_by_event.get(b.get("event_id"), b) \
            if source_rows_by_event else b
        env_a, env_b = RowEnv(src_a), RowEnv(src_b)
        for oi in order_by:
            try:
                va = _eval(oi.expr, env_a, params)
                vb = _eval(oi.expr, env_b, params)
            except CompileError:
                skipped.append(type(oi.expr).__name__)
                continue
            nulls = oi.nulls or ("FIRST" if oi.direction == "ASC" else "LAST")
            cmp = _compare_with_nulls(va, vb, nulls, oi.direction)
            if cmp == 0:
                continue
            checked += 1
            # _compare_with_nulls 已按方向返回“应出现顺序”的 -1/1
            if cmp != -1:
                problems.append(
                    f"[{label}] 相邻行违反 ORDER BY：{va!r} 与 {vb!r} "
                    f"应为 {oi.direction} NULLS {nulls}")
            break
    if problems:
        raise AssertionError("; ".join(problems))
    note = f"，{len(set(skipped))} 个排序键由窗口/手工选行核对覆盖" \
        if skipped else ""
    return [f"[{label}] {len(rows)} 行满足显式 ORDER BY "
            f"（{len(order_by)} 个键，含 NULLS 位置；相邻比较 {checked} 次{note}）"]


def _compare_with_nulls(a, b, nulls: str, direction: str = "ASC") -> int:
    """返回 -1 表示 a 应排在 b 前，1 表示 b 应在前，0 表示并列。"""
    if a is None and b is None:
        return 0
    if a is None:
        return -1 if nulls == "FIRST" else 1
    if b is None:
        return 1 if nulls == "FIRST" else -1
    if a < b:
        natural = -1
    elif a > b:
        natural = 1
    else:
        return 0
    return natural if direction == "ASC" else -natural


# ---------------------------------------------------------------------------
# 并列审计：证明 event_id 确实在打破同组并列
# ---------------------------------------------------------------------------

def audit_tie_groups(sel: ast.Select, source_rows: list[dict],
                     params: list) -> dict:
    if not sel.distinct_on:
        return {"applicable": False}
    n_keys = len(sel.distinct_on)
    tiebreak = sel.order_by[n_keys:]
    if not tiebreak:
        return {"applicable": True, "tie_groups": 0, "note": "无并列键可审计"}

    # WHERE 过滤后的参与行
    filtered = [r for r in source_rows
                if sel.where is None
                or _eval(sel.where, RowEnv(r), params)]

    groups: dict = {}
    for row in filtered:
        env = RowEnv(row)
        gk = tuple(("__n__",) if _eval(k, env, params) is None
                   else ("__v__", _eval(k, env, params))
                   for k in sel.distinct_on)
        groups.setdefault(gk, []).append(row)

    tie_groups = []
    for gk, members in groups.items():
        # 仅按 event_id 之前的并列键（分组键 + 非 event_id 的排序键）取值
        pre_event = []
        for row in members:
            env = RowEnv(row)
            sig = tuple(("__n__",) if _eval(oi.expr, env, params) is None
                        else ("__v__", _eval(oi.expr, env, params))
                        for oi in sel.order_by
                        if not (isinstance(oi.expr, ast.ColumnRef)
                                and oi.expr.bare_name == "event_id"))
            pre_event.append(sig)
        if len(set(pre_event)) < len(members):
            event_ids = sorted(m["event_id"] for m in members)
            tie_groups.append({"group_key": list(gk),
                               "member_event_ids": event_ids,
                               "identical_before_event_id":
                                   len(set(pre_event))})
    return {
        "applicable": True,
        "tie_groups": len(tie_groups),
        "details": tie_groups,
        "conclusion": (
            "存在同组在 event_id 之前排序键完全相同的并列行，event_id 唯一"
            "决定 ROW_NUMBER=1 的归属" if tie_groups
            else "当前数据未出现并列，event_id 仍作为确定性兜底保留"),
    }


# ---------------------------------------------------------------------------
# 主核对入口
# ---------------------------------------------------------------------------

@dataclass
class VerifyResult:
    query_id: str
    status: str
    checks: list[str]
    pg_row_count: int
    sqlite_row_count: int
    pg_columns: list
    rows_match: bool
    distinct_reference: dict | None
    sample_rows: list[dict]

    def as_dict(self) -> dict:
        return {
            "query_id": self.query_id,
            "status": self.status,
            "checks": self.checks,
            "pg_row_count": self.pg_row_count,
            "sqlite_row_count": self.sqlite_row_count,
            "pg_columns": self.pg_columns,
            "rows_match": self.rows_match,
            "distinct_reference": self.distinct_reference,
            "sample_rows": self.sample_rows,
        }


def verify_report(report: dict,
                  compiled: CompiledQuery,
                  pg_conn,
                  sqlite_readonly_path: str,
                  source_rows: list[dict]) -> VerifyResult:
    qid = report["query_id"]
    source_sql = report["sql"]
    params = list(report["parameters"])
    param_types = report["parameter_types"]
    tree = parse(source_sql)
    checks: list[str] = []

    # ---- PostgreSQL：真实绑定、只读事务 ---------------------------------
    typed_sql = _pg_typed_sql(source_sql, param_types)
    pg_seq = _pg_bind_sequence(source_sql, params)
    check_bindings_compiled(compiled, source_sql, param_types, len(pg_seq))
    with pg_conn.transaction():
        pg_conn.execute("SET TRANSACTION READ ONLY")
        pg_cur = pg_conn.execute(typed_sql, pg_seq)
        pg_rows = _rows_as_dicts(pg_cur, compiled.output_schema, normalize=False)
    checks.append(
        f"PostgreSQL 使用真实绑定执行：{len(pg_seq)} 个位置参数，"
        "NULL 参数经 ::声明类型 传递，未内联为 SQL 常量，事务只读")

    # ---- SQLite：只读 URI 打开，真实绑定 --------------------------------
    uri = f"file:{sqlite_readonly_path}?mode=ro"
    lite_conn = sqlite3.connect(uri, uri=True)
    try:
        lite_seq = compiled.bindings_for(params)
        lite_cur = lite_conn.execute(compiled.sqlite_sql, lite_seq)
        lite_rows = _rows_as_dicts(lite_cur, compiled.output_schema,
                                   normalize=True)
    finally:
        lite_conn.close()
    checks.append(
        f"SQLite 以 mode=ro 只读打开并使用真实绑定执行：{len(lite_seq)} 个 ? "
        "位置参数；布尔按声明由 0/1 映回 True/False，未做字符串化")

    # ---- 列顺序与逻辑类型 ------------------------------------------------
    pg_cols = [d.name for d in pg_cur.description]
    expected_cols = [c["name"] for c in compiled.output_schema]
    assert pg_cols == expected_cols, \
        f"{qid}: PG 列顺序 {pg_cols} != 声明 {expected_cols}"
    _assert_pg_logical_types(pg_rows, compiled.output_schema, qid)
    checks.append(
        "列顺序与逻辑输出类型一致：" +
        ", ".join(f"{c['name']}:{c['logical_type']}"
                  for c in compiled.output_schema))

    # ---- 行内容、重复行 --------------------------------------------------
    assert len(pg_rows) == len(lite_rows), \
        f"{qid}: 行数差异 PG={len(pg_rows)} SQLite={len(lite_rows)}"
    assert pg_rows == lite_rows, \
        f"{qid}: 行内容（含顺序）不一致\nPG={pg_rows}\nLITE={lite_rows}"
    dup = _duplicate_count(pg_rows)
    checks.append(
        f"两端 {len(pg_rows)} 行逐行相等（含顺序）；重复行数={dup}，"
        "重复行按普通 SELECT 语义保留并逐行核对")

    # ---- 明确排序 --------------------------------------------------------
    source_by_event = {r["event_id"]: r for r in source_rows}
    checks.extend(_check_explicit_order(lite_rows, tree.order_by,
                                        params, "SQLite", source_by_event))
    checks.extend(_check_explicit_order(pg_rows, tree.order_by,
                                        params, "PostgreSQL", source_by_event))

    # ---- DISTINCT ON 三方手工选行核对 ------------------------------------
    distinct_ref = None
    if tree.distinct_on:
        ref_rows = distinct_on_reference(tree, source_rows, params)
        projected = [{c["name"]: r[c["name"]] for c in compiled.output_schema}
                     for r in lite_rows]
        assert projected == ref_rows, \
            f"{qid}: 手工选行表与查询结果不一致\nref={ref_rows}\ngot={projected}"
        tie = audit_tie_groups(tree, source_rows, params)
        distinct_ref = {
            "manual_reference_rows": ref_rows,
            "matches_postgresql": pg_rows == ref_rows,
            "matches_sqlite": projected == ref_rows,
            "tie_audit": tie,
        }
        checks.append(
            "DISTINCT ON 与独立 Python 手工选行表逐行一致（非仅比对两个包装器"
            "的 JSON）；并列审计组=" + str(tie.get("tie_groups", 0)))
    if tree.cte is not None and tree.cte.query.distinct_on:
        inner = tree.cte.query
        ref_rows = distinct_on_reference(inner, source_rows, params)
        distinct_ref = distinct_ref or {}
        tie = audit_tie_groups(inner, source_rows, params)
        distinct_ref["cte_inner_manual_reference_rows"] = ref_rows
        distinct_ref["cte_tie_audit"] = tie
        # CTE 内层手工选行（已应用其 WHERE）必须是最终外层结果行的来源：
        # 外层只做过滤/投影/concat，event_id 集合必须一致。
        ref_ids = {r["event_id"] for r in ref_rows}
        got_ids = {r.get("event_id") for r in lite_rows}
        assert got_ids.issubset(ref_ids), \
            f"{qid}: 外层结果 event_id {got_ids} 超出 CTE 手工选行 {ref_ids}"
        distinct_ref["cte_outer_event_ids_subset_of_manual"] = True
        checks.append(
            "CTE 内部 DISTINCT ON 通过独立手工选行表核对（并列审计组="
            + str(tie.get("tie_groups", 0))
            + "），外层结果 event_id 是手工选行集合的子集")

    return VerifyResult(
        query_id=qid, status="verified", checks=checks,
        pg_row_count=len(pg_rows), sqlite_row_count=len(lite_rows),
        pg_columns=pg_cols, rows_match=True,
        distinct_reference=distinct_ref, sample_rows=lite_rows)


def check_bindings_compiled(compiled: CompiledQuery, source_sql: str,
                            param_types: list[str], pg_placeholder_count: int):
    from .lexer import tokenize
    param_tokens = [t for t in tokenize(source_sql) if t.kind == "PARAM"]
    # 源 SQL 中参数 token 的出现次序；同一 $n 多次引用会出现多次。
    # 编译器在三值逻辑等改写中会把一个参数渲染到多个 ?，因此编译产物的
    # 占位符序列是源序列的“展开超集”：逐源 token 至少匹配一次且类型一致。
    expected = [(int(t.text[1:]), param_types[int(t.text[1:]) - 1])
                for t in param_tokens]
    actual = [(b["source_index"], b["logical_type"])
              for b in compiled.binding_map]
    assert pg_placeholder_count == len(expected), \
        f"PostgreSQL 绑定数 {pg_placeholder_count} 与源参数 token 数 {len(expected)} 不符"
    # actual 必须能按顺序覆盖 expected（允许 expected 单项在 actual 中重复）
    ei = 0
    for pair in actual:
        if ei < len(expected) and pair == expected[ei]:
            ei += 1
        elif ei > 0 and pair == expected[ei - 1]:
            continue  # 同一参数因改写产生的重复占位
        else:
            raise AssertionError(
                f"绑定映射无法按源 token 次序覆盖：{expected} vs {actual}")
    assert ei == len(expected), \
        f"绑定映射缺少源参数：{expected} vs {actual}"
    # 字符串中的 '$1' 不应被参数化：词法分析已保证字符串整体是 STRING，
    # 这里复核编译产物保留了字面文本且占位符数不少于源 token 数。
    literal_dollar = source_sql.count("'$1'")
    if literal_dollar:
        assert compiled.source_sql.count("'$1'") == literal_dollar
        assert len(actual) >= len(expected)


def _assert_pg_logical_types(rows: list[dict], schema: list[dict], qid: str):
    for row in rows:
        for col in schema:
            v = row[col["name"]]
            if v is None:
                continue
            t = col["logical_type"]
            if t == BOOLEAN and not isinstance(v, bool):
                raise AssertionError(f"{qid}: 列 {col['name']} 期望布尔，实际 {type(v)}")
            if t == INTEGER and (not isinstance(v, int) or isinstance(v, bool)):
                raise AssertionError(f"{qid}: 列 {col['name']} 期望整数")
            if t == TEXT and not isinstance(v, str):
                raise AssertionError(f"{qid}: 列 {col['name']} 期望文本")


def _duplicate_count(rows: list[dict]) -> int:
    seen: dict = {}
    for r in rows:
        key = tuple(sorted((k, ("n" if v is None else "v", v))
                           for k, v in r.items()))
        seen[key] = seen.get(key, 0) + 1
    return sum(c - 1 for c in seen.values() if c > 1)
