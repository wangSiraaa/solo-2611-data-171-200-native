"""递归下降语法分析器。

严格限定 scope.json 允许的子集；任何超出子集的结构（JOIN、GROUP BY、
集合操作、普通 DISTINCT、窗口函数、未知函数……）都在出现的 token 原位置
抛 CompileError(unsupported_syntax / scope_violation)，由上层跳过该报表。
"""

from __future__ import annotations

from . import ast_nodes as ast
from .errors import CompileError
from .lexer import Token, tokenize

# scope 之外、遇到即拒绝的函数（保留原位置）
KNOWN_UNSUPPORTED_FUNCS = frozenset({
    "ROW_NUMBER", "RANK", "DENSE_RANK", "LAG", "LEAD", "SUM", "COUNT",
    "AVG", "MIN", "MAX", "STRING_AGG", "ARRAY_AGG", "COALESCE", "NULLIF",
    "SUBSTRING", "SUBSTR", "LOWER", "UPPER", "TRIM", "LENGTH",
    "REGEXP_REPLACE", "REGEXP_MATCHES",
})
# 显式允许的函数（参数与类型由类型检查阶段约束）
ALLOWED_FUNCS = frozenset({"CONCAT"})

COMPARISONS = {"=", "<>", "!=", "<", ">", "<=", ">="}
ARITHMETIC = {"+", "-", "*", "/", "%"}


