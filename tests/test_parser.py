"""带位置词法/语法分析与受限语法拒绝的测试。"""

from __future__ import annotations

import unittest

from dimcheck.ast_nodes import Binary, Call, Number, Power, Unary, Var
from dimcheck.diagnostics import DiagnosticBag, Severity
from dimcheck.dims import parse_dims_file
from dimcheck.lexer import tokenize
from dimcheck.parser import parse_formula_file
from dimcheck.sources import SourceStore


class LexerTests(unittest.TestCase):
    def _lex(self, text: str):
        store = SourceStore()
        src = store.add("mem", text)
        return tokenize(src)

    def test_tokens_carry_spans_and_unicode_columns(self):
        store = SourceStore()
        # 注释含多字节字符；列按 Unicode 码点计。
        src = store.add("mem", "# 温度注释 θ\nx : a = b + 2\n")
        tokens, errors = tokenize(src)
        self.assertEqual(errors, [])
        ident_x = next(t for t in tokens if t.type.name == "IDENT" and t.text == "x")
        self.assertEqual((ident_x.span.start_line, ident_x.span.start_col), (2, 1))
        plus = next(t for t in tokens if t.text == "+")
        self.assertEqual((plus.span.start_line, plus.span.start_col), (2, 11))

    def test_bad_character_is_diagnostic_not_exception(self):
        _tokens, errors = self._lex("a = b @ c\n")
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].span.start_col, 7)


class FormulaParserTests(unittest.TestCase):
    def parse(self, text: str):
        store = SourceStore()
        src = store.add("mem.formula", text)
        diags = DiagnosticBag()
        stmts = parse_formula_file(src, diags)
        return stmts, diags, store

    def test_unary_minus_binds_looser_than_power(self):
        stmts, diags, _ = self.parse("l: y = -x^2\n")
        self.assertEqual(diags.items, [])
        rhs = stmts[0].rhs
        self.assertIsInstance(rhs, Unary)
        self.assertEqual(rhs.op, "-")
        self.assertIsInstance(rhs.operand, Power)
        self.assertEqual(rhs.operand.exponent, 2)

    def test_precedence_and_associativity(self):
        stmts, diags, _ = self.parse("l: y = a + b * c - d / e\n")
        self.assertEqual(diags.items, [])
        rhs = stmts[0].rhs
        self.assertIsInstance(rhs, Binary)
        self.assertEqual(rhs.op, "-")
        self.assertIsInstance(rhs.left, Binary)
        self.assertEqual(rhs.left.op, "+")
        self.assertIsInstance(rhs.left.right, Binary)
        self.assertEqual(rhs.left.right.op, "*")
        self.assertIsInstance(rhs.right, Binary)
        self.assertEqual(rhs.right.op, "/")

    def test_rational_parenthesized_exponent_and_decimal(self):
        stmts, diags, _ = self.parse("l: y = x^(1/2) + 0.5\n")
        self.assertEqual(diags.items, [])
        power = stmts[0].rhs.left
        self.assertIsInstance(power, Power)
        self.assertEqual(str(power.exponent), "1/2")
        self.assertIsInstance(stmts[0].rhs.right, Number)

    def test_negative_rational_exponent(self):
        stmts, diags, _ = self.parse("l: y = x^(-2/3)\n")
        self.assertEqual(diags.items, [])
        self.assertEqual(str(stmts[0].rhs.exponent), "-2/3")

    def test_variable_exponent_rejected_with_position(self):
        stmts, diags, store = self.parse("l: y = x^time\n")
        self.assertEqual(stmts, [])
        self.assertTrue(diags.has_parse_error)
        diag = diags.items[0]
        self.assertEqual(diag.code, "E101")
        self.assertEqual(diag.span.start_line, 1)
        self.assertEqual(diag.span.start_col, 10)  # 指向 time（l: y = x^ 之后）
        self.assertIn("time", diag.snippet)

    def test_unknown_function_is_semantic_not_parse_crash(self):
        stmts, diags, _ = self.parse("l: y = mystery(a)\n")
        # 语法本身成立；语义拒绝发生在编译器，这里先保证能解析出 Call。
        self.assertEqual(diags.items, [])
        self.assertIsInstance(stmts[0].rhs, Call)
        self.assertEqual(stmts[0].rhs.name, "mystery")

    def test_known_function_call_node(self):
        stmts, diags, _ = self.parse("l: y = sqrt(a) + sin(b)\n")
        self.assertEqual(diags.items, [])
        self.assertIsInstance(stmts[0].rhs.left, Call)
        self.assertEqual(stmts[0].rhs.left.name, "sqrt")

    def test_missing_equal_rejected(self):
        stmts, diags, _ = self.parse("l: y + a\n")
        self.assertTrue(diags.has_parse_error)
        self.assertEqual(diags.items[0].code, "E103")

    def test_duplicate_label_rejected_but_other_line_kept(self):
        stmts, diags, _ = self.parse("l: y = a\nl: z = b\nm: w = c\n")
        self.assertTrue(diags.has_parse_error)
        self.assertEqual([s.label for s in stmts], ["l", "l", "m"])
        self.assertEqual(diags.items[0].code, "E110")

    def test_span_half_open_and_snippet(self):
        store = SourceStore()
        src = store.add("mem.formula", "l: y = a + b\n")
        stmts = parse_formula_file(src, DiagnosticBag())
        span = stmts[0].span
        self.assertEqual(src.text[span.start_offset:span.end_offset], "l: y = a + b")
        # 表达式 RHS 的 span 从 y 开始（半开，不含行末换行）。
        rhs = stmts[0].rhs.span
        self.assertEqual(src.text[rhs.start_offset:rhs.end_offset], "a + b")


class DimsParserTests(unittest.TestCase):
    def parse(self, text: str):
        store = SourceStore()
        src = store.add("mem.dims", text)
        diags = DiagnosticBag()
        return parse_dims_file(src, diags), diags

    def test_dimension_product_with_rational_power(self):
        decls, diags = self.parse("energy : M L^2 T^-2\nroot : L^(1/2)\nplain : Theta\n")
        self.assertEqual(diags.items, [])
        self.assertEqual(decls[0].dimension[1], 2)
        self.assertEqual(decls[0].dimension[2], -2)
        self.assertEqual(str(decls[1].dimension[1]), "1/2")
        self.assertEqual(decls[2].dimension[3], 1)

    def test_dimensionless_one(self):
        decls, diags = self.parse("angle : 1\n")
        self.assertEqual(diags.items, [])
        self.assertTrue(all(c == 0 for c in decls[0].dimension))

    def test_bad_base_rejected_with_position(self):
        decls, diags = self.parse("x : M N\n")
        self.assertEqual(decls, [])
        self.assertEqual(diags.items[0].code, "E201")
        self.assertEqual(diags.items[0].span.start_col, 7)

    def test_repeated_base_accumulates(self):
        decls, diags = self.parse("x : L L^2\n")
        self.assertEqual(diags.items, [])
        self.assertEqual(decls[0].dimension[1], 3)

    def test_non_positive_denominator_rejected(self):
        decls, diags = self.parse("x : L^(1/0)\n")
        self.assertEqual(diags.items[0].code, "E202")


if __name__ == "__main__":
    unittest.main()
