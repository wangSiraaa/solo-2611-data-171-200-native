"""结构编辑引擎：按顺序应用操作，搬移单元并重写全簿公式与命名范围。"""

from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.dimensions import DimensionHolder

from .formula import RewriteResult, rewrite_formula, rewrite_text
from .refs import Operation, point_mapper

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def strip_formula_caches(path: str | Path) -> int:
    """删除结果文件中公式单元的缓存值元素 <v>（含空缓存）。

    结构编辑不求值，任何缓存都可能冒充新结果，故一律移除；
    配合 calcPr fullCalcOnLoad=1 由打开方重算。返回清理的单元数。
    """
    path = Path(path)
    with zipfile.ZipFile(path) as z:
        items = {i.filename: z.read(i.filename) for i in z.infolist()}
        infos = z.infolist()
    stripped = 0
    for name in list(items):
        if not name.startswith("xl/worksheets/") or not name.endswith(".xml"):
            continue
        root = etree.fromstring(items[name])
        changed = False
        for c in root.iter(f"{{{MAIN_NS}}}c"):
            if c.find(f"{{{MAIN_NS}}}f") is None:
                continue
            v = c.find(f"{{{MAIN_NS}}}v")
            if v is not None:
                c.remove(v)
                stripped += 1
                changed = True
        if changed:
            items[name] = etree.tostring(
                root, xml_declaration=True, encoding="UTF-8", standalone=True
            )
    if stripped:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for info in infos:
                z.writestr(info.filename, items[info.filename])
    return stripped


