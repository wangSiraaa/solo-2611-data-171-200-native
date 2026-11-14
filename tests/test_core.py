"""核心逻辑单元测试：TSV 解析、类型转换、预演/提交/撤销（不涉及 curses）。"""

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tuigrid.input import PASTE_END, PASTE_START, InputParser  # noqa: E402
from tuigrid.model import COLS, ROWS, GridModel  # noqa: E402
from tuigrid.tsv import parse_tsv  # noqa: E402
from tuigrid.values import Column, ValueError_, convert_field  # noqa: E402

FIX = ROOT / "fixtures"


class TsvTests(unittest.TestCase):
    def test_quoted_newline_tab_and_doubled_quote(self):
        result = parse_tsv((FIX / "clips" / "valid.tsv").read_text(encoding="utf-8"))
        self.assertEqual(result.errors, [])
        self.assertEqual(len(result.records), 2)
        first, second = result.records
        self.assertEqual(len(first), 6)
        self.assertEqual(first[0].value, "Name 名称\nnewline")
        self.assertEqual(first[4].value, "false")
        self.assertEqual(first[5].value, 'quote "and" tab\there')
        self.assertEqual(second[0].value, "Second")
        self.assertEqual(second[5].value, "\\N")
        # 逻辑定位：第二行第一列
        self.assertEqual((second[0].source_row, second[0].source_col), (2, 1))

    def test_trailing_newline_no_extra_record(self):
        self.assertEqual(parse_tsv("a\tb\n").records and 1, 1)
        result = parse_tsv("a\tb\n")
        self.assertEqual(len(result.records), 1)
        self.assertEqual([f.value for f in result.records[0]], ["a", "b"])
        # 两个换行 => 中间一条空记录；空输入 => 零记录
        self.assertEqual(len(parse_tsv("a\n\n").records), 2)
        self.assertEqual(parse_tsv("").records, [])
        # 仅换行 => 一条含单个空字段的记录
        one = parse_tsv("\n")
        self.assertEqual(len(one.records), 1)
        self.assertEqual(one.records[0][0].value, "")

    def test_ragged_rectangle(self):
        result = parse_tsv((FIX / "clips" / "ragged.tsv").read_text(encoding="utf-8"))
        widths = {len(r) for r in result.records}
        self.assertEqual(widths, {2, 1})
        self.assertEqual(result.errors, [])  # 缺列属于矩形恢复问题，不是语法错误

    def test_unclosed_quote(self):
        result = parse_tsv((FIX / "clips" / "unclosed.tsv").read_text(encoding="utf-8"))
        self.assertTrue(result.unterminated)
        self.assertEqual(len(result.errors), 1)
        err = result.errors[0]
        self.assertIn("未闭合", err.reason)
        self.assertEqual((err.source_row, err.source_col), (1, 1))
        # 仍还原出已输入内容
        self.assertEqual(result.records[0][0].value, "unclosed\tfield\n")

    def test_stray_quote_and_post_close_garbage(self):
        r1 = parse_tsv('ab"c\n')
        self.assertTrue(any("裸双引号" in e.reason for e in r1.errors))
        r2 = parse_tsv('"ok"x\tb\n')
        self.assertTrue(any("闭合双引号后" in e.reason for e in r2.errors))
        self.assertEqual(r2.records[0][0].value, "okx")

    def test_crlf_outside_quote_but_literal_inside(self):
        result = parse_tsv('"a\rb"\tb\r\nx\ty\r\n')
        self.assertEqual(result.errors, [])
        self.assertEqual(result.records[0][0].value, "a\rb")
        self.assertEqual(result.records[0][1].value, "b")
        self.assertEqual([f.value for f in result.records[1]], ["x", "y"])


