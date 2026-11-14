"""按 schema 做字段值的类型校验与规范化。

关键约束（schema.json）：
- 文本空字段 => 空字符串 ``""``；非文本字段空 => 非法。
- 空值只能通过精确的 null_marker（``\\N``）表达，且列必须 nullable。
- 整数：十进制整数字符串，规范化为 Python ``int``；``0`` 与空串/None 完全不同。
- 十进制：定点文本，最多两位小数，规范化为恰好两位小数字符串；禁止指数、禁止 float。
- 布尔：仅小写 ``true`` / ``false``，映射为 Python ``bool``；``yes``/``0`` 等均非法。
- 日期：``YYYY-MM-DD`` 且通过真实日历校验（``datetime.date`` 构造）。
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass

_NULL_MARKER = "\\N"
_INTEGER_RE = re.compile(r"^[+-]?\d+$")
_DECIMAL_RE = re.compile(r"^([+-]?)(\d+)(?:\.(\d{1,2}))?$")
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_BOOLEAN_TRUE = "true"
_BOOLEAN_FALSE = "false"


@dataclass
class Column:
    id: str
    type: str
    nullable: bool


@dataclass
class Schema:
    columns: list[Column]
    null_marker: str

    @classmethod
    def load(cls, raw: dict) -> "Schema":
        cols = [
            Column(id=c["id"], type=c["type"], nullable=bool(c["nullable"]))
            for c in raw["columns"]
        ]
        return cls(columns=cols, null_marker=raw.get("null_marker", _NULL_MARKER))


class ValueError_(Exception):
    """单个字段的类型/取值错误（与内置 ValueError 区分命名）。"""


def _canonical_decimal(text: str) -> str:
    m = _DECIMAL_RE.match(text)
    if not m:
        raise ValueError_("十进制数需为定点文本，最多两位小数，禁止指数或浮点写法")
    sign, int_part, frac_part = m.group(1), m.group(2), m.group(3)
    # 规范化整数部分：去掉前导零，全零保留一个 0
    int_part = int_part.lstrip("0") or "0"
    frac = (frac_part or "").ljust(2, "0")
    canonical = f"{int_part}.{frac}"
    # -0 / -0.00 规范化为 0.00
    if sign == "-" and int_part == "0" and frac == "00":
        sign = ""
    return f"{sign}{canonical}"


def convert_field(text: str, col: Column, null_marker: str = _NULL_MARKER):
    """把 TSV 原始字符串按列类型转换为规范化的 Python 值。

    返回值类型：
      text    -> str | None
      integer -> int | None
      decimal -> str | None   （两位小数字符串，绝不使用 float）
      date    -> str | None   （校验通过的 ISO 日期字符串）
      boolean -> bool | None
    """

    # 1. 精确 null 标记：不做 strip、不区分大小写，必须逐字符相等
    if text == null_marker:
        if not col.nullable:
            raise ValueError_(f"列 {col.id} 不可为空，不接受 {null_marker}")
        return None

    # 2. 空字符串
    if text == "":
        if col.type == "text":
            return ""  # 文本空串是合法值，与 None 不同
        raise ValueError_(f"列 {col.id} 为 {col.type} 类型，空字段非法（空值须写 {null_marker}）")

    # 3. 按类型分派；绝不使用 bool()/float() 隐式转换
    if col.type == "text":
        return text

    if col.type == "integer":
        if not _INTEGER_RE.match(text):
            raise ValueError_(f"整数格式非法：{text!r}（需为十进制整数，如 0、-7）")
        return int(text)  # int() 仅做字符串解析，不涉及 float

    if col.type == "decimal":
        return _canonical_decimal(text)

    if col.type == "date":
        m = _DATE_RE.match(text)
        if not m:
            raise ValueError_(f"日期格式非法：{text!r}（需为 YYYY-MM-DD）")
        try:
            datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            raise ValueError_(f"日期不存在于真实日历：{text!r}")
        return text

    if col.type == "boolean":
        if text == _BOOLEAN_TRUE:
            return True
        if text == _BOOLEAN_FALSE:
            return False
        raise ValueError_(f"布尔值仅接受小写 true 或 false，收到 {text!r}")

    raise ValueError_(f"未知列类型：{col.type}")
