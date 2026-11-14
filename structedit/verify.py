"""验证器：重新读取实际结果 XLSX，独立核对结构编辑的正确性。

核对内容：
1. 源文件摘要不变；工作表顺序不变；
2. 用身份格图独立跟踪每个幸存源格，核对每个新引用确实指向对应身份
   （单格精确命中；范围为双向包含；被删引用为 #REF!）；
3. 非公式格内容逐格一致（不多不少）；普通样式随格保留；
4. 命名单元：定义随目标迁移、作用域不变、公式中的名称拼写不变；
5. 字符串常量等非引用 token 逐字不变；
6. 用 lxml 直接解析结果 XML：fullCalcOnLoad=1、公式单元无缓存值、
   原始 <f> 文本与名称定义和 openpyxl 视图一致。
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from .engine import sha256_of
from .formula import RefToken, build_refs, check_unsupported, tokenize
from .identity import IdentityGrid
from .refs import Operation

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

_MAX_RECT_CELLS = 1_000_000


@dataclass
class VerifyReport:
    checks: list[dict] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append({"check": name, "ok": ok, "detail": detail})

    @property
    def ok(self) -> bool:
        return all(c["ok"] for c in self.checks)

    @property
    def failures(self) -> list[dict]:
        return [c for c in self.checks if not c["ok"]]


# ---------------------------------------------------------------- 公式分析

def analyze(body: str) -> list[tuple[str, object]]:
    """把公式体（不含 '='）分解为 [('lit', 原文) | ('ref', RefToken)] 序列。"""
    check_unsupported(body)
    tokens = tokenize(body)
    refs = build_refs(tokens)
    items: list[tuple[str, object]] = []
    last = 0
    for ref in refs:
        if ref.start > last:
            items.append(("lit", body[last:ref.start]))
        items.append(("ref", ref))
        last = ref.end
    if last < len(body):
        items.append(("lit", body[last:]))
    return items


def _rect_cells(a, b):
    r1, r2 = sorted((a.row, b.row))
    c1, c2 = sorted((a.col, b.col))
    if (r2 - r1 + 1) * (c2 - c1 + 1) > _MAX_RECT_CELLS:
        raise ValueError("引用矩形过大，超出验证范围")
    return [(r, c) for r in range(r1, r2 + 1) for c in range(c1, c2 + 1)]


def _check_ref_pair(
    src: RefToken,
    dst: RefToken,
    context_sheet: str | None,
    edited_sheets: set[str],
    grid: IdentityGrid,
    where: str,
    errors: list[str],
) -> None:
    """核对一对对齐的引用 token：dst 是否正确地是 src 迁移后的结果。"""
    if src.is_ref_error:
        if dst.text != src.text:
            errors.append(f"{where}: 原已失效引用 {src.text} 不应变化，实际 {dst.text}")
        return
    target = src.sheet_name if src.sheet_name is not None else context_sheet
    if target is None or target.casefold() not in {s.casefold() for s in edited_sheets}:
        if dst.text != src.text:
            errors.append(
                f"{where}: 引用 {src.text} 目标表未编辑，应保持不变，实际 {dst.text}"
            )
        return
    # 目标表被编辑：用身份格图核对
    rev = grid.reverse()
    if src.sheet_raw != dst.sheet_raw:
        errors.append(
            f"{where}: 工作表前缀拼写应保留 {src.sheet_raw!r}，实际 {dst.sheet_raw!r}"
        )
    if src.kind == "cell":
        a = src.first
        if not grid.tracked(target, a.row, a.col):
            errors.append(f"{where}: 源格 {target}!{a.row},{a.col} 不在身份格图中")
            return
        fwd = grid.forward(target, a.row, a.col)
        if fwd is None:
            if not dst.is_ref_error:
                errors.append(f"{where}: {src.text} 的源格已被删除，应为 #REF!，实际 {dst.text}")
            return
        if dst.is_ref_error or dst.kind != "cell" or dst.first is None:
            errors.append(f"{where}: {src.text} 的源格幸存，不应变为 {dst.text}")
            return
        if (dst.first.row, dst.first.col) != fwd:
            errors.append(
                f"{where}: {src.text} 应指向身份格新位置 "
                f"{get_column_letter(fwd[1])}{fwd[0]}，实际 {dst.text}"
            )
        if (dst.first.col_abs, dst.first.row_abs) != (a.col_abs, a.row_abs):
            errors.append(f"{where}: {src.text} 的 $ 标记应保留，实际 {dst.text}")
        return
    # range
    a, b = src.first, src.second
    cells = _rect_cells(a, b)
    survivors = {}
    for r, c in cells:
        if not grid.tracked(target, r, c):
            errors.append(f"{where}: 源格 {target}!{r},{c} 不在身份格图中")
            return
        fwd = grid.forward(target, r, c)
        if fwd is not None:
            survivors[(r, c)] = fwd
    if not survivors:
        if not dst.is_ref_error:
            errors.append(
                f"{where}: {src.text} 的引用格全部被删除，应为 #REF!，实际 {dst.text}"
            )
        return
    if dst.is_ref_error or dst.first is None:
        errors.append(f"{where}: {src.text} 有幸存格，不应变为 {dst.text}")
        return
    if dst.kind == "range" and dst.second is not None:
        dst_first, dst_second = dst.first, dst.second
    elif dst.kind == "cell":
        dst_first = dst_second = dst.first      # 区间收缩为单格的写法
    else:
        errors.append(f"{where}: {src.text} 有幸存格，不应变为 {dst.text}")
        return
    dst_cells = set(_rect_cells(dst_first, dst_second))
    # (a) 每个幸存的源引用格必须落在新范围内
    for (r0, c0), (r, c) in survivors.items():
        if (r, c) not in dst_cells:
            errors.append(
                f"{where}: 幸存源格 {get_column_letter(c0)}{r0}→"
                f"{get_column_letter(c)}{r} 未包含在新引用 {dst.text} 内"
            )
    # (b) 新范围内带身份的格必须原本就被引用（插入产生的新格除外）
    for (r, c) in dst_cells:
        ident = rev.get((target, r, c))
        if ident is not None and ident not in survivors:
            errors.append(
                f"{where}: 新引用 {dst.text} 中的 "
                f"{get_column_letter(c)}{r} 是未曾被引用的源格 "
                f"{get_column_letter(ident[1])}{ident[0]}"
            )
    # $ 标记按角点保留
    for s_corner, d_corner in ((src.first, dst_first), (src.second, dst_second)):
        if (s_corner.col_abs, s_corner.row_abs) != (d_corner.col_abs, d_corner.row_abs):
            errors.append(f"{where}: {src.text} 的 $ 标记应保留，实际 {dst.text}")
            break


def _check_formula_pair(
    src_body: str,
    dst_body: str,
    context_sheet: str | None,
    edited_sheets: set[str],
    grid: IdentityGrid,
    where: str,
    errors: list[str],
) -> None:
    src_items = analyze(src_body)
    dst_items = analyze(dst_body)
    if len(src_items) != len(dst_items):
        errors.append(f"{where}: token 序列长度不一致 {src_body!r} vs {dst_body!r}")
        return
    src_refs = [it for it in src_items if it[0] == "ref"]
    dst_refs = [it for it in dst_items if it[0] == "ref"]
    if len(src_refs) != len(dst_refs):
        errors.append(f"{where}: 引用数量不一致 {src_body!r} vs {dst_body!r}")
        return
    for (sk, sv), (dk, dv) in zip(src_items, dst_items):
        if sk != dk:
            errors.append(f"{where}: token 类型错位 {src_body!r} vs {dst_body!r}")
            return
        if sk == "lit" and sv != dv:
            errors.append(f"{where}: 非引用片段被改动 {sv!r} → {dv!r}")
            return
    for (_, s), (_, d) in zip(src_refs, dst_refs):
        _check_ref_pair(s, d, context_sheet, edited_sheets, grid, where, errors)


# ---------------------------------------------------------------- 主流程

def _formula_cells(wb):
    out = {}
    for ws in wb.worksheets:
        for (r, c), cell in ws._cells.items():
            if isinstance(cell.value, str) and cell.value.startswith("="):
                out[(ws.title, r, c)] = cell.value
    return out


def _value_map(wb):
    out = {}
    for ws in wb.worksheets:
        for (r, c), cell in ws._cells.items():
            v = cell.value
            if isinstance(v, str) and v.startswith("="):
                continue
            if v is not None:
                out[(ws.title, r, c)] = v
    return out


def _defined_names(wb):
    """[(name, scope, text)]，scope 为 'workbook' 或工作表名。"""
    out = []
    for dn in wb.defined_names.values():
        out.append((dn.name, "workbook", dn.attr_text or ""))
    for ws in wb.worksheets:
        for dn in ws.defined_names.values():
            out.append((dn.name, ws.title, dn.attr_text or ""))
    return out


def verify(
    source: str | Path,
    operations: list[dict],
    result: str | Path,
    report_path: str | Path | None = None,
) -> VerifyReport:
    rep = VerifyReport()
    source = Path(source)
    result = Path(result)
    ops = [Operation.from_json(o) for o in operations]
    edited_sheets = {op.sheet for op in ops}

    report = None
    if report_path is not None:
        report = json.loads(Path(report_path).read_text(encoding="utf-8"))

    # ---- 1. 源摘要不变
    src_sha = sha256_of(source)
    if report is not None:
        rep.add("源文件摘要不变", src_sha == report["source"]["sha256"],
                f"sha256={src_sha}")
        rep.add("结果文件摘要与报告一致", sha256_of(result) == report["output"]["sha256"],
                f"sha256={sha256_of(result)}")
    else:
        rep.add("源文件可读", True, f"sha256={src_sha}")

    wb_src = load_workbook(source)
    wb_dst = load_workbook(result)

    # ---- 2. 工作表顺序
    rep.add("工作表顺序不变", wb_src.sheetnames == wb_dst.sheetnames,
            f"{wb_src.sheetnames} → {wb_dst.sheetnames}")

    # ---- 3. 身份格图：跟踪全部源格 + 所有被引用格
    grid = IdentityGrid()
    for ws in wb_src.worksheets:
        for (r, c) in ws._cells:
            grid.add(ws.title, r, c)
    src_formulas = _formula_cells(wb_src)
    src_names = _defined_names(wb_src)

    def _track_refs(body: str, context: str | None) -> None:
        for kind, item in analyze(body):
            if kind != "ref" or item.is_ref_error:
                continue
            target = item.sheet_name if item.sheet_name is not None else context
            if target is None:
                continue
            if item.kind == "cell":
                grid.add(target, item.first.row, item.first.col)
            else:
                for r, c in _rect_cells(item.first, item.second):
                    grid.add(target, r, c)

    for (sheet, _r, _c), f in src_formulas.items():
        _track_refs(f[1:], sheet)
    for _name, scope, text in src_names:
        _track_refs(text, None if scope == "workbook" else scope)
    for op in ops:
        grid.apply(op)

    # ---- 4. 公式：位置集合 + 逐引用身份核对
    dst_formulas = _formula_cells(wb_dst)
    expected_positions = set()
    errors: list[str] = []
    for (sheet, r, c), f in sorted(src_formulas.items()):
        fwd = grid.forward(sheet, r, c)
        if fwd is None:
            continue                                # 公式格被删除
        expected_positions.add((sheet, *fwd))
        dst_f = dst_formulas.get((sheet, *fwd))
        where = f"{sheet}!{get_column_letter(c)}{r}"
        if dst_f is None:
            errors.append(f"{where}: 公式在结果中丢失")
            continue
        _check_formula_pair(f[1:], dst_f[1:], sheet, edited_sheets, grid,
                            f"{where}→{get_column_letter(fwd[1])}{fwd[0]}", errors)
    rep.add("公式单元位置集合正确", set(dst_formulas) == expected_positions,
            f"结果 {len(dst_formulas)} 个，预期 {len(expected_positions)} 个")
    rep.add("逐引用身份核对", not errors, "; ".join(errors) if errors else
            f"共核对 {len(src_formulas)} 个公式的全部引用")

    # ---- 5. 非公式格内容逐格一致
    rev = grid.reverse()
    expected_values = {}
    for (sheet, r, c), v in _value_map(wb_src).items():
        fwd = grid.forward(sheet, r, c)
        if fwd is not None:
            expected_values[(sheet, *fwd)] = v
    actual_values = _value_map(wb_dst)
    diffs = []
    for key in set(expected_values) | set(actual_values):
        if expected_values.get(key) != actual_values.get(key):
            diffs.append(
                f"{key[0]}!{get_column_letter(key[2])}{key[1]}: "
                f"预期 {expected_values.get(key)!r} 实际 {actual_values.get(key)!r}"
            )
    rep.add("非公式格内容逐格一致（含插入格为空）", not diffs, "; ".join(diffs))

    # ---- 6. 普通样式随格保留
    style_diffs = []
    for ws in wb_src.worksheets:
        ws_d = wb_dst[ws.title]
        for (r, c), cell in ws._cells.items():
            fwd = grid.forward(ws.title, r, c)
            if fwd is None:
                continue
            dst_cell = ws_d._cells.get(fwd)
            if dst_cell is None or dst_cell._style != cell._style:
                style_diffs.append(f"{ws.title}!{get_column_letter(c)}{r}")
    rep.add("普通样式随格保留", not style_diffs, "; ".join(style_diffs))

    # ---- 7. 命名范围
    dst_names = {(n, s): t for n, s, t in _defined_names(wb_dst)}
    name_errors = []
    for name, scope, text in src_names:
        dst_text = dst_names.get((name, scope))
        where = f"名称 {name}（作用域 {scope}）"
        if dst_text is None:
            name_errors.append(f"{where}: 在结果中丢失或作用域改变")
            continue
        context = None if scope == "workbook" else scope
        _check_formula_pair(text, dst_text, context, edited_sheets, grid,
                            where, name_errors)
    missing = set(dst_names) - {(n, s) for n, s, _t in src_names}
    if missing:
        name_errors.append(f"结果中出现多余名称: {missing}")
    rep.add("命名范围迁移与作用域", not name_errors, "; ".join(name_errors))

    # ---- 8. lxml 直接解析结果 XML
    _verify_raw_xml(result, wb_dst, dst_formulas, rep)

    # ---- 9. 与 edit 报告交叉核对
    if report is not None:
        _cross_check_report(report, dst_formulas, dst_names, rep)

    return rep


def _verify_raw_xml(result: Path, wb_dst, dst_formulas, rep: VerifyReport) -> None:
    with zipfile.ZipFile(result) as z:
        wb_xml = etree.fromstring(z.read("xl/workbook.xml"))
        calc = wb_xml.find(f"{{{MAIN_NS}}}calcPr")
        rep.add(
            "calcPr fullCalcOnLoad=1（未求值，要求打开方重算）",
            calc is not None and calc.get("fullCalcOnLoad") == "1",
            etree.tostring(calc).decode() if calc is not None else "缺少 calcPr",
        )
        # sheet 名 → xml 路径
        rels = etree.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        rid_to_target = {
            rel.get("Id"): rel.get("Target")
            for rel in rels.iter(f"{{{PKG_REL_NS}}}Relationship")
        }
        sheet_files = {}
        for sh in wb_xml.iter(f"{{{MAIN_NS}}}sheet"):
            target = rid_to_target[sh.get(f"{{{REL_NS}}}id")]
            # openpyxl 写绝对路径（/xl/...），Excel 写相对路径（worksheets/...）
            path = target.lstrip("/") if target.startswith("/") else "xl/" + target
            sheet_files[sh.get("name")] = path
        # 公式单元不得带缓存值；原始 <f> 文本与 openpyxl 视图一致
        cache_errors, formula_errors = [], []
        raw_names = []
        for sheet_name, path in sheet_files.items():
            root = etree.fromstring(z.read(path))
            for c in root.iter(f"{{{MAIN_NS}}}c"):
                f_el = c.find(f"{{{MAIN_NS}}}f")
                if f_el is None:
                    continue
                v_el = c.find(f"{{{MAIN_NS}}}v")
                if v_el is not None:
                    cache_errors.append(f"{sheet_name}!{c.get('r')}")
                ref = c.get("r")
                expect = dst_formulas.get((sheet_name, *_rc(ref)))
                if expect is None or expect[1:] != (f_el.text or ""):
                    formula_errors.append(f"{sheet_name}!{ref}")
        dn_parent = wb_xml.find(f"{{{MAIN_NS}}}definedNames")
        if dn_parent is not None:
            for dn in dn_parent.iter(f"{{{MAIN_NS}}}definedName"):
                raw_names.append(
                    (dn.get("name"), dn.get("localSheetId"), dn.text or "")
                )
        rep.add("公式单元无旧缓存值", not cache_errors, "; ".join(cache_errors))
        rep.add("原始 <f> 文本与公式视图一致", not formula_errors,
                "; ".join(formula_errors))
        # 原始 definedName 与 openpyxl 视图一致
        expect_names = set()
        for dn in wb_dst.defined_names.values():
            expect_names.add((dn.name, None, dn.attr_text or ""))
        for idx, ws in enumerate(wb_dst.worksheets):
            for dn in ws.defined_names.values():
                expect_names.add((dn.name, str(idx), dn.attr_text or ""))
        rep.add("原始 definedName 与名称视图一致",
                set(raw_names) == expect_names,
                f"raw={sorted(raw_names)} view={sorted(expect_names)}")


def _rc(ref: str) -> tuple[int, int]:
    from openpyxl.utils.cell import coordinate_from_string, column_index_from_string

    col, row = coordinate_from_string(ref)
    return row, column_index_from_string(col)


def _cross_check_report(report, dst_formulas, dst_names, rep: VerifyReport) -> None:
    errors = []
    for entry in report["formulas"]:
        if entry["status"] == "deleted":
            continue
        key = (entry["sheet"], *_rc(entry["cell_after"]))
        actual = dst_formulas.get(key)
        if actual != entry["after"]:
            errors.append(f"{entry['sheet']}!{entry['cell_after']}: "
                          f"报告 {entry['after']!r} 实际 {actual!r}")
    for entry in report["defined_names"]:
        actual = dst_names.get((entry["name"], entry["scope"]))
        if actual != entry["after"]:
            errors.append(f"名称 {entry['name']}: 报告 {entry['after']!r} 实际 {actual!r}")
    rep.add("edit 报告与实际结果一致", not errors, "; ".join(errors))
