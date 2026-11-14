"""受限 PostgreSQL -> SQLite 查询编译器（题 179）。

公共入口：
    compile_query  / compile_report   —— 编译阶段
    verify_report                     —— 双库执行与核对阶段

本包不依赖任何现成 transpile：词法分析、语法分析、类型推断、SQLite 代码生成
均为本仓库手写实现；sqlglot 只在测试里充当独立的解析交叉校验。
"""

from .compiler import compile_query, compile_report, CompiledQuery
from .errors import CompileError
from .verifier import verify_report

__all__ = [
    "compile_query",
    "compile_report",
    "verify_report",
    "CompiledQuery",
    "CompileError",
]
