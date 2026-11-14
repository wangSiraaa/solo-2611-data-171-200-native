"""编译错误：携带源 SQL 中的原始位置（offset/line/column）与片段指示。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Position:
    offset: int
    line: int
    column: int

    def as_dict(self) -> dict[str, int]:
        return {"offset": self.offset, "line": self.line, "column": self.column}


class CompileError(Exception):
    """所有拒绝报表的情况统一抛出本异常。

    rule 取值：
      unsupported_syntax          scope 之外的语法（正则、JOIN、GROUP BY、未知函数等）
      nondeterministic_distinct   DISTINCT ON 的 ORDER BY 不含 event_id，选行不确定
      invalid_ordering            DISTINCT ON 键与 ORDER BY 前导键不一致等
      type_error                 声明类型与表达式用法冲突
      invalid_parameter          参数声明/绑定值不合法
      parse_error                词法或语法层面的非法输入
      scope_violation            违反“一条 SELECT、一个非递归 CTE”等结构约束
    """

    def __init__(self, rule: str, message: str, position: Position | None = None,
                 source: str | None = None):
        super().__init__(message)
        self.rule = rule
        self.message = message
        self.position = position
        self.source = source

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"rule": self.rule, "message": self.message}
        if self.position is not None:
            data["position"] = self.position.as_dict()
            data["snippet"] = self._snippet()
        return data

    def _snippet(self) -> str:
        if self.source is None or self.position is None:
            return ""
        lines = self.source.splitlines()
        if not lines:
            return ""
        line_no = self.position.line
        if not 1 <= line_no <= len(lines):
            return ""
        line_text = lines[line_no - 1]
        caret = " " * (self.position.column - 1) + "^"
        return f"{line_text}\n{caret}"
