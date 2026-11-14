"""预检边界：计数守恒、9 原子/同元素 6 上限、不支持结构直接报错。"""

from __future__ import annotations

import pytest

from map_reaction.errors import PrecheckError, UnsupportedFixtureError
from map_reaction.precheck import (MAX_ATOMS_SAME_ELEMENT,
                                   MAX_NONHYDROGEN_ATOMS, precheck)
from map_reaction.reader import parse_rxn

from tests.test_tiny_reactions import _mol_block, _rxn


def _block_n(name: str, symbol: str, n: int) -> str:
    return _mol_block(name, [(symbol, 0)] * n, [])


def test_too_many_heavy_atoms(tmp_path):
    left = _block_n("big_l", "C", MAX_NONHYDROGEN_ATOMS + 1)
    right = _block_n("big_r", "C", MAX_NONHYDROGEN_ATOMS + 1)
    reaction = parse_rxn(_rxn(tmp_path, "toobig", [left], [right]))
    with pytest.raises(PrecheckError, match="重原子数"):
        precheck(reaction)


def test_too_many_same_element(tmp_path):
    n = MAX_ATOMS_SAME_ELEMENT + 1
    left = _block_n("many_l", "O", n)
    right = _block_n("many_r", "O", n)
    reaction = parse_rxn(_rxn(tmp_path, "manyo", [left], [right]))
    with pytest.raises(PrecheckError, match="同元素上限"):
        precheck(reaction)


def test_isotope_count_must_conserve(tmp_path):
    left = _mol_block("iso_l", [("C", 13), ("C", 0)], [])
    right = _mol_block("iso_r", [("C", 13), ("C", 13)], [])
    reaction = parse_rxn(_rxn(tmp_path, "isocons", [left], [right]))
    with pytest.raises(PrecheckError, match="同位素"):
        precheck(reaction)


def test_element_mismatch_message_lists_diff(tmp_path):
    left = _mol_block("n_l", [("N", 0)], [])
    right = _mol_block("o_r", [("O", 0)], [])
    reaction = parse_rxn(_rxn(tmp_path, "elem", [left], [right]))
    with pytest.raises(PrecheckError) as exc:
        precheck(reaction)
    message = str(exc.value)
    assert "缺少" in message and "多出" in message


def test_explicit_hydrogen_rejected(tmp_path):
    block = (
        "with_h\n     RDKit          2D\n\n"
        "  2  1  0  0  0  0  0  0  0  0999 V2000\n"
        "    0.0000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0\n"
        "    1.0000    0.0000    0.0000 H   0  0  0  0  0  0  0  0  0  0  0  0\n"
        "  1  2  1  0\nM  END\n"
    )
    with pytest.raises(UnsupportedFixtureError, match="显式氢"):
        parse_rxn(_rxn(tmp_path, "exph", [block], [block]))


def test_aromatic_bond_rejected(tmp_path):
    # V2000 键型 4 = aromatic
    block = (
        "arom\n     RDKit          2D\n\n"
        "  2  1  0  0  0  0  0  0  0  0999 V2000\n"
        "    0.0000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0\n"
        "    1.0000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0\n"
        "  1  2  4  0\nM  END\n"
    )
    with pytest.raises(UnsupportedFixtureError, match="芳香"):
        parse_rxn(_rxn(tmp_path, "arom", [block], [block]))


def test_at_least_one_atom_present_after_failed_precheck(tmp_path):
    """预检失败也不能先丢原子：异常后模型仍可访问全部输入原子。"""
    left = _mol_block("n_l", [("N", 0), ("N", 0)], [])
    right = _mol_block("o_r", [("O", 0), ("O", 0)], [])
    reaction = parse_rxn(_rxn(tmp_path, "keep", [left], [right]))
    with pytest.raises(PrecheckError):
        precheck(reaction)
    assert len(reaction.atoms("reactant")) == 2
    assert len(reaction.atoms("product")) == 2
    assert {a.symbol for a in reaction.atoms("reactant")} == {"N"}
