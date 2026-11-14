"""``.dims`` 声明文件解析。

声明语法（严格受限）::

    declaration      := IDENT ":" dimension_product
    dimension_product := "1" | dimension_factor {" " dimension_factor}
    dimension_factor  := ("M" | "L" | "T" | "Theta") [ "^" exponent ]
    exponent          := signed-integer | "(" signed-integer "/" positive-integer ")"

只允许四个基础维数，不接受任意标识符；``1`` 表示无量纲。
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .ast_nodes import DeclStmt
from .diagnostics import DiagnosticBag, Severity
from .lexer import Token, TokenType, tokenize
from .parser import ParseError, _split_lines
from .sources import SourceText, Span

_BASE_INDEX = {"M": 0, "L": 1, "T": 2, "Theta": 3}
_ZERO = Fraction(0)


@dataclass
class _DimsParser:
    toks: list[Token]
    pos: int = 0

    @property
    def cur(self) -> Token:
        return self.toks[self.pos]

    def advance(self) -> Token:
        tok = self.cur
        self.pos += 1  # 末尾保留在 EOF 哨兵上
        return tok

    def expect(self, kind: TokenType, what: str) -> Token:
        if self.cur.type is not kind:
            raise ParseError(
                "E200",
                f"声明中期望 {what}，遇到 {self.cur.text!r}",
                self.cur.span,
            )
        return self.advance()

    def parse_exponent(self) -> tuple[Fraction, Span]:
        # 无括号：可选符号的整数字面量（如 T^-2）。
        if self.cur.type in (TokenType.NUMBER, TokenType.PLUS, TokenType.MINUS):
            sign = 1
            start = self.cur
            if self.cur.type in (TokenType.PLUS, TokenType.MINUS):
                if self.cur.type is TokenType.MINUS:
                    sign = -1
                self.advance()
            num = self.expect(TokenType.NUMBER, "整数字面量指数")
            return Fraction(sign * int(num.text)), Span(
                start.span.file,
                start.span.start_line,
                start.span.start_col,
                num.span.end_line,
                num.span.end_col,
                start.span.start_offset,
                num.span.end_offset,
            )
        lparen = self.expect(TokenType.LPAREN, "'(' 开始有理指数")
        sign = 1
        if self.cur.type in (TokenType.PLUS, TokenType.MINUS):
            if self.cur.type is TokenType.MINUS:
                sign = -1
            self.advance()
        num = self.expect(TokenType.NUMBER, "指数分子")
        numerator = int(num.text)
        denominator = 1
        if self.cur.type is TokenType.SLASH:
            self.advance()
            den = self.expect(TokenType.NUMBER, "指数分母")
            denominator = int(den.text)
            if denominator <= 0:
                raise ParseError(
                    "E202",
                    f"有理指数分母必须为正整数，得到 {denominator}",
                    den.span,
                )
        rparen = self.expect(TokenType.RPAREN, "')' 结束有理指数")
        return Fraction(sign * numerator, denominator), Span(
            lparen.span.file,
            lparen.span.start_line,
            lparen.span.start_col,
            rparen.span.end_line,
            rparen.span.end_col,
            lparen.span.start_offset,
            rparen.span.end_offset,
        )

    def parse_dimension_product(self) -> tuple[tuple[Fraction, Fraction, Fraction, Fraction], Span]:
        vec = [_ZERO, _ZERO, _ZERO, _ZERO]
        start_tok = self.cur
        if self.cur.type is TokenType.NUMBER and self.cur.text == "1":
            one = self.advance()
            if self.cur.type is not TokenType.EOF:
                raise ParseError(
                    "E203",
                    f"无量纲声明 '1' 后不能再出现因子 {self.cur.text!r}",
                    self.cur.span,
                )
            return (_ZERO, _ZERO, _ZERO, _ZERO), one.span

        end_span = start_tok.span
        while self.cur.type is not TokenType.EOF:
            base_tok = self.cur
            if base_tok.type is not TokenType.IDENT or base_tok.text not in _BASE_INDEX:
                raise ParseError(
                    "E201",
                    f"声明 RHS 只允许 M/L/T/Theta，遇到 {base_tok.text!r}",
                    base_tok.span,
                )
            self.advance()
            power = Fraction(1)
            if self.cur.type is TokenType.CARET:
                self.advance()
                power, _ = self.parse_exponent()
            vec[_BASE_INDEX[base_tok.text]] += power
            end_span = base_tok.span
        dim_span = Span(
            start_tok.span.file,
            start_tok.span.start_line,
            start_tok.span.start_col,
            end_span.end_line,
            end_span.end_col,
            start_tok.span.start_offset,
            end_span.end_offset,
        )
        return (vec[0], vec[1], vec[2], vec[3]), dim_span


def parse_dims_file(source: SourceText, diags: DiagnosticBag) -> list[DeclStmt]:
    tokens, lex_errors = tokenize(source)
    for err in lex_errors:
        diags.add("E001", err.message, err.span, Severity.PARSE_ERROR, source.snippet(err.span))

    declarations: list[DeclStmt] = []
    for line in _split_lines(tokens):
        parser = _DimsParser(line.tokens)
        try:
            name_tok = parser.expect(TokenType.IDENT, "变量名")
            parser.expect(TokenType.COLON, "':'")
            dimension, dim_span = parser.parse_dimension_product()
        except ParseError as exc:
            diags.add(
                exc.code,
                exc.message,
                exc.span,
                Severity.PARSE_ERROR,
                source.snippet(exc.span),
            )
            continue
        declarations.append(
            DeclStmt(
                name=name_tok.text,
                name_span=name_tok.span,
                span=Span(
                    name_tok.span.file,
                    name_tok.span.start_line,
                    name_tok.span.start_col,
                    dim_span.end_line,
                    dim_span.end_col,
                    name_tok.span.start_offset,
                    dim_span.end_offset,
                ),
                dimension=dimension,
                dimension_span=dim_span,
            )
        )
    return declarations
