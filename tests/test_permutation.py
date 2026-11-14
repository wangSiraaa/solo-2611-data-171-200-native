"""原子编号置换不变性：重排输入原子编号后，目标值与化学变化口径不变。

通过 RDKit RenumberAtoms 生成同反应的编号置换 RXN（图、元素、同位素、
电荷、键级完全不变，只改原子出现顺序），重新走完整求解与核验流程。
"""

from __future__ import annotations

import os

from rdkit import Chem

from map_reaction.changes import bond_changes
from map_reaction.cli import run_mapping
from map_reaction.precheck import precheck
from map_reaction.reader import parse_rxn
from map_reaction.solver import solve
from map_reaction.verifier import verify_files
from tests.brute import brute_force_optimum

FIXTURES = os.path.join(os.path.dirname(os.path.dirname(__file__)), "fixtures")


def _permuted_rxn_text(path: str, seed: int) -> str:
    text = open(path, encoding="utf-8").read()
    lines = text.splitlines()
    counts = next(line for line in lines[3:] if line.strip())
    n_r, n_p = int(counts[0:3]), int(counts[3:6])
    blocks = [b.lstrip("\n") for b in text.split("$MOL")[1:]]

    rebuilt = [lines[0], "", lines[2], "",
               f"{n_r:3d}{n_p:3d}"]
    for block_index, block in enumerate(blocks):
        mol = Chem.MolFromMolBlock(block, removeHs=False,
                                   strictParsing=False)
        n = mol.GetNumAtoms()
        # 确定性伪随机置换（不依赖随机库的跨平台稳定性）
        perm = list(range(n))
        for i in range(n - 1, 0, -1):
            j = (seed * (i + 3) + block_index * 7 + 1) % (i + 1)
            perm[i], perm[j] = perm[j], perm[i]
        permuted = Chem.RenumberAtoms(mol, perm)
        new_block = Chem.MolToMolBlock(permuted, includeStereo=False)
        name = mol.GetProp("_Name") if mol.HasProp("_Name") else ""
        new_lines = new_block.splitlines()
        new_lines[0] = name
        rebuilt.append("$MOL")
        rebuilt.extend(new_lines)
    return "\n".join(rebuilt) + "\n"


def _change_signature(reaction, mapping):
    """与文件编号无关的变化口径：(类型, 元素对, 前后键级) 的多重集。"""
    rows = bond_changes(reaction, mapping)
    sig = []
    r_atoms = reaction.atoms("reactant")
    for row in rows:
        pair_symbols = tuple(sorted(
            r_atoms[n - 1].symbol for n in row["map_numbers"]))
        sig.append((row["type"], pair_symbols,
                    row["bond_order_before"], row["bond_order_after"]))
    return sorted(sig)


def test_permutation_invariance(tmp_path):
    for name in ("hydrolysis", "oxidation", "isotope", "substitution",
                 "symmetric"):
        original = parse_rxn(os.path.join(FIXTURES, f"{name}.rxn"))
        precheck(original)
        main_orig, _, _ = solve(original)
        sig_orig = _change_signature(original, main_orig)
        brute_orig, _ = brute_force_optimum(original)

        for seed in (1, 2, 3):
            perm_path = tmp_path / f"{name}.perm{seed}.rxn"
            perm_path.write_text(
                _permuted_rxn_text(
                    os.path.join(FIXTURES, f"{name}.rxn"), seed),
                encoding="utf-8")
            perm_reaction = parse_rxn(str(perm_path))
            precheck(perm_reaction)
            main_perm, _, _ = solve(perm_reaction)

            # 目标值与暴力枚举一致，且与原始文件相同
            brute_perm, _ = brute_force_optimum(perm_reaction)
            assert main_perm.cost == brute_perm == brute_orig == main_orig.cost
            # 化学变化口径（类型/元素/键级）不变
            assert _change_signature(perm_reaction, main_perm) == sig_orig

            # 写回并通过独立读回核验
            report = run_mapping(str(perm_path),
                                 str(tmp_path / f"out{seed}"))
            verify_files(perm_reaction,
                         report["verification"]["rxn"]["mapped_rxn"],
                         main_perm.cost)


def test_shipped_permuted_fixture_matches_original():
    """仓库自带的 hydrolysis-permuted 与 hydrolysis 同口径。"""
    original = parse_rxn(os.path.join(FIXTURES, "hydrolysis.rxn"))
    shipped = parse_rxn(os.path.join(FIXTURES, "hydrolysis-permuted.rxn"))
    main_o, _, _ = solve(original)
    main_p, _, _ = solve(shipped)
    assert main_o.cost == main_p.cost
    assert _change_signature(original, main_o) == \
        _change_signature(shipped, main_p)
    assert brute_force_optimum(original)[0] == \
        brute_force_optimum(shipped)[0]
