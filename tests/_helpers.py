"""测试辅助：内存中构造 .dims/.formula 源并运行 check。"""

from __future__ import annotations

import os
from fractions import Fraction

from dimcheck.report import check
from dimcheck.sources import SourceStore


def write_case(tmp_path, name: str, text: str) -> str:
    path = os.path.join(str(tmp_path), name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def run(tmp_path, files: dict[str, str]):
    store = SourceStore()
    paths = [write_case(tmp_path, name, text) for name, text in files.items()]
    return check(paths, store), store


def vec(result, name: str):
    for var in result.variables:
        if var.get("name") == name:
            return tuple(Fraction(x) for x in var["dimension"])
    raise KeyError(name)


def var_entry(result, name: str) -> dict:
    for var in result.variables:
        if var.get("name") == name:
            return var
    raise KeyError(name)


def meta(result) -> dict:
    return var_entry(result, "__degrees_of_freedom__")
