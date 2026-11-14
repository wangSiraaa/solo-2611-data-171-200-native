"""身份格图：独立跟踪每个源格在结构操作后的去向，用于验证。"""

from __future__ import annotations

from .refs import Operation, point_mapper


class IdentityGrid:
    """以源坐标为身份，逐操作更新每个被跟踪格子的当前坐标。

    与引用重写器的区间代数相互独立：这里只做逐点映射，
    用来核对“新引用确实指向对应身份”。
    """

    def __init__(self) -> None:
        # (sheet, r0, c0) -> (r, c) 当前坐标；被删除的身份从此映射中移除
        self.pos: dict[tuple[str, int, int], tuple[int, int]] = {}
        # 所有登记过的身份（含后来被删除的），用于区分“未跟踪”与“已删除”
        self.known: set[tuple[str, int, int]] = set()

    def add(self, sheet: str, r: int, c: int) -> None:
        key = (sheet, r, c)
        self.pos.setdefault(key, (r, c))
        self.known.add(key)

    def apply(self, op: Operation) -> None:
        pmap = point_mapper(op)
        new: dict[tuple[str, int, int], tuple[int, int]] = {}
        for (sheet, r0, c0), (r, c) in self.pos.items():
            if sheet != op.sheet:
                new[(sheet, r0, c0)] = (r, c)
                continue
            nr, nc = (pmap(r), c) if op.axis == "row" else (r, pmap(c))
            if nr is None or nc is None:
                continue                      # 该身份被删除
            new[(sheet, r0, c0)] = (nr, nc)
        self.pos = new

    def forward(self, sheet: str, r: int, c: int) -> tuple[int, int] | None:
        """源格当前坐标；被删除返回 None；未跟踪返回 "untracked" 由调用方处理。"""
        return self.pos.get((sheet, r, c))

    def tracked(self, sheet: str, r: int, c: int) -> bool:
        return (sheet, r, c) in self.known

    def reverse(self) -> dict[tuple[str, int, int], tuple[int, int]]:
        """(sheet, 当前r, 当前c) -> (源r, 源c)。"""
        return {(s, r, c): (r0, c0) for (s, r0, c0), (r, c) in self.pos.items()}
