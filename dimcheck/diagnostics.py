"""结构化诊断；解析失败与量纲矛盾用不同 severity / code 分开。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .sources import Span


class Severity(str, Enum):
    PARSE_ERROR = "parse_error"        # 词法/语法/受限语法拒绝
    DIM_CONFLICT = "dimension_conflict"  # 求解出的量纲矛盾
    SEMANTIC_ERROR = "semantic_error"    # 未知函数等语义拒绝
    NOTE = "note"


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    span: Span
    severity: Severity
    snippet: str | None = None

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "location": {
                "file": self.span.file,
                "line": self.span.start_line,
                "column": self.span.start_col,
                "end_line": self.span.end_line,
                "end_column": self.span.end_col,
                "start_offset": self.span.start_offset,
                "end_offset": self.span.end_offset,
            },
            "snippet": self.snippet,
        }


@dataclass
class DiagnosticBag:
    items: list[Diagnostic] = field(default_factory=list)

    def add(
        self,
        code: str,
        message: str,
        span: Span,
        severity: Severity,
        snippet: str | None = None,
    ) -> None:
        self.items.append(Diagnostic(code, message, span, severity, snippet))

    @property
    def has_parse_error(self) -> bool:
        return any(d.severity is Severity.PARSE_ERROR for d in self.items)

    @property
    def has_dim_conflict(self) -> bool:
        return any(d.severity is Severity.DIM_CONFLICT for d in self.items)