class ValueTests(unittest.TestCase):
    def setUp(self):
        self.text = Column("note", "text", True)
        self.text_nn = Column("name", "text", False)
        self.integer = Column("qty", "integer", False)
        self.decimal = Column("price", "decimal", False)
        self.day = Column("day", "date", False)
        self.boolean = Column("active", "boolean", False)

    def test_empty_string_vs_null_vs_zero_vs_false(self):
        self.assertEqual(convert_field("", self.text), "")
        self.assertIsNone(convert_field("\\N", self.text))
        self.assertEqual(convert_field("0", self.integer), 0)
        self.assertIs(convert_field("false", self.boolean), False)
        # 类型不被 Python 真假值吞掉
        self.assertNotEqual(convert_field("0", self.integer), convert_field("", self.text))
        self.assertIsNotNone(convert_field("false", self.boolean))

    def test_empty_nontext_invalid(self):
        for col in (self.integer, self.decimal, self.day, self.boolean):
            with self.assertRaises(ValueError_):
                convert_field("", col)

    def test_null_marker_rules(self):
        with self.assertRaises(ValueError_):
            convert_field("\\N", self.integer)  # 不可空列
        self.assertIsNone(convert_field("\\N", self.text))
        # 必须逐字符精确匹配：带空格或小写变体都只是普通文本，不是 null
        self.assertEqual(convert_field(" \\N", self.text), " \\N")
        # 可空文本列里，普通字符串 "\n" 就是数据，不等于 null 标记
        self.assertEqual(convert_field("\\n", self.text), "\\n")
        with self.assertRaises(ValueError_):
            convert_field("\\N", self.text_nn)  # 不可空文本列同样拒绝 null

    def test_integer_strict(self):
        self.assertEqual(convert_field("-7", self.integer), -7)
        self.assertEqual(convert_field("+012", self.integer), 12)
        for bad in ("1.0", "0x10", "1e3", "true", " 1", "1 "):
            with self.assertRaises(ValueError_):
                convert_field(bad, self.integer)

    def test_decimal_canonical_without_float(self):
        self.assertEqual(convert_field("2.0", self.decimal), "2.00")
        with self.assertRaises(ValueError_):
            convert_field("1.234", self.decimal)  # 超过两位小数
        self.assertEqual(convert_field("0.00", self.decimal), "0.00")
        self.assertEqual(convert_field("-0.0", self.decimal), "0.00")
        self.assertEqual(convert_field("12", self.decimal), "12.00")
        self.assertEqual(convert_field("-0.5", self.decimal), "-0.50")
        for bad in ("1e2", ".5", "1.", "abc", "1.2.3"):
            with self.assertRaises(ValueError_):
                convert_field(bad, self.decimal)

    def test_boolean_strict(self):
        self.assertIs(convert_field("true", self.boolean), True)
        self.assertIs(convert_field("false", self.boolean), False)
        for bad in ("TRUE", "yes", "0", "1", "False "):
            with self.assertRaises(ValueError_):
                convert_field(bad, self.boolean)

    def test_date_real_calendar(self):
        self.assertEqual(convert_field("2026-11-14", self.day), "2026-11-14")
        for bad in ("2026-02-30", "2026-13-01", "2026-1-1", "20260211", "2026-02-29"):
            with self.assertRaises(ValueError_):
                convert_field(bad, self.day)
        # 2024 是闰年
        self.assertEqual(convert_field("2024-02-29", self.day), "2024-02-29")


