"""A1 引用模型与结构编辑坐标变换。

坐标变换规则（对“当前坐标”逐次应用）：
- 插入：位置 p 之前插入 n 行/列。
  * 单格：坐标 >= p 则后移 n；
  * 区间 [lo, hi]：p <= lo 整体后移 n；lo < p <= hi 扩展为 [lo, hi+n]；p > hi 不变。
- 删除：删除连续区间 [s, s+n)。
  * 单格：落入删除区间 -> None（引用失效 #REF!）；其后前移 n；
  * 区间：幸存格的外接矩形；无幸存格 -> None。
"""

from __future__ import annotations

from dataclasses import dataclass

from openpyxl.utils import get_column_letter

MAX_COL = 16384      # XFD
MAX_ROW = 1048576

# 操作类型
INSERT_ROWS = "insert_rows"
DELETE_ROWS = "delete_rows"
INSERT_COLS = "insert_cols"
DELETE_COLS = "delete_cols"


@dataclass(frozen=True)
class Operation:
    kind: str          # insert_rows / delete_rows / insert_cols / delete_cols
    sheet: str         # 目标工作表名
    pos: int           # 插入位置(before) 或 删除起点(start)，1 起
    count: int

    @property
    def axis(self) -> str:
        return "row" if self.kind in (INSERT_ROWS, DELETE_ROWS) else "col"

    @property
    def is_insert(self) -> bool:
        return self.kind in (INSERT_ROWS, INSERT_COLS)

    @property
    def is_delete(self) -> bool:
        return not self.is_insert

    @property
    def delete_end(self) -> int:
        """删除区间终点（含），1 起。"""
        return self.pos + self.count - 1

    def describe(self) -> str:
        axis_cn = "行" if self.axis == "row" else "列"
        if self.is_insert:
            return f"{self.kind}: 在 {self.sheet} 第 {self.pos} {axis_cn}前插入 {self.count} {axis_cn}"
        return f"{self.kind}: 删除 {self.sheet} 第 {self.pos}..{self.delete_end} {axis_cn}"

    @classmethod
    def from_json(cls, obj: dict) -> "Operation":
        kind = obj["op"]
        if kind not in (INSERT_ROWS, DELETE_ROWS, INSERT_COLS, DELETE_COLS):
            raise ValueError(f"不支持的操作类型: {kind!r}")
        pos = obj.get("before", obj.get("start"))
        if pos is None:
            raise ValueError(f"操作缺少 before/start 字段: {obj!r}")
        count = int(obj.get("count", 1))
        if pos < 1 or count < 1:
            raise ValueError(f"非法的位置或数量: {obj!r}")
        return cls(kind=kind, sheet=obj["sheet"], pos=int(pos), count=count)


def map_point_insert(p: int, before: int, count: int) -> int:
    """单点：在 before 之前插入 count 个位置后的新坐标。"""
    return p + count if p >= before else p


def map_point_delete(p: int, start: int, count: int) -> int | None:
    """单点：删除 [start, start+count) 后的新坐标；被删除返回 None。"""
    if start <= p < start + count:
        return None
    return p - count if p >= start + count else p


def map_interval_insert(lo: int, hi: int, before: int, count: int) -> tuple[int, int]:
    """区间：插入后的新区间。

    插入点 <= 起点 -> 整体移位；严格落在 (lo, hi] 内 -> 扩展；> hi -> 不变。
    """
    if before <= lo:
        return lo + count, hi + count
    if before <= hi:
        return lo, hi + count
    return lo, hi


def map_interval_delete(lo: int, hi: int, start: int, count: int) -> tuple[int, int] | None:
    """区间：删除后的幸存格外接区间；无幸存格返回 None。"""
    end = start + count - 1
    parts: list[tuple[int, int]] = []
    a_lo, a_hi = lo, min(hi, start - 1)          # 删除区间之前的幸存段
    if a_lo <= a_hi:
        parts.append((a_lo, a_hi))
    b_lo, b_hi = max(lo, end + 1), hi            # 删除区间之后的幸存段（前移）
    if b_lo <= b_hi:
        parts.append((b_lo - count, b_hi - count))
    if not parts:
        return None
    return parts[0][0], parts[-1][1]


def point_mapper(op: Operation):
    """返回该操作对单点坐标的映射函数 p -> 新坐标 或 None（被删除）。"""
    if op.is_insert:
        return lambda p: map_point_insert(p, op.pos, op.count)
    return lambda p: map_point_delete(p, op.pos, op.count)


def interval_mapper(op: Operation):
    """返回该操作对区间的映射函数 (lo, hi) -> 新区间 或 None。"""
    if op.is_insert:
        return lambda lo, hi: map_interval_insert(lo, hi, op.pos, op.count)
    return lambda lo, hi: map_interval_delete(lo, hi, op.pos, op.count)


@dataclass
class CellRef:
    col: int
    row: int
    col_abs: bool = False
    row_abs: bool = False

    def render(self) -> str:
        c = "$" if self.col_abs else ""
        r = "$" if self.row_abs else ""
        return f"{c}{get_column_letter(self.col)}{r}{self.row}"


def axis_value(ref: CellRef, axis: str) -> int:
    return ref.row if axis == "row" else ref.col


def with_axis(ref: CellRef, axis: str, value: int) -> CellRef:
    if axis == "row":
        return CellRef(col=ref.col, row=value, col_abs=ref.col_abs, row_abs=ref.row_abs)
    return CellRef(col=value, row=ref.row, col_abs=ref.col_abs, row_abs=ref.row_abs)
