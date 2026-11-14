"""网格数据模型：加载/导出、粘贴预演、整批提交与单层撤销。

模型固定为 20 行 × 8 列（由 grid.json/schema.json 决定），row_id 不是可粘贴列。
预演只构造 :class:`Preview`，绝不修改网格；只有 Enter 提交且全部合法时才落表，
落表前保存整块快照供唯一一次 Ctrl+Z 撤销。
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path

from .tsv import Field, ParseError, ParseResult, parse_tsv
from .values import Column, Schema, ValueError_, convert_field

ROWS = 20
COLS = 8


@dataclass
class PreviewError:
    """预演中的一条错误，同时给出逻辑源行列、目标坐标与原因。"""

    source_row: int          # TSV 逻辑记录号（1 起）
    source_col: int          # TSV 逻辑字段号（1 起）
    target_row: int | None   # 网格逻辑行（0 起），越界时为 None
    target_col: int | None   # 网格列下标（0 起），越界时为 None
    target_row_id: str | None
    target_col_id: str | None
    reason: str


@dataclass
class CellChange:
    row: int
    col: int
    row_id: str
    col_id: str
    old_value: object
    new_value: object
    source_row: int
    source_col: int


@dataclass
class Preview:
    anchor_row: int
    anchor_col: int
    anchor_row_id: str
    anchor_col_id: str
    n_records: int
    n_fields: int          # 首条记录的字段数（逻辑矩形宽度）
    width: int
    height: int
    changes: list[CellChange]
    errors: list[PreviewError]
    raw_payload: str
    parse: ParseResult

    @property
    def fits(self) -> bool:
        return (
            self.anchor_row + self.height <= ROWS
            and self.anchor_col + self.width <= COLS
        )

    @property
    def ok(self) -> bool:
        return self.fits and not self.errors and bool(self.changes)


class GridModel:
    def __init__(self, grid_raw: list[dict], schema: Schema):
        self.schema = schema
        self.row_ids: list[str] = [r["row_id"] for r in grid_raw]
        # cells[r][c]：保留原始 JSON 类型（str/int/bool/None）
        self.cells: list[list[object]] = [list(r["cells"]) for r in grid_raw]
        assert len(self.cells) == ROWS and all(len(r) == COLS for r in self.cells)
        # 单层撤销快照：None 表示没有可撤销内容
        self._undo_snapshot: list[list[object]] | None = None
        self._undo_row_ids: list[str] | None = None

    # ---------- 加载 / 导出 ----------

    @classmethod
    def load(cls, grid_path: str | Path, schema_path: str | Path) -> "GridModel":
        schema = Schema.load(json.loads(Path(schema_path).read_text(encoding="utf-8")))
        grid_raw = json.loads(Path(grid_path).read_text(encoding="utf-8"))
        return cls(grid_raw, schema)

    def export_json(self, dest: str | Path) -> Path:
        """按 grid.json 的同构格式导出当前表，row_id 与值/类型原样保留。"""
        payload = [
            {"row_id": self.row_ids[r], "cells": self.cells[r]}
            for r in range(ROWS)
        ]
        path = Path(dest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return path

    # ---------- 预演 ----------

    def build_preview(self, payload: str, anchor_row: int, anchor_col: int) -> Preview:
        """对粘贴 payload 做完整预演，不触碰网格。"""
        parsed = parse_tsv(payload)
        height = len(parsed.records)
        width = max((len(rec) for rec in parsed.records), default=0)

        preview = Preview(
            anchor_row=anchor_row,
            anchor_col=anchor_col,
            anchor_row_id=self.row_ids[anchor_row],
            anchor_col_id=self.schema.columns[anchor_col].id,
            n_records=height,
            n_fields=len(parsed.records[0]) if parsed.records else 0,
            width=width,
            height=height,
            changes=[],
            errors=[],
            raw_payload=payload,
            parse=parsed,
        )

        # 1) 解析期结构错误（未闭合引号、裸引号等）
        for pe in parsed.errors:
            tr, tc = self._target(pe.source_row, pe.source_col, anchor_row, anchor_col)
            preview.errors.append(
                PreviewError(
                    source_row=pe.source_row,
                    source_col=pe.source_col,
                    target_row=tr,
                    target_col=tc,
                    target_row_id=self._safe_row_id(tr),
                    target_col_id=self._safe_col_id(tc),
                    reason=pe.reason,
                )
            )

        # 2) 逐格做矩形恢复、越界与类型校验
        seen_widths: set[int] = set()
        for rec in parsed.records:
            seen_widths.add(len(rec))
        ragged = len(seen_widths) > 1

        for sr, record in enumerate(parsed.records, start=1):
            for sf, fld in enumerate(record, start=1):
                self._preview_one_field(preview, fld, sr, sf, anchor_row, anchor_col)
            # 缺列：该记录字段数少于最大宽度
            if ragged and len(record) < width:
                for missing in range(len(record) + 1, width + 1):
                    tr, tc = self._target(sr, missing, anchor_row, anchor_col)
                    preview.errors.append(
                        PreviewError(
                            source_row=sr,
                            source_col=missing,
                            target_row=tr,
                            target_col=tc,
                            target_row_id=self._safe_row_id(tr),
                            target_col_id=self._safe_col_id(tc),
                            reason=f"缺列：逻辑矩形宽 {width} 列，本行只有 {len(record)} 列",
                        )
                    )

        # 3) 矩形越界汇总（逐格越界已在上面登记，这里保证范围提示完整）
        if not preview.fits:
            reasons = []
            if anchor_row + height > ROWS:
                reasons.append(
                    f"纵向越界：需要 {height} 行，自第 {anchor_row + 1} 行起超过 {ROWS} 行边界"
                )
            if anchor_col + width > COLS:
                reasons.append(
                    f"横向越界：需要 {width} 列，自第 {anchor_col + 1} 列起超过 {COLS} 列边界"
                )
            # 只在没有逐格越界错误时补一条总览，避免重复刷屏
            if reasons and not any(
                e.target_row is None or e.target_col is None for e in preview.errors
            ):
                preview.errors.append(
                    PreviewError(
                        source_row=1,
                        source_col=1,
                        target_row=None,
                        target_col=None,
                        row_id=None,
                        col_id=None,
                        reason="；".join(reasons),
                    )
                )

        return preview

    def _preview_one_field(
        self,
        preview: Preview,
        fld: Field,
        sr: int,
        sf: int,
        anchor_row: int,
        anchor_col: int,
    ) -> None:
        tr, tc = self._target(sr, sf, anchor_row, anchor_col)

        # 越界：不做类型检查，直接登记
        if tr is None or tc is None:
            preview.errors.append(
                PreviewError(
                    source_row=sr,
                    source_col=sf,
                    target_row=tr,
                    target_col=tc,
                    target_row_id=self._safe_row_id(tr),
                    target_col_id=self._safe_col_id(tc),
                    reason="越过 20×8 表边界，该单元格无落点",
                )
            )
            return

        col = self.schema.columns[tc]
        try:
            new_value = convert_field(fld.value, col, self.schema.null_marker)
        except ValueError_ as exc:
            preview.errors.append(
                PreviewError(
                    source_row=sr,
                    source_col=sf,
                    target_row=tr,
                    target_col=tc,
                    target_row_id=self.row_ids[tr],
                    target_col_id=col.id,
                    reason=str(exc),
                )
            )
            return

        preview.changes.append(
            CellChange(
                row=tr,
                col=tc,
                row_id=self.row_ids[tr],
                col_id=col.id,
                old_value=self.cells[tr][tc],
                new_value=new_value,
                source_row=sr,
                source_col=sf,
            )
        )

    @staticmethod
    def _target(sr: int, sf: int, anchor_row: int, anchor_col: int):
        tr = anchor_row + sr - 1
        tc = anchor_col + sf - 1
        return (
            tr if 0 <= tr < ROWS else None,
            tc if 0 <= tc < COLS else None,
        )

    def _safe_row_id(self, tr: int | None) -> str | None:
        return self.row_ids[tr] if tr is not None and 0 <= tr < ROWS else None

    def _safe_col_id(self, tc: int | None) -> str | None:
        if tc is not None and 0 <= tc < COLS:
            return self.schema.columns[tc].id
        return None

    # ---------- 提交 / 撤销 ----------

    def commit(self, preview: Preview) -> int:
        """整批提交。仅在预演完全合法且放得下时允许，返回改动格数。"""
        if not preview.ok:
            raise RuntimeError("预演存在错误或越界，拒绝提交")

        # 保存整块快照（深拷贝，包含多行文本等任意值与类型）
        self._undo_snapshot = copy.deepcopy(self.cells)
        self._undo_row_ids = list(self.row_ids)

        for change in preview.changes:
            self.cells[change.row][change.col] = change.new_value
        return len(preview.changes)

    def can_undo(self) -> bool:
        return self._undo_snapshot is not None

    def undo(self) -> bool:
        """恢复最近一次成功粘贴之前的全部原值与类型。只有一层。"""
        if self._undo_snapshot is None:
            return False
        self.cells = copy.deepcopy(self._undo_snapshot)
        self.row_ids = list(self._undo_row_ids)  # type: ignore[arg-type]
        self._undo_snapshot = None
        self._undo_row_ids = None
        return True
