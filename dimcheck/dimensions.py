"""四维有理量纲向量 (M, L, T, Theta) 与受限函数表。"""

from __future__ import annotations

from fractions import Fraction

BASES = ("M", "L", "T", "Theta")
DIMENSIONLESS: tuple[Fraction, ...] = (Fraction(0),) * 4

# 函数名 -> (参数个数, 类型)
# "scalar": 输入输出都必须无量纲；"sqrt": 输出维数 = 输入维数 * 1/2。
FUNCTIONS: dict[str, tuple[int, str]] = {
    "exp": (1, "scalar"),
    "log": (1, "scalar"),
    "sin": (1, "scalar"),
    "cos": (1, "scalar"),
    "sqrt": (1, "sqrt"),
}


def zero_vec() -> list[Fraction]:
    return [Fraction(0), Fraction(0), Fraction(0), Fraction(0)]


def is_dimensionless(vec) -> bool:
    return all(c == 0 for c in vec)


def format_fraction(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def format_dimension(vec) -> str:
    """把向量渲染成协议风格的 ``M L^2 T^-2``；零向量渲染为 ``1``。"""
    parts: list[str] = []
    for base, exponent in zip(BASES, vec):
        if exponent == 0:
            continue
        if exponent == 1:
            parts.append(base)
        else:
            parts.append(f"{base}^{format_fraction(Fraction(exponent))}")
    return " ".join(parts) if parts else "1"
