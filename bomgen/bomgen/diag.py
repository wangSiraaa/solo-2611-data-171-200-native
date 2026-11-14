"""诊断条目: 阻断发布的错误与警告的统一表示。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Diag:
    code: str                      # 诊断码, 如 GAP_UNKNOWN_CONDITION
    severity: str                  # "error"(阻断发布) | "warning"
    message: str
    path: tuple[str, ...] | None = None   # 发生路径(点分段), 与具体发生无关时为 None
    basis: dict = field(default_factory=dict)  # 判定依据(未决原因/环链/约束两侧取值等)

    def sort_key(self):
        return (self.code, self.path or (), self.message)

    def to_json(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "path": ".".join(self.path) if self.path else None,
            "message": self.message,
            "basis": self.basis,
        }
