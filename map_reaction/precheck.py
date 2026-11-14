"""硬性预检与软性属性报告。

硬性范围（不满足直接报错，绝不在报错前丢弃任何原子）：
  * 每侧重原子数不超过 9；同元素每侧不超过 6；
  * 键级只允许 1/2/3、不含显式 H 与芳香键（读取阶段已拦截）；
  * 两侧 (元素, 同位素) 多重集必须严格相等。

隐式氢与形式电荷允许随反应变化，只做 before/after 汇总报告，
不参与可行性判断，也不作为映射不变量。
"""

from __future__ import annotations

from collections import Counter

from .errors import PrecheckError
from .models import Reaction

MAX_NONHYDROGEN_ATOMS = 9
MAX_ATOMS_SAME_ELEMENT = 6


def _invariant_counts(reaction: Reaction, side: str) -> Counter[tuple[int, int]]:
    return Counter(a.invariant for a in reaction.atoms(side))


def _element_counts(reaction: Reaction, side: str) -> Counter[int]:
    return Counter(a.atomic_number for a in reaction.atoms(side))


def precheck(reaction: Reaction) -> dict:
    """校验支持范围与守恒；返回可打印的预检/属性变化报告。"""
    problems: list[str] = []

    for side in ("reactant", "product"):
        atoms = reaction.atoms(side)
        if len(atoms) > MAX_NONHYDROGEN_ATOMS:
            problems.append(
                f"{side} 侧重原子数 {len(atoms)} 超过 "
                f"{MAX_NONHYDROGEN_ATOMS}"
            )
        per_element = _element_counts(reaction, side)
        for atomic_number, count in sorted(per_element.items()):
            if count > MAX_ATOMS_SAME_ELEMENT:
                symbol = next(a.symbol for a in atoms
                              if a.atomic_number == atomic_number)
                problems.append(
                    f"{side} 侧元素 {symbol} 有 {count} 个，超过 "
                    f"同元素上限 {MAX_ATOMS_SAME_ELEMENT}"
                )

    left = _invariant_counts(reaction, "reactant")
    right = _invariant_counts(reaction, "product")
    if left != right:
        missing = left - right
        extra = right - left
        details = []
        for invariant, count in sorted(missing.items()):
            details.append(f"产物侧缺少 {invariant} x{count}")
        for invariant, count in sorted(extra.items()):
            details.append(f"产物侧多出 {invariant} x{count}")
        problems.append("元素/同位素计数不守恒：" + "；".join(details))

    if problems:
        # 不做任何“删掉不认识的原子再继续”的补救。
        raise PrecheckError("；".join(problems))

    return {
        "n_heavy_atoms": {
            "reactant": len(reaction.atoms("reactant")),
            "product": len(reaction.atoms("product")),
        },
        "invariant_counts": {
            "reactant": sorted(left.items()),
            "product": sorted(right.items()),
        },
        "charge_totals": {
            side: sum(a.formal_charge for a in reaction.atoms(side))
            for side in ("reactant", "product")
        },
        "implicit_h_totals": {
            side: sum(a.implicit_hs for a in reaction.atoms(side))
            for side in ("reactant", "product")
        },
        "note": "形式电荷与隐式氢允许反应前后变化，仅报告不作硬匹配",
    }
