"""structedit 冒烟测试：边界语义与范围外构造的拒绝。

直接运行：.venv/bin/python tests/smoke_test.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openpyxl import Workbook, load_workbook

from structedit.engine import run_edit
from structedit.formula import UnsupportedConstruct

TMP = Path(tempfile.mkdtemp(prefix="structedit_test_"))


def make_wb(formulas: dict[str, dict[str, str]], names: dict[str, str] | None = None,
            local_names: dict[str, dict[str, str]] | None = None) -> Path:
    wb = Workbook()
    first = True
    for sheet, cells in formulas.items():
        ws = wb.active if first else wb.create_sheet()
        first = False
        ws.title = sheet
        for coord, value in cells.items():
            ws[coord] = value
    for name, ref in (names or {}).items():
        wb.defined_names[name] = __import__(
            "openpyxl.workbook.defined_name", fromlist=["DefinedName"]
        ).DefinedName(name, attr_text=ref)
    for sheet, sheet_names in (local_names or {}).items():
        from openpyxl.workbook.defined_name import DefinedName

        for name, ref in sheet_names.items():
            wb[sheet].defined_names[name] = DefinedName(name, attr_text=ref)
    path = TMP / f"wb{len(list(TMP.iterdir()))}.xlsx"
    wb.save(path)
    return path


def edit_and_read(src: Path, ops: list[dict]) -> dict[str, dict[str, str]]:
    out = src.with_name(src.stem + "_out.xlsx")
    run_edit(src, ops, out)
    wb = load_workbook(out)
    result = {}
    for ws in wb.worksheets:
        result[ws.title] = {
            f"{c.column_letter}{c.row}": c.value
            for row in ws.iter_rows()
            for c in row
            if c.value is not None
        }
    return result


def expect(actual, expected, label):
    assert actual == expected, f"{label}\n  预期: {expected!r}\n  实际: {actual!r}"
    print(f"  ok: {label}")


def expect_reject(src: Path, ops: list[dict], label: str):
    try:
        run_edit(src, ops, src.with_name(src.stem + "_rej.xlsx"))
    except UnsupportedConstruct as exc:
        print(f"  ok: {label} -> 拒绝: {exc}")
        return
    raise AssertionError(f"{label}: 未被拒绝！")


OPS_ON_DATA = lambda kind, pos, n=1: [{"op": kind, "sheet": "Data", **pos, "count": n}]

print("== 边界语义 ==")

# 插入在起点处 -> 整体移位；严格内部 -> 扩展；结束之后 -> 不变
src = make_wb({"Data": {"A1": "=SUM(B1:B5)", "A2": "=SUM(C1:C5)", "A3": "=SUM(D1:D5)"}})
r = edit_and_read(src, OPS_ON_DATA("insert_rows", {"before": 1}, 2))
expect(r["Data"]["A3"], "=SUM(B3:B7)", "插入在区间起点处 -> 整体移位")
src = make_wb({"Data": {"A1": "=SUM(B1:B5)"}})
r = edit_and_read(src, OPS_ON_DATA("insert_rows", {"before": 3}, 2))
expect(r["Data"]["A1"], "=SUM(B1:B7)", "插入严格落在区间内部 -> 扩展")
r = edit_and_read(src, OPS_ON_DATA("insert_rows", {"before": 6}, 2))
expect(r["Data"]["A1"], "=SUM(B1:B5)", "插入在区间结束之后 -> 不变")

# 单格：插入点处移位；删除 -> #REF! 而不是改指相邻格
src = make_wb({"Data": {"A1": "=B4", "A2": "=B2"}})
r = edit_and_read(src, OPS_ON_DATA("insert_rows", {"before": 4}))
expect(r["Data"]["A1"], "=B5", "单格在插入点处 -> 移位")
src = make_wb({"Data": {"A1": "=B4", "A2": "=B5"}})
r = edit_and_read(src, OPS_ON_DATA("delete_rows", {"start": 4}))
expect(r["Data"]["A1"], "=#REF!", "单格被删 -> #REF!（不改指相邻格）")
expect(r["Data"]["A2"], "=B4", "删除点之后的单格 -> 前移")

# 删除使范围收缩为幸存格外接矩形；全部删除 -> #REF!
src = make_wb({"Data": {"A1": "=SUM(B2:D8)", "A2": "=SUM(B4:B6)"}})
r = edit_and_read(src, OPS_ON_DATA("delete_rows", {"start": 4}, 3))
expect(r["Data"]["A1"], "=SUM(B2:D5)", "删除区间中部 -> 收缩为幸存格外接矩形")
expect(r["Data"]["A2"], "=SUM(#REF!)", "范围全部被删 -> #REF!")

# $ 标记保留，坐标仍随结构编辑变化
src = make_wb({"Data": {"A1": "=$B$2+B$3+$B4"}})
r = edit_and_read(src, OPS_ON_DATA("insert_rows", {"before": 2}))
expect(r["Data"]["A1"], "=$B$3+B$4+$B5", "$ 绝对/混合标记保留且坐标迁移")

# 未限定引用绑定公式所在表；他表公式不受影响
src = make_wb({"Data": {"A1": "=B2"}, "Other": {"A1": "=B2", "A2": "=Data!B2"}})
r = edit_and_read(src, OPS_ON_DATA("insert_rows", {"before": 2}))
expect(r["Data"]["A1"], "=B3", "本表未限定引用随本表操作迁移")
expect(r["Other"]["A1"], "=B2", "他表未限定引用不受影响")
expect(r["Other"]["A2"], "=Data!B3", "跨表限定引用随目标表迁移")

# 字符串常量中的 Data!B4 保持原文；名称 token 不变
src = make_wb({"Data": {"A1": '="Data!B4"', "A2": "=MyName+1"}},
               names={"MyName": "Data!$B$4"})
r = edit_and_read(src, OPS_ON_DATA("insert_rows", {"before": 4}))
expect(r["Data"]["A1"], '="Data!B4"', "字符串常量中的引用保持原文")
expect(r["Data"]["A2"], "=MyName+1", "公式中的名称拼写不变")
wb = load_workbook(src.with_name(src.stem + "_out.xlsx"))
expect(wb.defined_names["MyName"].attr_text, "Data!$B$5", "名称定义随目标移动")

# 带空格与撇号转义的工作表名
src = make_wb({"My Sheet": {"A1": "x", "A2": "y"},
               "O'Brien": {"A1": "='My Sheet'!A1", "A2": "=O'Brien!A1"}})
# 第二个公式是表内未限定引用（表名即本表），改用限定形式指向 My Sheet
src2 = make_wb({"My Sheet": {"A1": "x", "A2": "y"},
                "O'Brien": {"A1": "='My Sheet'!A1"}})
r = edit_and_read(src2, [{"op": "insert_rows", "sheet": "My Sheet", "before": 1, "count": 1}])
expect(r["O'Brien"]["A1"], "='My Sheet'!A2", "带空格工作表名的限定引用迁移")

src = make_wb({"O'Brien": {"A1": "x", "A2": "y"}, "Ref": {"A1": "='O''Brien'!A1"}})
r = edit_and_read(src, [{"op": "insert_rows", "sheet": "O'Brien", "before": 1, "count": 1}])
expect(r["Ref"]["A1"], "='O''Brien'!A2", "撇号转义工作表名的限定引用迁移")

# sheet 局部名称随目标移动、作用域不变
src = make_wb({"Data": {"B2": 5, "B3": 6}, "R": {"A1": "=L"}},
              local_names={"R": {"L": "Data!$B$2:$B$3"}})
out = src.with_name(src.stem + "_out.xlsx")
run_edit(src, OPS_ON_DATA("delete_rows", {"start": 2}), out)
wb = load_workbook(out)
expect(wb["R"].defined_names["L"].attr_text, "Data!$B$2", "局部名称随删除收缩")
expect("L" not in wb.defined_names, True, "局部名称不升级为全局")

# 顺序操作作用于当前坐标（先插后删的净效果）
src = make_wb({"Data": {"A1": "=B5"}})
r = edit_and_read(src, [
    {"op": "insert_rows", "sheet": "Data", "before": 3, "count": 2},   # B5 -> B7
    {"op": "delete_rows", "sheet": "Data", "start": 3, "count": 2},    # 删新插入行 -> B5
])
expect(r["Data"]["A1"], "=B5", "操作按顺序作用于当前坐标")

# 函数名与单元引用形似（LOG10）时不误判
src = make_wb({"Data": {"A1": 1.5, "A2": "=LOG10(B2)"}})
r = edit_and_read(src, OPS_ON_DATA("insert_rows", {"before": 2}))
expect(r["Data"]["A3"], "=LOG10(B3)", "函数名 LOG10 不被当作引用")

print("== 范围外构造拒绝 ==")
for formula, label in [
    ('=INDIRECT("A1")', "INDIRECT 动态地址"),
    ("=OFFSET(A1,1,1)", "OFFSET 动态地址"),
    ("=Sheet1:Sheet2!A1", "3D 引用"),
    ("='Sheet1:Sheet2'!A1", "3D 引用（引号形式）"),
    ("=[1]Data!A1", "外部工作簿引用"),
    ("='[Book.xlsx]Data'!A1", "外部工作簿引用（引号形式）"),
    ("=Table1[Col]", "结构化表引用"),
    ("=Data!A1:Data!B2", "范围第二段带工作表限定"),
]:
    src = make_wb({"Data": {"A1": formula, "B2": 1}})
    expect_reject(src, OPS_ON_DATA("insert_rows", {"before": 1}), label)

print("\n全部冒烟测试通过")