def sha256_of(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- 单元搬移

def _remap_dimensions(ws, op: Operation) -> None:
    """行高/列宽等维度记录随结构编辑移动（普通样式保留）。"""
    pmap = point_mapper(op)
    if op.axis == "row":
        new = DimensionHolder(worksheet=ws)
        for idx, dim in ws.row_dimensions.items():
            ni = pmap(idx)
            if ni is None:
                continue
            dim.index = ni
            new[ni] = dim
        ws.row_dimensions = new
    else:
        from .refs import interval_mapper

        imap = interval_mapper(op)
        new = DimensionHolder(worksheet=ws)
        for _key, dim in ws.column_dimensions.items():
            res = imap(dim.min, dim.max)
            if res is None:
                continue
            dim.min, dim.max = res
            dim.index = get_column_letter(dim.min)
            new[dim.index] = dim
        ws.column_dimensions = new


def _remap_merges(ws, op: Operation) -> None:
    if not ws.merged_cells.ranges:
        return
    from .refs import interval_mapper

    imap = interval_mapper(op)
    kept = []
    for rng in ws.merged_cells.ranges:
        if op.axis == "row":
            res = imap(rng.min_row, rng.max_row)
            if res is None:
                continue
            rng.min_row, rng.max_row = res
        else:
            res = imap(rng.min_col, rng.max_col)
            if res is None:
                continue
            rng.min_col, rng.max_col = res
        kept.append(rng)
    ws.merged_cells.ranges = set(kept)


def move_cells(ws, op: Operation) -> dict:
    """按操作搬移目标工作表单元；返回移动/删除统计。

    直接重建 ws._cells：单元对象（含样式、类型、批注等）整体跟随坐标，
    被删除行/列上的单元丢弃。
    """
    pmap = point_mapper(op)
    moved: dict[tuple[int, int], tuple[int, int]] = {}
    deleted: list[tuple[int, int]] = []
    new_cells: dict[tuple[int, int], object] = {}
    for (r, c), cell in ws._cells.items():
        nr, nc = (pmap(r), c) if op.axis == "row" else (r, pmap(c))
        if nr is None or nc is None:
            deleted.append((r, c))
            continue
        cell.row, cell.column = nr, nc
        new_cells[(nr, nc)] = cell
        if (nr, nc) != (r, c):
            moved[(r, c)] = (nr, nc)
    ws._cells = new_cells
    ws._current_row = max((r for r, _ in ws._cells), default=0)
    _remap_dimensions(ws, op)
    _remap_merges(ws, op)
    return {
        "moved": {f"{r},{c}": f"{nr},{nc}" for (r, c), (nr, nc) in moved.items()},
        "deleted": [f"{r},{c}" for r, c in sorted(deleted)],
    }


# ---------------------------------------------------------------- 报告模型

@dataclass
class FormulaTrack:
    sheet: str
    cell_before: str
    before: str
    coord: tuple[int, int]          # 当前坐标（随操作更新）
    alive: bool = True
    after: str | None = None
    cell_after: str | None = None
    events: list[dict] = field(default_factory=list)


@dataclass
class NameTrack:
    name: str
    scope: str                      # "workbook" 或工作表名
    before: str
    after: str | None = None
    events: list[dict] = field(default_factory=list)


class EditEngine:
    def __init__(self, source: str | Path):
        self.source = Path(source)
        self.source_sha256 = sha256_of(self.source)
        self.wb = load_workbook(self.source)     # data_only=False：保留公式原文
        self.formulas: list[FormulaTrack] = []
        self.names: list[NameTrack] = []
        self.op_reports: list[dict] = []
        self.caches_stripped = 0
        self._snapshot()

    # ---------------- 快照：登记所有公式与命名范围，跟踪其坐标
    def _snapshot(self) -> None:
        from openpyxl.utils import get_column_letter as gcl

        for ws in self.wb.worksheets:
            for (r, c), cell in ws._cells.items():
                if isinstance(cell.value, str) and cell.value.startswith("="):
                    self.formulas.append(
                        FormulaTrack(
                            sheet=ws.title,
                            cell_before=f"{gcl(c)}{r}",
                            before=cell.value,
                            coord=(r, c),
                        )
                    )
        for dn in self.wb.defined_names.values():
            self.names.append(
                NameTrack(dn.name, "workbook", dn.attr_text or "")
            )
        for ws in self.wb.worksheets:
            for dn in ws.defined_names.values():
                self.names.append(
                    NameTrack(dn.name, ws.title, dn.attr_text or "")
                )

    # ---------------- 主流程
    def apply(self, op: Operation) -> None:
        if op.sheet not in self.wb.sheetnames:
            raise ValueError(f"操作目标工作表不存在: {op.sheet!r}")
        ws = self.wb[op.sheet]
        op_index = len(self.op_reports) + 1
        move_stats = move_cells(ws, op)
        self._advance_formula_coords(op)
        formula_events = self._rewrite_formulas(op, op_index)
        name_events = self._rewrite_names(op, op_index)
        self.op_reports.append(
            {
                "index": op_index,
                "operation": op.describe(),
                "cells_moved": len(move_stats["moved"]),
                "cells_deleted": move_stats["deleted"],
                "formulas_touched": formula_events,
                "names_touched": name_events,
            }
        )

    def _advance_formula_coords(self, op: Operation) -> None:
        pmap = point_mapper(op)
        for tr in self.formulas:
            if not tr.alive or tr.sheet != op.sheet:
                continue
            r, c = tr.coord
            nr, nc = (pmap(r), c) if op.axis == "row" else (r, pmap(c))
            if nr is None or nc is None:
                tr.alive = False
                tr.events.append(
                    {"op": op.describe(), "ref": tr.cell_before,
                     "result": None,
                     "reason": "公式所在单元被本次删除移除"}
                )
            else:
                tr.coord = (nr, nc)

    def _rewrite_formulas(self, op: Operation, op_index: int) -> int:
        touched = 0
        # 当前坐标 -> 跟踪记录（仅本操作后仍存活的公式）
        by_pos = {(t.sheet, t.coord): t for t in self.formulas if t.alive}
        for ws in self.wb.worksheets:
            for (r, c), cell in ws._cells.items():
                v = cell.value
                if not (isinstance(v, str) and v.startswith("=")):
                    continue
                res: RewriteResult = rewrite_formula(v, ws.title, op)
                if res.text != v:
                    cell.value = res.text
                tr = by_pos.get((ws.title, (r, c)))
                if tr is not None:
                    for ch in res.changes:
                        if ch.ref_before != ch.ref_after:
                            tr.events.append(
                                {
                                    "op": op.describe(),
                                    "ref": ch.ref_before,
                                    "result": ch.ref_after,
                                    "reason": ch.reason,
                                }
                            )
                if res.text != v:
                    touched += 1
        return touched

    def _rewrite_names(self, op: Operation, op_index: int) -> int:
        touched = 0
        targets: list[tuple[object, str | None]] = []
        for dn in self.wb.defined_names.values():
            targets.append((dn, None))
        for ws in self.wb.worksheets:
            for dn in ws.defined_names.values():
                targets.append((dn, ws.title))
        track_by_key = {(n.name, n.scope): n for n in self.names}
        for dn, scope_sheet in targets:
            old = dn.attr_text or ""
            res = rewrite_text(old, scope_sheet, op)
            if res.text != old:
                dn.attr_text = res.text
                touched += 1
            scope = "workbook" if scope_sheet is None else scope_sheet
            tr = track_by_key.get((dn.name, scope))
            if tr is not None:
                for ch in res.changes:
                    if ch.ref_before != ch.ref_after:
                        tr.events.append(
                            {
                                "op": op.describe(),
                                "ref": ch.ref_before,
                                "result": ch.ref_after,
                                "reason": ch.reason,
                            }
                        )
        return touched

    # ---------------- 收尾与交付
    def finalize(self, output: str | Path) -> dict:
        from openpyxl.utils import get_column_letter as gcl

        # 重算状态：不计算公式值，不写回任何缓存，要求打开方全量重算
        self.wb.calculation.fullCalcOnLoad = True

        for tr in self.formulas:
            if tr.alive:
                r, c = tr.coord
                cell = self.wb[tr.sheet]._cells.get((r, c))
                tr.after = cell.value if cell is not None else None
                tr.cell_after = f"{gcl(c)}{r}"
        for tr in self.names:
            found = None
            for ws in [None, *self.wb.worksheets]:
                container = (
                    self.wb.defined_names if ws is None else ws.defined_names
                )
                if tr.name in container:
                    dn = container[tr.name]
                    scope = "workbook" if ws is None else ws.title
                    if scope == tr.scope:
                        found = dn.attr_text
                        break
            tr.after = found

        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        self.wb.save(output)
        self.caches_stripped = strip_formula_caches(output)
        # 源文件只读性自证
        assert sha256_of(self.source) == self.source_sha256, "源文件被修改！"
        return self.report(output)

    def report(self, output: Path) -> dict:
        return {
            "tool": "structedit",
            "source": {"path": str(self.source), "sha256": self.source_sha256},
            "output": {"path": str(output), "sha256": sha256_of(output)},
            "operations": self.op_reports,
            "formulas": [
                {
                    "sheet": t.sheet,
                    "cell_before": t.cell_before,
                    "cell_after": t.cell_after,
                    "before": t.before,
                    "after": t.after,
                    "status": ("deleted" if not t.alive
                               else "changed" if t.after != t.before
                               else "unchanged"),
                    "events": t.events,
                }
                for t in self.formulas
            ],
            "defined_names": [
                {
                    "name": t.name,
                    "scope": t.scope,
                    "before": t.before,
                    "after": t.after,
                    "status": "changed" if t.after != t.before else "unchanged",
                    "events": t.events,
                }
                for t in self.names
            ],
            "recalculation": {
                "fullCalcOnLoad": True,
                "formula_caches_stripped": self.caches_stripped,
                "note": "结构编辑不计算公式值；结果副本中公式单元的缓存值已移除"
                        f"（{self.caches_stripped} 处），并设置 calcPr fullCalcOnLoad=1，"
                        "由打开方重算。",
            },
        }


def run_edit(
    source: str | Path,
    operations: list[dict],
    output: str | Path,
    report_path: str | Path | None = None,
) -> dict:
    engine = EditEngine(source)
    for obj in operations:
        engine.apply(Operation.from_json(obj))
    report = engine.finalize(output)
    if report_path is not None:
        Path(report_path).parent.mkdir(parents=True, exist_ok=True)
        Path(report_path).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return report
