"""dimcheck：受限工程文本的量纲一致性检查（纯标准库，Python 3.11）。"""

from __future__ import annotations

from .ast_nodes import (
    Binary,
    Call,
    DeclStmt,
    FormulaStmt,
    Node,
    Number,
    Power,
    Unary,
    Var,
)
from .compiler import CompiledSystem, Compiler, Item, PrimConstraint
from .diagnostics import Diagnostic, DiagnosticBag, Severity
from .dims import parse_dims_file
from .parser import parse_formula_file
from .report import CheckResult, check, explain, to_dict
from .sources import SourceStore, Span
from .solver import ComponentConflict, SolveResult, solve

__all__ = [
    "Binary",
    "Call",
    "CheckResult",
    "CompiledSystem",
    "ComponentConflict",
    "Compiler",
    "DeclStmt",
    "Diagnostic",
    "DiagnosticBag",
    "FormulaStmt",
    "Item",
    "Node",
    "Number",
    "Power",
    "PrimConstraint",
    "Severity",
    "SolveResult",
    "SourceStore",
    "Span",
    "Unary",
    "Var",
    "check",
    "explain",
    "parse_dims_file",
    "parse_formula_file",
    "solve",
    "to_dict",
]