class InputParserTests(unittest.TestCase):
    def test_paste_is_one_event_and_inner_newline_not_enter(self):
        p = InputParser()
        events = p.feed(PASTE_START + b'a\tb\n"x\ny"\n' + PASTE_END)
        kinds = [(e.kind, e.name) for e in events]
        self.assertEqual(kinds, [("paste", "")])
        self.assertEqual(events[0].text, 'a\tb\n"x\ny"\n')

    def test_markers_and_utf8_split_across_feeds(self):
        payload = "合\ntab\there".encode("utf-8")
        stream = PASTE_START + payload + PASTE_END
        p = InputParser()
        events = []
        # 按 1 字节切碎，模拟极端的 read 拆分
        for i in range(len(stream)):
            events.extend(p.feed(stream[i : i + 1]))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "paste")
        self.assertEqual(events[0].text, "合\ntab\there")

    def test_marker_split_at_every_position(self):
        stream = PASTE_START + b"abc" + PASTE_END
        for cut in range(1, len(stream)):
            p = InputParser()
            ev = p.feed(stream[:cut]) + p.feed(stream[cut:])
            self.assertEqual(len(ev), 1, f"cut={cut}")
            self.assertEqual(ev[0].kind, "paste")
            self.assertEqual(ev[0].text, "abc")

    def test_arrows_and_enter_and_ctrl_z(self):
        p = InputParser()
        events = p.feed(b"\x1b[A\x1b[B\x1b[C\x1b[D\r\n\x1a")
        names = [e.name for e in events]
        self.assertEqual(names, ["up", "down", "right", "left", "enter", "enter", "ctrl_z"])

    def test_lone_escape_via_idle(self):
        p = InputParser()
        self.assertEqual(p.feed(b"\x1b"), [])
        events = p.idle()
        self.assertEqual([e.name for e in events], ["esc"])
        # SS3 方向键也识别
        self.assertEqual([e.name for e in p.feed(b"\x1bOC")], ["right"])

    def test_f2_and_bad_utf8(self):
        p = InputParser()
        self.assertEqual([e.name for e in p.feed(b"\x1b[12~")], ["f2"])
        bad = p.feed(PASTE_START + b"\xff\xfe" + PASTE_END)
        self.assertEqual(bad[0].kind, "paste_error")


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.model = GridModel.load(FIX / "grid.json", FIX / "schema.json")
        self.original = copy.deepcopy(self.model.cells)

    def test_valid_preview_commit_and_undo_multiline(self):
        payload = (FIX / "clips" / "valid.tsv").read_text(encoding="utf-8")
        # README 要求：从 name 列（下标 1）开始，且至少剩两行 -> 选 R18（下标 17）
        preview = self.model.build_preview(payload, 17, 1)
        self.assertTrue(preview.ok, [e.reason for e in preview.errors])
        self.assertEqual(preview.height, 2)
        self.assertEqual(preview.width, 6)
        # 预演不改原表
        self.assertEqual(self.model.cells, self.original)

        n = self.model.commit(preview)
        self.assertEqual(n, 12)
        # 多行文本、布尔 false、整数 0、decimal 规范化、\N 全部落对类型
        r18 = self.model.cells[17]
        self.assertEqual(r18[1], "Name 名称\nnewline")
        self.assertIsInstance(r18[1], str)
        self.assertEqual(r18[2], 0)
        self.assertIsInstance(r18[2], int)
        self.assertEqual(r18[3], "0.00")
        self.assertIs(r18[5], False)
        self.assertEqual(r18[6], 'quote "and" tab\there')
        r19 = self.model.cells[18]
        self.assertIsNone(r19[6])  # \N -> None
        self.assertEqual(r19[2], 12)
        self.assertEqual(r19[3], "3.40")

        # 单层撤销：整块恢复（含多行文本）
        self.assertTrue(self.model.undo())
        self.assertEqual(self.model.cells, self.original)
        self.assertFalse(self.model.undo())  # 只有一层

    def test_invalid_clip_locates_all_errors_and_blocks_commit(self):
        payload = (FIX / "clips" / "invalid.tsv").read_text(encoding="utf-8")
        preview = self.model.build_preview(payload, 17, 1)
        self.assertFalse(preview.ok)
        # 第二行（源行 2）四个类型错误：qty/false、price/1.234、day/02-30、active/yes
        self.assertEqual(
            [(e.source_row, e.source_col) for e in preview.errors],
            [(2, 2), (2, 3), (2, 4), (2, 5)],
        )
        # 源行 2 落在锚点 R18 的下一行 R19（0 基下标 18），目标列依次为 qty..active（2..5）
        self.assertTrue(all(e.target_row == 18 for e in preview.errors))
        self.assertEqual([e.target_col for e in preview.errors], [2, 3, 4, 5])
        self.assertTrue(all(e.target_row_id == "R19" for e in preview.errors))
        with self.assertRaises(RuntimeError):
            self.model.commit(preview)
        # 失败预演后网格不变、撤销记录不受影响
        self.assertEqual(self.model.cells, self.original)
        self.assertFalse(self.model.can_undo())

    def test_failed_preview_does_not_touch_undo_record(self):
        good = (FIX / "clips" / "valid.tsv").read_text(encoding="utf-8")
        p1 = self.model.build_preview(good, 17, 1)
        self.model.commit(p1)
        self.assertTrue(self.model.can_undo())
        bad = self.model.build_preview(
            (FIX / "clips" / "invalid.tsv").read_text("utf-8"), 17, 1
        )
        with self.assertRaises(RuntimeError):
            self.model.commit(bad)
        # 撤销记录仍是成功那次的快照
        self.assertTrue(self.model.can_undo())
        self.model.undo()
        self.assertEqual(self.model.cells, self.original)

    def test_ragged_and_unclosed_preview_errors(self):
        ragged = self.model.build_preview(
            (FIX / "clips" / "ragged.tsv").read_text("utf-8"), 0, 0
        )
        self.assertTrue(any("缺列" in e.reason for e in ragged.errors))
        self.assertFalse(ragged.ok)

        unclosed = self.model.build_preview(
            (FIX / "clips" / "unclosed.tsv").read_text("utf-8"), 0, 0
        )
        self.assertTrue(any("未闭合" in e.reason for e in unclosed.errors))
        self.assertFalse(unclosed.ok)
        self.assertEqual(self.model.cells, self.original)

    def test_out_of_bounds(self):
        # 锚点在最后一行，两行矩形放不下
        preview = self.model.build_preview(
            (FIX / "clips" / "valid.tsv").read_text("utf-8"), ROWS - 1, 1
        )
        self.assertFalse(preview.fits)
        self.assertTrue(any(e.target_row is None for e in preview.errors))
        # 横向越界：从 group 列（下标 7）粘贴 6 列
        side = self.model.build_preview(
            (FIX / "clips" / "valid.tsv").read_text("utf-8"), 0, COLS - 1
        )
        self.assertFalse(side.fits)
        self.assertTrue(any(e.target_col is None for e in side.errors))

    def test_export_roundtrip_preserves_types_and_row_ids(self):
        good = (FIX / "clips" / "valid.tsv").read_text("utf-8")
        self.model.commit(self.model.build_preview(good, 17, 1))
        dest = ROOT / "artifacts" / "unit_export.json"
        self.model.export_json(dest)
        raw = json.loads(dest.read_text(encoding="utf-8"))
        self.assertEqual([r["row_id"] for r in raw], self.model.row_ids)
        r18 = raw[17]["cells"]
        self.assertEqual(r18[1], "Name 名称\nnewline")
        self.assertEqual(r18[2], 0)
        self.assertEqual(r18[3], "0.00")
        self.assertIs(r18[5], False)
        self.assertIsNone(raw[18]["cells"][6])
        # 未改动行与原文件完全一致
        orig = json.loads((FIX / "grid.json").read_text("utf-8"))
        self.assertEqual(raw[0], orig[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
