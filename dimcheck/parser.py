"""公式文件的递归下降解析器（不使用 eval）。

受限语法（优先级从低到高）::

    additive       := multiplicative (("+" | "-") multiplicative)*
    multiplicative := unary (("*" | "/") unary)*
    unary          := ("+" | "-") unary | power
    power          := primary ("^" exponent)?
    exponent       := NUMBER | "(" ["+"|"-"] NUMBER ["/" NUMBER] ")"
    primary        := NUMBER | IDENT ["(" expr ("," expr)* ")"] | "(" additive ")"
    statement      := IDENT ":" additive "=" additive

一元负号低于幂：``-x^2`` 解析为 ``-(x^2)``。
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .ast_nodes import Binary, Call, FormulaStmt, Node, Number, Power, Unary, Var
from .diagnostics import DiagnosticBag, Severity
from .lexer import Token, TokenType, tokenize
from .sources import SourceText, Span


class ParseError(Exception):
    def __init__(self, code: str, message: str, span: Span) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.span = span


@dataclass
class _Line:
    tokens: list[Token]
    start_index: int


def _split_lines(tokens: list[Token]) -> list[_Line]:
    """切成逻辑行；每行末尾补一个 EOF 哨兵，解析器据此判断行结束。"""
    lines: list[_Line] = []
    current: list[Token] = []
    eof = next(t for t in reversed(tokens) if t.type is TokenType.EOF)
    for idx, tok in enumerate(tokens):
        if tok.type is TokenType.EOF:
            if current:
                lines.append(_Line(current + [eof], idx - len(current)))
            break
        if tok.type is TokenType.NEWLINE:
            if current:
                lines.append(_Line(current + [eof], idx - len(current)))
            current = []
        else:
            current.append(tok)
    return lines


class _ExprParser:
    def __init__(self, tokens: list[Token]) -> None:
        self.toks = tokens
        self.pos = 0

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
                "E103",
                f"期望 {what}，但遇到 {self.cur.text!r}",
                self.cur.span,
            )
        return self.advance()

    def parse_additive(self) -> Node:
        node = self.parse_multiplicative()
        while self.cur.type in (TokenType.PLUS, TokenType.MINUS):
            op_tok = self.advance()
            right = self.parse_multiplicative()
            node = Binary(_merge(node.span, right.span), op_tok.text, node, right)
        return node

    def parse_multiplicative(self) -> Node:
        node = self.parse_unary()
        while self.cur.type in (TokenType.STAR, TokenType.SLASH):
            op_tok = self.advance()
            right = self.parse_unary()
            node = Binary(_merge(node.span, right.span), op_tok.text, node, right)
        return node

    def parse_unary(self) -> Node:
        if self.cur.type in (TokenType.PLUS, TokenType.MINUS):
            op_tok = self.advance()
            operand = self.parse_unary()
            return Unary(_merge(op_tok.span, operand.span), op_tok.text, operand)
        return self.parse_power()

    def parse_power(self) -> Node:
        base = self.parse_primary()
        if self.cur.type is not TokenType.CARET:
            return base
        caret = self.advance()
        exponent, exp_span = self._parse_exponent()
        return Power(_merge(base.span, exp_span), base, exponent, exp_span)

    def _parse_exponent(self) -> tuple[Fraction, Span]:
        # 无括号：可选符号的整数字面量（如 x^-2）。
        if self.cur.type in (TokenType.NUMBER, TokenType.PLUS, TokenType.MINUS):
            sign = 1
            start = self.cur
            if self.cur.type in (TokenType.PLUS, TokenType.MINUS):
                if self.cur.type is TokenType.MINUS:
                    sign = -1
                self.advance()
            tok = self.expect(TokenType.NUMBER, "整数字面量指数")
            return Fraction(sign * int(tok.text)), _merge(start.span, tok.span)
        # 括号内的（有符号）整数或整数/正整数
        if self.cur.type is TokenType.LPAREN:
            lparen = self.advance()
            sign = 1
            if self.cur.type in (TokenType.PLUS, TokenType.MINUS):
                if self.cur.type is TokenType.MINUS:
                    sign = -1
                self.advance()
            num_tok = self.expect(TokenType.NUMBER, "指数分子（整数）")
            numerator = int(num_tok.text)
            denominator = 1
            if self.cur.type is TokenType.SLASH:
                self.advance()
                den_tok = self.expect(TokenType.NUMBER, "指数分母（正整数）")
                denominator = int(den_tok.text)
                if denominator <= 0:
                    raise ParseError(
                        "E102",
                        f"有理指数分母必须为正整数，得到 {denominator}",
                        den_tok.span,
                    )
            rparen = self.expect(TokenType.RPAREN, "')' 结束有理指数")
            return Fraction(sign * numerator, denominator), _merge(lparen.span, rparen.span)
        raise ParseError(
            "E101",
            f"幂指数只能是整数字面量或括号内有理常量，遇到 {self.cur.text!r}",
            self.cur.span,
        )

    def parse_primary(self) -> Node:
        tok = self.cur
        if tok.type is TokenType.NUMBER:
            self.advance()
            return Number(tok.span, Fraction(tok.text))
        if tok.type is TokenType.IDENT:
            self.advance()
            if self.cur.type is TokenType.LPAREN:
                self.advance()
                args: list[Node] = []
                if self.cur.type is not TokenType.RPAREN:
                    args.append(self.parse_additive())
                    while self.cur.type is TokenType.COMMA:
                        self.advance()
                        args.append(self.parse_additive())
                rparen = self.expect(TokenType.RPAREN, "')' 结束函数调用")
                return Call(_merge(tok.span, rparen.span), tok.text, tuple(args))
            return Var(tok.span, tok.text)
        if tok.type is TokenType.LPAREN:
            self.advance()
            inner = self.parse_additive()
            rparen = self.expect(TokenType.RPAREN, "')'")
            return inner
        raise ParseError(
            "E100",
            f"期望数字、标识符或括号表达式，遇到 {tok.text!r}",
            tok.span,
        )


def _merge(a: Span, b: Span) -> Span:
    """递归下降中 a 恒为左侧节点、b 恒为右侧节点，故 a 先开始、b 后结束。"""
    return Span(
        a.file,
        a.start_line,
        a.start_col,
        b.end_line,
        b.end_col,
        a.start_offset,
        b.end_offset,
    )


def parse_formula_file(source: SourceText, diags: DiagnosticBag) -> list[FormulaStmt]:
    tokens, lex_errors = tokenize(source)
    for err in lex_errors:
        diags.add("E001", err.message, err.span, Severity.PARSE_ERROR, source.snippet(err.span))

    statements: list[FormulaStmt] = []
    seen_labels: dict[str, Span] = {}
    for line in _split_lines(tokens):
        parser = _ExprParser(line.tokens)
        try:
            label_tok = parser.expect(TokenType.IDENT, "公式标签")
            parser.expect(TokenType.COLON, "':'")
            lhs = parser.parse_additive()
            parser.expect(TokenType.EQUAL, "'='")
            rhs = parser.parse_additive()
            if parser.cur.type is not TokenType.EOF:
                raise ParseError(
                    "E104",
                    f"公式末尾有多余记号 {parser.cur.text!r}",
                    parser.cur.span,
                )
        except ParseError as exc:
            diags.add(
                exc.code,
                exc.message,
                exc.span,
                Severity.PARSE_ERROR,
                source.snippet(exc.span),
            )
            continue

        if label_tok.text in seen_labels:
            first = seen_labels[label_tok.text]
            diags.add(
                "E110",
                f"公式标签 {label_tok.text!r} 重复（首次出现于 {first}）",
                label_tok.span,
                Severity.PARSE_ERROR,
                source.snippet(label_tok.span),
            )
        seen_labels.setdefault(label_tok.text, label_tok.span)
        statements.append(
            FormulaStmt(
                label=label_tok.text,
                label_span=label_tok.span,
                lhs=lhs,
                rhs=rhs,
                span=_merge(label_tok.span, rhs.span),
            )
        )
    return statements
