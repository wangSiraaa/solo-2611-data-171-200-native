"""手写词法分析器。

设计要点：
- 每个 Token 携带源 SQL 的原始偏移、行、列，保证不支持语法可定位到原位置；
- 字符串字面量（含 '' 转义、字面文本里的 '$1'）整体成为一个 STRING token，
  参数化逻辑永远不会进入字符串内部；
- $1..$n 识别为 PARAM token；
- 只识别整数；浮点等数值直接报不支持；
- ~（正则操作符）照常切词，由语法分析给出 unsupported_syntax 拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import CompileError, Position

KEYWORDS = frozenset({
    "WITH", "AS", "SELECT", "DISTINCT", "ON", "FROM", "WHERE", "ORDER",
    "BY", "ASC", "DESC", "NULLS", "FIRST", "LAST", "AND", "OR", "NOT",
    "IS", "IN", "LIKE", "ILIKE", "BETWEEN", "TRUE", "FALSE", "NULL",
    "CAST", "CASE", "WHEN", "THEN", "ELSE", "END", "RECURSIVE", "GROUP",
    "HAVING", "LIMIT", "OFFSET", "JOIN", "INNER", "LEFT", "RIGHT",
    "FULL", "OUTER", "CROSS", "UNION", "INTERSECT", "EXCEPT", "ALL",
    # DDL 解析复用同一词法分析器时需要识别的关键字
    "CREATE", "TABLE", "PRIMARY", "KEY", "CHECK", "UNIQUE", "DEFAULT",
    "REFERENCES", "FOREIGN", "CONSTRAINT", "OVER",
})

# 多字符操作符按长度优先匹配
MULTI_OPS = ("<=", ">=", "<>", "!=", "::", "||", "->", "=>")
SINGLE_OPS = set("()[].,;+-*/%=<>~")


@dataclass(frozen=True)
class Token:
    kind: str          # KEYWORD / IDENT / STRING / NUMBER / PARAM / OP
    text: str          # 原样文本（字符串保留外层引号）
    pos: Position
    end_pos: Position  # 末字符之后的位置


class Lexer:
    def __init__(self, source: str):
        self.src = source
        self.n = len(source)
        self.i = 0
        self.line = 1
        self.col = 1
        self.tokens: list[Token] = []

    # ---- 位置维护 -------------------------------------------------------
    def _pos(self) -> Position:
        return Position(self.i, self.line, self.col)

    def _advance(self, step: int = 1) -> None:
        for _ in range(step):
            if self.i < self.n and self.src[self.i] == "\n":
                self.line += 1
                self.col = 1
            else:
                self.col += 1
            self.i += 1

    # ---- 主入口 ---------------------------------------------------------
    def tokenize(self) -> list[Token]:
        while self.i < self.n:
            ch = self.src[self.i]
            if ch in " \t\r\n":
                self._advance()
            elif ch == "-" and self._peek(1) == "-":
                self._skip_line_comment()
            elif ch == "/" and self._peek(1) == "*":
                self._skip_block_comment()
            elif ch == "'":
                self._read_string()
            elif ch == '"':
                self._read_quoted_ident()
            elif ch.isalpha() or ch == "_":
                self._read_word()
            elif ch.isdigit():
                self._read_number()
            elif ch == "$":
                self._read_param()
            else:
                self._read_operator()
        return self.tokens

    def _peek(self, offset: int) -> str:
        j = self.i + offset
        return self.src[j] if j < self.n else ""

    def _skip_line_comment(self) -> None:
        while self.i < self.n and self.src[self.i] != "\n":
            self._advance()

    def _skip_block_comment(self) -> None:
        start = self._pos()
        depth = 0
        while self.i < self.n:
            if self.src[self.i] == "/" and self._peek(1) == "*":
                depth += 1
                self._advance(2)
            elif self.src[self.i] == "*" and self._peek(1) == "/":
                depth -= 1
                self._advance(2)
                if depth == 0:
                    return
            else:
                self._advance()
        raise CompileError("parse_error", "块注释未闭合", start, self.src)

    def _read_string(self) -> None:
        start = self._pos()
        self._advance()  # 开引号
        while True:
            if self.i >= self.n:
                raise CompileError("parse_error", "字符串字面量未闭合",
                                   start, self.src)
            if self.src[self.i] == "'":
                if self._peek(1) == "'":  # 转义的单引号
                    self._advance(2)
                    continue
                self._advance()  # 闭引号
                break
            self._advance()
        text = self.src[start.offset:self.i]
        self.tokens.append(Token("STRING", text, start, self._pos()))

    def _read_quoted_ident(self) -> None:
        start = self._pos()
        self._advance()
        buf = ['"']
        while True:
            if self.i >= self.n:
                raise CompileError("parse_error", "引号标识符未闭合",
                                   start, self.src)
            if self.src[self.i] == '"':
                if self._peek(1) == '"':
                    buf.append('""')
                    self._advance(2)
                    continue
                buf.append('"')
                self._advance()
                break
            buf.append(self.src[self.i])
            self._advance()
        self.tokens.append(Token("IDENT", "".join(buf), start, self._pos()))

    def _read_word(self) -> None:
        start = self._pos()
        j = self.i
        while j < self.n and (self.src[j].isalnum() or self.src[j] == "_"):
            j += 1
        text = self.src[self.i:j]
        self._advance(j - self.i)
        upper = text.upper()
        kind = "KEYWORD" if upper in KEYWORDS else "IDENT"
        self.tokens.append(Token(kind, text if kind == "IDENT" else upper,
                                 start, self._pos()))

    def _read_number(self) -> None:
        start = self._pos()
        j = self.i
        saw_dot = False
        while j < self.n and (self.src[j].isdigit() or self.src[j] == "."):
            if self.src[j] == ".":
                if saw_dot:
                    break
                saw_dot = True
            j += 1
        text = self.src[self.i:j]
        if saw_dot:
            raise CompileError(
                "unsupported_syntax",
                f"数值 {text} 含小数：scope 仅支持整数类型，浮点/numeric 被拒绝",
                start, self.src)
        self._advance(j - self.i)
        self.tokens.append(Token("NUMBER", text, start, self._pos()))

    def _read_param(self) -> None:
        start = self._pos()
        j = self.i + 1
        if j >= self.n or not self.src[j].isdigit():
            raise CompileError("parse_error",
                               "'$' 后必须跟参数编号（$1..$n）",
                               start, self.src)
        while j < self.n and self.src[j].isdigit():
            j += 1
        text = self.src[self.i:j]
        self._advance(j - self.i)
        self.tokens.append(Token("PARAM", text, start, self._pos()))

    def _read_operator(self) -> None:
        start = self._pos()
        two = self.src[self.i:self.i + 2]
        if two in MULTI_OPS:
            self._advance(2)
            self.tokens.append(Token("OP", two, start, self._pos()))
            return
        ch = self.src[self.i]
        if ch in SINGLE_OPS:
            self._advance()
            self.tokens.append(Token("OP", ch, start, self._pos()))
            return
        raise CompileError("unsupported_syntax",
                           f"无法识别的符号 {ch!r}", start, self.src)


def tokenize(source: str) -> list[Token]:
    return Lexer(source).tokenize()
