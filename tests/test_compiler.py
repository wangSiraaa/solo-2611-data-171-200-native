"""pytest 测试：编译、拒绝路径、手工选行、双库语义与辅助列隔离。

运行：.venv/bin/python -m pytest tests/ -q
双库测试在本机 PG16 实例可用时执行，否则跳过；纯编译器/手工选行测试始终运行。
"""

from __future__ import annotations

import json
import os
import sqlite3

import pytest

from pg2sqlite.compiler import compile_query, compile_report
from pg2sqlite.distinct_check import distinct_on_reference
from pg2sqlite.errors import CompileError
from pg2sqlite.lexer import tokenize
from pg2sqlite.parser import parse
from pg2sqlite.verifier import build_sqlite_db

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "fixtures")


def _read(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return f.read()


def _queries():
    with open(os.path.join(FIX, "queries.json"), encoding="utf-8") as f:
        return {q["query_id"]: q for q in json.load(f)}


PG_DDL = _read("source.sql")
LITE_DDL = _read("target.sql")
ROWS = json.loads(_read("rows.json"))
SOURCE_ROWS = [dict(zip(ROWS["events"]["columns"], r))
               for r in ROWS["events"]["rows"]]


# ---------------------------------------------------------------------------
# 词法/语法：参数与字符串隔离、原位置拒绝
# ---------------------------------------------------------------------------

def test_string_dollar_one_is_not_a_parameter():
    sql = "SELECT concat('$1', label) FROM events"
    toks = tokenize(sql)
    assert not any(t.kind == "PARAM" and t.text == "$1" for t in toks)
    assert any(t.kind == "STRING" and t.text == "'$1'" for t in toks)


def test_regex_rejected_with_position():
    q = _queries()["unsupported_regex"]
    out = compile_report(q, PG_DDL, LITE_DDL)
    assert out["status"] == "rejected"
    err = out["error"]
    assert err["rule"] == "unsupported_syntax"
    assert err["position"]["offset"] == 40
    assert "^" in err["snippet"]


def test_nondeterministic_rejected_not_appended():
    q = _queries()["nondeterministic"]
    out = compile_report(q, PG_DDL, LITE_DDL)
    assert out["status"] == "rejected"
    assert out["error"]["rule"] == "nondeterministic_distinct"
    # 拒绝信息明确说明不会自行追加排序
    assert "不会自行追加" in out["error"]["message"]


@pytest.mark.parametrize("bad_sql,rule", [
    ("SELECT * FROM events JOIN x ON events.event_id = x.id",
     "unsupported_syntax"),
    ("SELECT count(*) FROM events", "unsupported_syntax"),
    ("SELECT DISTINCT label FROM events", "unsupported_syntax"),
    ("WITH RECURSIVE r AS (SELECT 1) SELECT * FROM r",
     "unsupported_syntax"),
    ("SELECT event_id FROM events GROUP BY event_id",
     "unsupported_syntax"),
    ("SELECT event_id FROM events UNION SELECT event_id FROM events",
     "unsupported_syntax"),
    ("SELECT label || 'x' FROM events", "unsupported_syntax"),
    ("SELECT row_number() OVER () FROM events", "unsupported_syntax"),
])
def test_unsupported_syntax_rejected(bad_sql, rule):
    with pytest.raises(CompileError) as ei:
        compile_query(bad_sql, [], PG_DDL, LITE_DDL)
    assert ei.value.rule == rule
    assert ei.value.position is not None


def test_multiple_ctes_rejected():
    sql = ("WITH a AS (SELECT event_id FROM events), "
           "b AS (SELECT event_id FROM events) SELECT * FROM a")
    with pytest.raises(CompileError) as ei:
        compile_query(sql, [], PG_DDL, LITE_DDL)
    assert ei.value.rule == "scope_violation"


# ---------------------------------------------------------------------------
# 参数：同值多处引用、类型校验、布尔不被当作整数
# ---------------------------------------------------------------------------

def test_parameter_binding_order_and_repeat():
    sql = ("SELECT event_id FROM events WHERE customer_id >= $1 "
           "AND customer_id <= $1 AND active IS DISTINCT FROM $2")
    c = compile_query(sql, ["integer", "boolean"], PG_DDL, LITE_DDL)
    # $1 出现两次 -> 两个占位；$2 经三值逻辑展开为三个占位
    sources = [b["source_index"] for b in c.binding_map]
    assert sources[0:2] == [1, 1]
    assert all(s == 2 for s in sources[2:])
    seq = c.bindings_for([2, False])
    assert seq[0] == 2 and seq[1] == 2
    assert all(v == 0 for v in seq[2:])  # False -> 0，未字符串化


def test_boolean_param_rejects_python_int():
    from pg2sqlite.types import validate_parameters
    with pytest.raises(CompileError):
        validate_parameters(["boolean"], [1])  # int 不是 bool


def test_integer_param_rejects_bool():
    from pg2sqlite.types import validate_parameters
    with pytest.raises(CompileError):
        validate_parameters(["integer"], [True])


def test_param_index_out_of_range():
    with pytest.raises(CompileError) as ei:
        compile_query("SELECT $2 FROM events", ["integer"], PG_DDL, LITE_DDL)
    assert ei.value.rule == "invalid_parameter"


# ---------------------------------------------------------------------------
# concat：NULL 空串、仅 text 类型
# ---------------------------------------------------------------------------

def test_concat_null_becomes_empty_string():
    c = compile_query(
        "SELECT concat('a', label, 'b') AS s FROM events",
        [], PG_DDL, LITE_DDL)
    sql = c.sqlite_sql
    assert "COALESCE(CAST(label AS TEXT), '')" in sql
    # 字面量不包 COALESCE
    assert "'a'" in sql and "'b'" in sql


def test_concat_rejects_integer_argument():
    with pytest.raises(CompileError) as ei:
        compile_query("SELECT concat(event_id) FROM events",
                      [], PG_DDL, LITE_DDL)
    assert ei.value.rule == "type_error"


# ---------------------------------------------------------------------------
# DISTINCT ON：确定性、辅助列隔离、手工选行
# ---------------------------------------------------------------------------

def test_distinct_on_helper_columns_not_in_output():
    q = _queries()["distinct_latest"]
    c = compile_query(q["sql"], q["parameter_types"], PG_DDL, LITE_DDL)
    # 排名列只在内层
    assert "rn__rank" in c.sqlite_sql
    outer = c.sqlite_sql.split(" FROM (", 1)[0]
    assert "rn__rank" not in outer
    assert "tick_ord" not in outer
    # 输出 schema 严格三列且顺序固定
    assert [s["name"] for s in c.output_schema] == \
        ["customer_id", "event_id", "label"]


def test_manual_distinct_on_selection_matches_expected_rows():
    q = _queries()["distinct_latest"]
    tree = parse(q["sql"])
    ref = distinct_on_reference(tree, SOURCE_ROWS, [])
    # 同组同 tick（cust 1 的 event 1/2）由 event_id DESC 打破，选 event 2
    by_cust = {r["customer_id"]: r["event_id"] for r in ref}
    assert by_cust == {1: 2, 2: 5, 3: 6}


def test_distinct_on_requires_event_id():
    sql = ("SELECT DISTINCT ON (customer_id) customer_id, label "
           "FROM events ORDER BY customer_id, tick")
    with pytest.raises(CompileError) as ei:
        compile_query(sql, [], PG_DDL, LITE_DDL)
    assert ei.value.rule == "nondeterministic_distinct"


def test_distinct_on_prefix_mismatch_rejected():
    sql = ("SELECT DISTINCT ON (customer_id) customer_id, event_id "
           "FROM events ORDER BY tick, customer_id, event_id")
    with pytest.raises(CompileError) as ei:
        compile_query(sql, [], PG_DDL, LITE_DDL)
    assert ei.value.rule == "invalid_ordering"


# ---------------------------------------------------------------------------
# SQLite 执行语义：布尔映回、NULL、concat、三值逻辑
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def sqlite_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("db") / "target.db"
    build_sqlite_db(str(path), LITE_DDL, ROWS)
    return str(path)


def _exec(sqlite_db, compiled, params):
    conn = sqlite3.connect(f"file:{sqlite_db}?mode=ro", uri=True)
    try:
        cur = conn.execute(compiled.sqlite_sql,
                           compiled.bindings_for(params))
        cols = [d[0] for d in cur.description]
        return cols, cur.fetchall()
    finally:
        conn.close()


def test_nullsafe_boolean_mapping(sqlite_db):
    q = _queries()["nullsafe"]
    c = compile_query(q["sql"], q["parameter_types"], PG_DDL, LITE_DDL)
    cols, rows = _exec(sqlite_db, c, q["parameters"])
    assert cols == ["event_id", "changed"]
    # active NULL 的行（3,6）与 NULL 参数“不区分” -> False(0)；其余 True(1)
    result = dict(rows)
    assert result == {1: 1, 2: 1, 3: 0, 4: 1, 5: 1, 6: 0}


def test_is_distinct_from_boolean_param_true(sqlite_db):
    sql = ("SELECT event_id, active IS DISTINCT FROM $1 AS d "
           "FROM events ORDER BY event_id")
    c = compile_query(sql, ["boolean"], PG_DDL, LITE_DDL)
    cols, rows = _exec(sqlite_db, c, [True])
    d = dict(rows)
    # active=t(1,4)->0; f(2,5)->1; NULL(3,6)->1
    assert d == {1: 0, 2: 1, 3: 1, 4: 0, 5: 1, 6: 1}


def test_concat_null_label(sqlite_db):
    q = _queries()["concat_nulls"]
    c = compile_query(q["sql"], q["parameter_types"], PG_DDL, LITE_DDL)
    cols, rows = _exec(sqlite_db, c, [])
    captions = dict(rows)
    # event 5 的 label 为 NULL -> 空串，不传播 NULL
    assert captions[5] == "name=;"
    assert captions[1] == "name=alpha;"


def test_cte_query_executes_and_keeps_literal_dollar(sqlite_db):
    q = _queries()["cte_parameters"]
    c = compile_query(q["sql"], q["parameter_types"], PG_DDL, LITE_DDL)
    assert "' literal $1'" in c.sqlite_sql
    cols, rows = _exec(sqlite_db, c, q["parameters"])
    assert cols == ["customer_id", "event_id", "name"]
    names = {r[0]: r[2] for r in rows}
    # cust 1 选 event 2（label beta）；cust 2 选 event 5，其 label 为 NULL，
    # concat 按空串处理；$2='!'，字面量里的 '$1' 原样保留。
    assert names[1] == "beta! literal $1"
    assert names[2] == "! literal $1"


def test_cte_helper_columns_do_not_escape_scope():
    q = _queries()["cte_parameters"]
    c = compile_query(q["sql"], q["parameter_types"], PG_DDL, LITE_DDL)
    outer = c.sqlite_sql.split(") SELECT", 1)[1]
    # 内层窗口排名列、辅助排序列、子查询别名均不得穿透 CTE 到外层
    for forbidden in ("rn__rank", "tick_ord", "t0__"):
        assert forbidden not in outer
    # 外层输出列顺序固定为 CTE 声明的三列
    assert [s["name"] for s in c.output_schema] == \
        ["customer_id", "event_id", "name"]


def test_nulls_last_ordering_preserved(sqlite_db):
    q = _queries()["concat_nulls"]
    c = compile_query(q["sql"], q["parameter_types"], PG_DDL, LITE_DDL)
    _, rows = _exec(sqlite_db, c, [])
    # tick DESC NULLS LAST：tick NULL 的 event 3/6 必须在最后
    assert [r[0] for r in rows][-2:] == [3, 6]


# ---------------------------------------------------------------------------
# sqlglot 独立解析交叉校验（仅解析，绝不 transpile）
# ---------------------------------------------------------------------------

def test_sqlglot_parses_every_accepted_fixture():
    import sqlglot
    for qid in ("distinct_latest", "cte_parameters", "nullsafe",
                "concat_nulls"):
        q = _queries()[qid]
        # 两家解析器都应无异常；我们用自己的 AST 做编译，sqlglot 仅佐证语法成立
        sqlglot.parse_one(q["sql"], read="postgres")
        parse(q["sql"])


def test_sqlglot_confirms_regex_is_dialect_operator():
    import sqlglot
    from sqlglot import exp
    q = _queries()["unsupported_regex"]
    tree = sqlglot.parse_one(q["sql"], read="postgres")
    # 正则节点确实存在，证明我们的拒绝是主动 scope 限制而非解析失败
    assert tree.find(exp.RegexpLike) is not None
