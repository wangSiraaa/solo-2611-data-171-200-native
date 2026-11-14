"""教学级 reaction atom-mapping 工具包。

只依赖 RDKit 完成 RXN/SDF 读写与图属性查询；映射求解本身在
:mod:`map_reaction.solver` 中以带约束的图匹配（分支定界 DFS）实现，
不调用任何现成的 reaction atom mapper，也不使用化学打分模型。
"""

from .errors import PrecheckError, UnsupportedFixtureError

__all__ = ["PrecheckError", "UnsupportedFixtureError"]