class Parser:
    def __init__(self, source: str):
        self.src = source
        self.tokens = tokenize(source)
        self.pos_idx = 0

    # ---- token 基础操作 --------------------------------------------------
    @property
    def cur(self) -> Token | None:
        return self.tokens[self.pos_idx] if self.pos_idx < len(self.tokens) else None

    def _err(self, rule: str, msg: str, token: Token | None = None) -> CompileError:
        tok = token or self.cur
        pos = tok.pos if tok is not None else None
        return CompileError(rule, msg, pos, self.src)

    def _kw(self, word: str) -> bool:
        t = self.cur
        return t is not None and t.kind == "KEYWORD" and t.text == word

    def _op(self, sym: str) -> bool:
        t = self.cur
        return t is not None and t.kind == "OP" and t.text == sym

    def _eat_kw(self, word: str) -> Token:
        if not self._kw(word):
            raise self._err("parse_error", f"期望关键字 {word}")
        tok = self.tokens[self.pos_idx]
        self.pos_idx += 1
        return tok

    def _eat_op(self, sym: str) -> Token:
        if not self._op(sym):
            raise self._err("parse_error", f"期望符号 {sym!r}")
        tok = self.tokens[self.pos_idx]
        self.pos_idx += 1
        return tok

    def _reject(self, word: str, rule: str, msg: str) -> None:
        tok = self._eat_kw(word)
        raise CompileError(rule, msg, tok.pos, self.src)

    # ---- 入口 ------------------------------------------------------------
    def parse_statement(self) -> ast.Select:
        node = self._parse_select()
        if self.cur is not None:
            if self._op(";"):
                self.pos_idx += 1
            if self.cur is not None:
                raise self._err("scope_violation",
                                "仅支持单条 SELECT 语句，语句后存在多余 token")
        return node

    # ---- SELECT 骨架 -----------------------------------------------------
    def _parse_select(self) -> ast.Select:
        cte = None
        if self._kw("WITH"):
            cte = self._parse_cte()

        start = self._eat_kw("SELECT")
        distinct_on: list[ast.Expr] = []
        if self._kw("DISTINCT"):
            distinct_on = self._parse_distinct_on()

        items = self._parse_select_items()
        table = None
        if self._kw("FROM"):
            table = self._parse_from()
        where = self._parse_where()
        order_by = self._parse_order_by()
        self._reject_trailing_clauses()

        return ast.Select(
            src_pos=start.pos, cte=cte, distinct_on=distinct_on,
            items=items, table=table, where=where, order_by=order_by)

    def _parse_cte(self) -> ast.Cte:
        start = self._eat_kw("WITH")
        if self._kw("RECURSIVE"):
            tok = self._eat_kw("RECURSIVE")
            raise CompileError(
                "unsupported_syntax",
                "RECURSIVE CTE 不在 scope 内（仅允许一个非递归 CTE）",
                tok.pos, self.src)
        name_tok = self._expect_ident()
        columns: list[str] | None = None
        if self._op("("):
            self._eat_op("(")
            columns = [self._expect_ident_name()]
            while self._op(","):
                self.pos_idx += 1
                columns.append(self._expect_ident_name())
            self._eat_op(")")
        self._eat_kw("AS")
        self._eat_op("(")
        inner = self._parse_select()
        self._eat_op(")")
        if self._op(",") or self._kw(","):
            pass
        if self.cur is not None and self._op(","):
            tok = self._eat_op(",")
            raise CompileError(
                "scope_violation",
                "仅允许一个非递归 CTE，检测到第二个 CTE 定义",
                tok.pos, self.src)
        return ast.Cte(src_pos=start.pos, name=name_tok.text,
                       columns=columns, query=inner)

    def _parse_distinct_on(self) -> list[ast.Expr]:
        start = self._eat_kw("DISTINCT")
        if not self._kw("ON"):
            raise CompileError(
                "unsupported_syntax",
                "普通 DISTINCT 去重在 scope 之外；仅支持 DISTINCT ON (...)",
                start.pos, self.src)
        self._eat_kw("ON")
        self._eat_op("(")
        keys = [self._parse_expr()]
        while self._op(","):
            self.pos_idx += 1
            keys.append(self._parse_expr())
        self._eat_op(")")
        return keys

    def _parse_select_items(self) -> list[ast.SelectItem]:
        items = [self._parse_select_item()]
        while self._op(","):
            self.pos_idx += 1
            items.append(self._parse_select_item())
        return items

    def _parse_select_item(self) -> ast.SelectItem:
        start_pos = (self.cur.pos if self.cur is not None else None)
        # SELECT * / t.* 显式拒绝：输出列必须显式声明，才能固定逻辑输出 schema
        if self._op("*"):
            tok = self._eat_op("*")
            raise CompileError(
                "unsupported_syntax",
                "SELECT * 不在 scope 内：报表必须显式列出输出列以固定列顺序",
                tok.pos, self.src)
        expr = self._parse_expr()
        alias = None
        if self._kw("AS"):
            self.pos_idx += 1
            alias = self._expect_ident_name()
        elif self.cur is not None and self.cur.kind == "IDENT":
            # 隐式别名
            alias = self._expect_ident_name()
        return ast.SelectItem(src_pos=start_pos, expr=expr, alias=alias)

    def _parse_from(self) -> str:
        self._eat_kw("FROM")
        name_tok = self._expect_ident()
        # 子查询 / JOIN / 多表一律拒绝
        if self._kw("JOIN") or self._kw("INNER") or self._kw("LEFT") \
                or self._kw("RIGHT") or self._kw("FULL") or self._kw("CROSS"):
            raise self._err("unsupported_syntax",
                            "JOIN/多表查询不在 scope 内，仅支持单表或单个 CTE")
        if self._op(","):
            raise self._err("scope_violation",
                            "FROM 仅允许一个关系（单表或 CTE），检测到逗号多表")
        return name_tok.text

    def _parse_where(self) -> ast.Expr | None:
        if not self._kw("WHERE"):
            return None
        self.pos_idx += 1
        return self._parse_expr()

    def _parse_order_by(self) -> list[ast.OrderItem]:
        if not self._kw("ORDER"):
            return []
        self._eat_kw("ORDER")
        self._eat_kw("BY")
        items = [self._parse_order_item()]
        while self._op(","):
            self.pos_idx += 1
            items.append(self._parse_order_item())
        return items

    def _parse_order_item(self) -> ast.OrderItem:
        start_pos = self.cur.pos if self.cur is not None else None
        if self.cur is not None and self.cur.kind == "NUMBER":
            raise self._err(
                "unsupported_syntax",
                "ORDER BY 序数位置（ORDER BY n）不在 scope 内，请使用列名/表达式")
        expr = self._parse_expr()
        direction = "ASC"
        if self._kw("ASC"):
            self.pos_idx += 1
        elif self._kw("DESC"):
            self.pos_idx += 1
            direction = "DESC"
        nulls: str | None = None
        if self._kw("NULLS"):
            self.pos_idx += 1
            if self._kw("FIRST"):
                nulls = "FIRST"
                self.pos_idx += 1
            elif self._kw("LAST"):
                nulls = "LAST"
                self.pos_idx += 1
            else:
                raise self._err("parse_error", "NULLS 后应为 FIRST 或 LAST")
        return ast.OrderItem(src_pos=start_pos, expr=expr,
                             direction=direction, nulls=nulls)

    def _reject_trailing_clauses(self) -> None:
        if self.cur is None:
            return
        reject_map = {
            "GROUP": ("GROUP BY 聚合不在 scope 内", "unsupported_syntax"),
            "HAVING": ("HAVING 聚合过滤不在 scope 内", "unsupported_syntax"),
            "LIMIT": ("LIMIT 不在本报表 scope 内", "unsupported_syntax"),
            "OFFSET": ("OFFSET 不在本报表 scope 内", "unsupported_syntax"),
            "UNION": ("集合操作 UNION 不在 scope 内", "unsupported_syntax"),
            "INTERSECT": ("集合操作 INTERSECT 不在 scope 内", "unsupported_syntax"),
            "EXCEPT": ("集合操作 EXCEPT 不在 scope 内", "unsupported_syntax"),
            "WINDOW": ("窗口子句不在 scope 内", "unsupported_syntax"),
        }
        if self.cur.kind == "KEYWORD" and self.cur.text in reject_map:
            msg, rule = reject_map[self.cur.text]
            raise self._err(rule, msg)
        if self._kw("JOIN") or self._kw("INNER") or self._kw("LEFT"):
            raise self._err("unsupported_syntax", "JOIN 不在 scope 内")

    # ---- 表达式：优先级下降 ---------------------------------------------
    def _parse_expr(self) -> ast.Expr:
        return self._parse_or()

    def _parse_or(self) -> ast.Expr:
        node = self._parse_and()
        while self._kw("OR"):
            tok = self.pos_idx
            self.pos_idx += 1
            right = self._parse_and()
            node = ast.BinaryOp(src_pos=self.tokens[tok].pos,
                                op="OR", left=node, right=right)
        return node

    def _parse_and(self) -> ast.Expr:
        node = self._parse_not()
        while self._kw("AND"):
            tok = self.pos_idx
            self.pos_idx += 1
            right = self._parse_not()
            node = ast.BinaryOp(src_pos=self.tokens[tok].pos,
                                op="AND", left=node, right=right)
        return node

    def _parse_not(self) -> ast.Expr:
        if self._kw("NOT"):
            tok = self._eat_kw("NOT")
            operand = self._parse_not()
            return ast.UnaryOp(src_pos=tok.pos, op="NOT", operand=operand)
        return self._parse_predicate()

    def _parse_predicate(self) -> ast.Expr:
        node = self._parse_additive()
        while True:
            tok = self.cur
            if tok is None:
                break
            # IS DISTINCT FROM / IS NULL / IS NOT ...
            if self._kw("IS"):
                node = self._parse_is(node)
                continue
            if tok.kind == "OP" and tok.text in COMPARISONS:
                self.pos_idx += 1
                right = self._parse_additive()
                node = ast.BinaryOp(src_pos=tok.pos, op=tok.text,
                                    left=node, right=right)
                continue
            if self._kw("IN"):
                node = self._parse_in(node, negated=False)
                continue
            if self._kw("BETWEEN"):
                node = self._parse_between(node, negated=False)
                continue
            if self._kw("LIKE"):
                node = self._parse_like(node, negated=False, ilike=False)
                continue
            if self._kw("ILIKE"):
                node = self._parse_like(node, negated=False, ilike=True)
                continue
            if self._kw("NOT"):
                nxt = self.tokens[self.pos_idx + 1] if \
                    self.pos_idx + 1 < len(self.tokens) else None
                if nxt is not None and nxt.kind == "KEYWORD" and \
                        nxt.text in ("IN", "BETWEEN", "LIKE", "ILIKE"):
                    self._eat_kw("NOT")
                    if self._kw("IN"):
                        node = self._parse_in(node, negated=True)
                    elif self._kw("BETWEEN"):
                        node = self._parse_between(node, negated=True)
                    else:
                        node = self._parse_like(
                            node, negated=True, ilike=self._kw("ILIKE"))
                    continue
                break
            if tok.kind == "OP" and tok.text == "~":
                raise CompileError(
                    "unsupported_syntax",
                    "正则匹配操作符 '~' 不在 scope 内（scope.json 明确拒绝 regex）",
                    tok.pos, self.src)
            if tok.kind == "OP" and tok.text == "||":
                raise CompileError(
                    "unsupported_syntax",
                    "'||' 连接符不在 scope 内：文本拼接只允许声明为 text 的 "
                    "concat(...)，避免 NULL 传播语义被静默改变",
                    tok.pos, self.src)
            break
        return node

    def _parse_is(self, node: ast.Expr) -> ast.Expr:
        is_tok = self._eat_kw("IS")
        negated = False
        if self._kw("NOT"):
            negated = True
            self.pos_idx += 1
        if self._kw("DISTINCT"):
            self._eat_kw("DISTINCT")
            self._eat_kw("FROM")
            right = self._parse_additive()
            return ast.IsDistinctFrom(src_pos=is_tok.pos, left=node,
                                      right=right, negated=negated)
        if self._kw("NULL"):
            self.pos_idx += 1
            return ast.IsNull(src_pos=is_tok.pos, operand=node, negated=negated)
        if self._kw("TRUE") or self._kw("FALSE"):
            # IS TRUE / IS FALSE 属于未声明方言特性，拒绝而非猜测
            raise self._err("unsupported_syntax",
                            "IS TRUE / IS FALSE 谓词不在 scope 内")
        raise self._err("parse_error", "IS 后仅支持 DISTINCT FROM 或 NULL")

    def _parse_in(self, node: ast.Expr, negated: bool) -> ast.InList:
        tok = self._eat_kw("IN")
        if not self._op("("):
            raise CompileError(
                "unsupported_syntax",
                "IN 仅支持字面值列表；子查询形式不在 scope 内",
                tok.pos, self.src)
        self._eat_op("(")
        if self._kw("SELECT") or self._kw("WITH"):
            raise self._err("unsupported_syntax",
                            "IN (子查询) 不在 scope 内")
        items = [self._parse_expr()]
        while self._op(","):
            self.pos_idx += 1
            items.append(self._parse_expr())
        self._eat_op(")")
        return ast.InList(src_pos=tok.pos, operand=node, items=items,
                          negated=negated)

    def _parse_between(self, node: ast.Expr, negated: bool) -> ast.Between:
        tok = self._eat_kw("BETWEEN")
        low = self._parse_additive()
        self._eat_kw("AND")
        high = self._parse_additive()
        return ast.Between(src_pos=tok.pos, operand=node, low=low,
                           high=high, negated=negated)

    def _parse_like(self, node: ast.Expr, negated: bool,
                    ilike: bool) -> ast.Like:
        tok = self.cur
        self.pos_idx += 1  # LIKE / ILIKE
        pattern = self._parse_additive()
        if ilike:
            raise CompileError(
                "unsupported_syntax",
                "ILIKE 依赖大小写折叠规则，不在 scope 内",
                tok.pos, self.src)
        return ast.Like(src_pos=tok.pos, operand=node, pattern=pattern,
                        negated=negated, ilike=ilike)

    def _parse_additive(self) -> ast.Expr:
        node = self._parse_multiplicative()
        while self.cur is not None and self.cur.kind == "OP" \
                and self.cur.text in ("+", "-"):
            tok = self.cur
            self.pos_idx += 1
            right = self._parse_multiplicative()
            node = ast.BinaryOp(src_pos=tok.pos, op=tok.text,
                                left=node, right=right)
        return node

    def _parse_multiplicative(self) -> ast.Expr:
        node = self._parse_unary()
        while self.cur is not None and self.cur.kind == "OP" \
                and self.cur.text in ("*", "/", "%"):
            tok = self.cur
            self.pos_idx += 1
            right = self._parse_unary()
            node = ast.BinaryOp(src_pos=tok.pos, op=tok.text,
                                left=node, right=right)
        return node

    def _parse_unary(self) -> ast.Expr:
        if self.cur is not None and self.cur.kind == "OP" \
                and self.cur.text == "-":
            tok = self.cur
            self.pos_idx += 1
            return ast.UnaryOp(src_pos=tok.pos, op="-",
                               operand=self._parse_unary())
        if self.cur is not None and self.cur.kind == "OP" \
                and self.cur.text == "+":
            self.pos_idx += 1
            return self._parse_unary()
        return self._parse_primary()

    def _parse_primary(self) -> ast.Expr:
        tok = self.cur
        if tok is None:
            raise self._err("parse_error", "表达式意外结束")

        if tok.kind == "NUMBER":
            self.pos_idx += 1
            return ast.IntegerLit(src_pos=tok.pos, value=int(tok.text))
        if tok.kind == "STRING":
            self.pos_idx += 1
            return ast.StringLit(src_pos=tok.pos, text=tok.text)
        if tok.kind == "PARAM":
            self.pos_idx += 1
            return ast.Param(src_pos=tok.pos, index=int(tok.text[1:]))
        if tok.kind == "OP" and tok.text == "(":
            self.pos_idx += 1
            inner = self._parse_expr()
            self._eat_op(")")
            return inner
        if tok.kind == "KEYWORD":
            if tok.text == "NULL":
                self.pos_idx += 1
                return ast.NullLit(src_pos=tok.pos)
            if tok.text in ("TRUE", "FALSE"):
                self.pos_idx += 1
                return ast.BoolLit(src_pos=tok.pos,
                                   value=(tok.text == "TRUE"))
            if tok.text == "CASE":
                return self._parse_case()
            if tok.text == "CAST":
                return self._parse_cast()
            raise self._err(
                "unsupported_syntax",
                f"关键字 {tok.text} 开头的表达式不在受限 scope 内")
        if tok.kind == "IDENT":
            return self._parse_column_or_func()
        raise self._err("parse_error", f"无法作为表达式开头的 token：{tok.text}")

    def _parse_column_or_func(self) -> ast.Expr:
        name_tok = self._expect_ident()
        if self._op("("):
            return self._parse_func_call(name_tok)
        # 列引用：可能带 table 限定
        if self._op("."):
            self.pos_idx += 1
            col_tok = self._expect_ident()
            if self._op("."):
                raise self._err("unsupported_syntax",
                                "不支持 schema.table.column 三级名称")
            return ast.ColumnRef(src_pos=name_tok.pos,
                                 table=name_tok.text, name=col_tok.text)
        return ast.ColumnRef(src_pos=name_tok.pos, table=None,
                             name=name_tok.text)

    def _parse_func_call(self, name_tok: Token) -> ast.Expr:
        self._eat_op("(")
        upper = name_tok.text.upper()
        distinct = False
        if self._kw("DISTINCT"):
            distinct = True
            self.pos_idx += 1
        args: list[ast.Expr] = []
        if self._op("*"):
            star = self._eat_op("*")
            raise CompileError(
                "unsupported_syntax",
                f"聚合函数 {name_tok.text}(*) 不在 scope 内（无聚合/窗口）",
                star.pos, self.src)
        if not self._op(")"):
            args.append(self._parse_expr())
            while self._op(","):
                self.pos_idx += 1
                args.append(self._parse_expr())
        self._eat_op(")")
        # 窗口函数形式 f(...) OVER (...)：直接拒绝
        if self._kw("OVER"):
            over = self._eat_kw("OVER")
            raise CompileError(
                "unsupported_syntax",
                "窗口函数 OVER 不在 scope 内（DISTINCT ON 改写由编译器内部生成）",
                over.pos, self.src)
        if upper in KNOWN_UNSUPPORTED_FUNCS:
            raise CompileError(
                "unsupported_syntax",
                f"函数 {name_tok.text}() 不在 scope 内",
                name_tok.pos, self.src)
        if upper not in ALLOWED_FUNCS:
            raise CompileError(
                "unsupported_syntax",
                f"未知函数 {name_tok.text}()：scope 仅显式允许 concat()",
                name_tok.pos, self.src)
        return ast.FuncCall(src_pos=name_tok.pos, name=upper,
                            args=args, distinct=distinct)

    def _parse_cast(self) -> ast.Expr:
        tok = self._eat_kw("CAST")
        self._eat_op("(")
        operand = self._parse_expr()
        self._eat_kw("AS")
        type_tok = self.cur
        if type_tok is None or type_tok.kind not in ("IDENT", "KEYWORD"):
            raise self._err("parse_error", "CAST 缺少目标类型")
        self.pos_idx += 1
        # 仅吃掉类型名本身；带精度/长度的类型如 varchar(10) 拒绝
        if self._op("("):
            raise self._err("unsupported_syntax",
                            "带长度/精度的类型转换不在 scope 内")
        upper_type = type_tok.text.upper()
        if upper_type not in ("INTEGER", "INT", "TEXT", "BOOLEAN", "BOOL"):
            raise CompileError(
                "unsupported_syntax",
                f"CAST 目标类型 {type_tok.text} 不在 scope（integer/text/boolean）",
                type_tok.pos, self.src)
        self._eat_op(")")
        return ast.CastExpr(src_pos=tok.pos, operand=operand,
                            type_name=upper_type)

    def _parse_case(self) -> ast.Expr:
        tok = self._eat_kw("CASE")
        operand = None
        if not self._kw("WHEN"):
            operand = self._parse_expr()
        whens: list[tuple[ast.Expr, ast.Expr]] = []
        while self._kw("WHEN"):
            self.pos_idx += 1
            cond = self._parse_expr()
            self._eat_kw("THEN")
            result = self._parse_expr()
            whens.append((cond, result))
        if not whens:
            raise self._err("parse_error", "CASE 至少需要一个 WHEN")
        default = None
        if self._kw("ELSE"):
            self.pos_idx += 1
            default = self._parse_expr()
        self._eat_kw("END")
        return ast.CaseExpr(src_pos=tok.pos, operand=operand,
                            whens=whens, default=default)

    # ---- 标识符工具 ------------------------------------------------------
    def _expect_ident(self) -> Token:
        t = self.cur
        if t is None or t.kind != "IDENT":
            raise self._err("parse_error", "期望标识符")
        self.pos_idx += 1
        return t

    def _expect_ident_name(self) -> str:
        return self._expect_ident().text.strip('"')


def parse(source: str) -> ast.Select:
    return Parser(source).parse_statement()
