"""编译阶段入口：compile_query / compile_report。

输入：源 PostgreSQL SQL、参数声明类型、两端 DDL（用于声明类型与列校验）。
输出：CompiledQuery（SQLite SQL、绑定映射、逻辑输出类型、改写依据）。
任何 scope 之外的语法或不确定选行都抛 CompileError，由调用方跳过该报表并
继续处理其余报表。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .codegen import Codegen, output_schema
from .distinct import check_distinct_on
from .errors import CompileError
from .parser import parse
from .types import (
    BOOLEAN, INTEGER, TypeChecker, TableSchema,
    parse_ddl, validate_parameters,
)


@dataclass
class CompiledQuery:
    query_id: str
    sqlite_sql: str
    # 绑定映射：SQLite ? 的次序 -> (源 $n, 逻辑类型)；同一 $n 可出现多次
    binding_map: list[dict]
    output_schema: list[dict]
    rewrites: list[str] = field(default_factory=list)
    distinct_evidence: list[str] = field(default_factory=list)
    source_sql: str = ""

    def bindings_for(self, parameters: list) -> list:
        """按 binding_map 把真实绑定值展开成 SQLite 位置参数序列。

        binding_map 与生成 SQL 中的 ? 一一对应；同一 $n 因三值逻辑等改写
        出现多次时，此处重复给出同一绑定值，保证占位符数量精确匹配。
        """
        out: list = []
        for entry in self.binding_map:
            value = parameters[entry["source_index"] - 1]
            logical = entry["logical_type"]
            if value is None:
                out.append(None)
            elif logical == BOOLEAN:
                # 布尔物理存为 0/1；不经过字符串化
                out.append(1 if value else 0)
            else:
                out.append(value)  # integer / text 原样
        return out

    def as_dict(self) -> dict:
        return {
            "query_id": self.query_id,
            "sqlite_sql": self.sqlite_sql,
            "binding_map": self.binding_map,
            "output_schema": self.output_schema,
            "rewrites": self.rewrites,
            "distinct_evidence": self.distinct_evidence,
        }


def compile_query(source_sql: str,
                  parameter_types: list[str],
                  pg_ddl: str,
                  sqlite_ddl: str,
                  query_id: str = "") -> CompiledQuery:
    # 1) 参数声明本身先校验
    validate_parameters(parameter_types, None)

    # 2) 解析（手写递归下降；scope 外结构在原位置拒绝）
    tree = parse(source_sql)

    # 3) 两端 DDL 必须声明同一组表/列/逻辑类型；SQLite 布尔列物理是 INTEGER，
    #    以 PostgreSQL DDL 的 BOOLEAN 声明作为逻辑类型来源。
    pg_tables = parse_ddl(pg_ddl)
    lite_tables = parse_ddl(sqlite_ddl)
    _cross_check_ddl(pg_tables, lite_tables)

    # 4) 类型推断
    checker = TypeChecker(pg_tables, parameter_types)
    try:
        node_types = checker.infer(tree)

        # 5) DISTINCT ON 确定性校验（内外两层都查；只检查不追加排序）
        distinct_evidence: list[str] = []
        distinct_evidence.extend(
            _check_select_distinct(tree, pg_tables, checker))

        # 6) 代码生成（含 CTE 包装）
        gen = Codegen(node_types, checker.cte_outputs)
        sqlite_sql = _emit(tree, gen)
    except CompileError as exc:
        # 语义阶段（distinct/types）构造错误时可能未携带源文本，统一回填，
        # 保证所有拒绝都能打印原位置 caret。
        if exc.source is None and exc.position is not None:
            exc.source = source_sql
        raise

    return CompiledQuery(
        query_id=query_id,
        sqlite_sql=sqlite_sql,
        binding_map=[{"placeholder_index": i + 1,
                      "source_index": idx,
                      "source_token": f"${idx}",
                      "logical_type": t}
                     for i, (idx, t) in enumerate(gen.binding_map)],
        output_schema=output_schema(tree, node_types),
        rewrites=gen.rewrites,
        distinct_evidence=distinct_evidence,
        source_sql=source_sql,
    )


def compile_report(report: dict, pg_ddl: str, sqlite_ddl: str) -> dict:
    """面向 queries.json 单条记录的便捷封装；返回结构化结果/错误字典。"""
    qid = report.get("query_id", "<unknown>")
    try:
        compiled = compile_query(
            report["sql"],
            report.get("parameter_types", []),
            pg_ddl, sqlite_ddl, query_id=qid)
        return {"query_id": qid, "status": "compiled",
                "compiled": compiled.as_dict()}
    except CompileError as exc:
        return {"query_id": qid, "status": "rejected", "error": exc.as_dict()}


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------

def _cross_check_ddl(pg: dict[str, TableSchema],
                     lite: dict[str, TableSchema]) -> None:
    if set(pg) != set(lite):
        raise CompileError(
            "scope_violation",
            f"两端 DDL 表集合不一致：PG={sorted(pg)} SQLite={sorted(lite)}")
    for name, pg_sch in pg.items():
        lite_sch = lite[name]
        if pg_sch.columns != lite_sch.columns:
            raise CompileError(
                "scope_violation",
                f"表 {name} 列顺序不一致：{pg_sch.columns} vs {lite_sch.columns}")
        for col in pg_sch.columns:
            pt, lt = pg_sch.types[col], lite_sch.types[col]
            # SQLite 侧 BOOLEAN 物理列为 INTEGER，逻辑类型以 PG 声明为准
            if pt == BOOLEAN and lt == INTEGER:
                continue
            if pt != lt:
                raise CompileError(
                    "scope_violation",
                    f"表 {name}.{col} 两端类型不一致：{pt} vs {lt}")


def _check_select_distinct(tree, pg_tables, checker: TypeChecker) -> list[str]:
    evidence: list[str] = []

    def one(sel, relation: str):
        if not sel.distinct_on:
            return
        if relation in checker.cte_outputs:
            schema = TableSchema(
                relation,
                [n for n, _ in checker.cte_outputs[relation]],
                {n: t for n, t in checker.cte_outputs[relation]})
        else:
            schema = pg_tables[relation.strip('"')]
        evidence.extend(check_distinct_on(sel, schema, relation))

    if tree.cte is not None:
        inner_rel = tree.cte.query.table
        if inner_rel is None:
            raise CompileError("scope_violation",
                               "CTE 内 SELECT 缺少 FROM", tree.cte.src_pos)
        one(tree.cte.query, inner_rel)
    if tree.table is not None:
        one(tree, tree.table)
    return evidence


def _emit(tree, gen: Codegen) -> str:
    if tree.cte is None:
        return gen.select(tree)

    # 一个非递归 CTE：CTE 名称与输出列顺序原样保留，外层引用不串名。
    cte = tree.cte
    inner_sql = gen.select(cte.query)
    declared = cte.columns
    if declared:
        col_list = " (" + ", ".join(declared) + ")"
    else:
        col_list = ""
    outer_sql = gen.select(tree)
    gen.rewrites.append(
        f"WITH {cte.name}{col_list} -> SQLite WITH {cte.name}{col_list}："
        "CTE 名称、输出列名与列顺序按内部 SELECT 原样保留，外层只按名称引用，"
        "重写产生的辅助排名列不会穿透 CTE 边界")
    return f"WITH {cte.name.strip(chr(34))}{col_list} AS ({inner_sql}) {outer_sql}"
