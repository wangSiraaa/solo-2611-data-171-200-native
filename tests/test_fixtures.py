"""真实 fixture：读取、预检、求解、写回、独立核验的端到端测试。"""

from __future__ import annotations

import json
import os

import pytest

from map_reaction.cli import run_mapping
from map_reaction.errors import PrecheckError
from map_reaction.precheck import precheck
from map_reaction.reader import parse_rxn, parse_sdf
from map_reaction.solver import objective_value, solve
from map_reaction.verifier import verify_files, verify_sdf
from tests.brute import brute_force_optimum

FIXTURES = os.path.join(os.path.dirname(os.path.dirname(__file__)), "fixtures")

CONSERVED = [
    "hydrolysis",
    "oxidation",
    "isotope",
    "substitution",
    "symmetric",
    "hydrolysis-permuted",
]

# 各案例的人工核对口径（重原子层面）
EXPECTED = {
    "oxidation": {"cost": 1, "n_changes": 1, "types": {"order_changed"}},
    "isotope": {"cost": 1, "n_changes": 1, "types": {"order_changed"}},
    "symmetric": {"cost": 1, "n_changes": 1, "types": {"order_changed"},
                  "ambiguous": True},
    "substitution": {"cost": 2, "n_changes": 2,
                     "types": {"bond_broken", "bond_formed"}},
    "hydrolysis": {"cost": 2, "n_changes": 2,
                   "types": {"bond_broken", "bond_formed"},
                   "ambiguous": True},
    "hydrolysis-permuted": {"cost": 2, "n_changes": 2,
                            "types": {"bond_broken", "bond_formed"},
                            "ambiguous": True},
}


@pytest.mark.parametrize("name", CONSERVED)
def test_end_to_end(name, tmp_path):
    rxn_path = os.path.join(FIXTURES, f"{name}.rxn")
    reaction = parse_rxn(rxn_path)
    report = precheck(reaction)
    assert report["n_heavy_atoms"]["reactant"] <= 9
    assert report["n_heavy_atoms"]["product"] <= 9

    main, alternate, n_optimal = solve(reaction)
    # 与独立暴力枚举完全一致
    brute_cost, brute_n = brute_force_optimum(reaction)
    assert main.cost == brute_cost
    assert objective_value(reaction, main) == main.cost
    if alternate is not None:
        assert objective_value(reaction, alternate) == main.cost

    expected = EXPECTED[name]
    assert main.cost == expected["cost"]
    assert isinstance(report, dict) and report["note"]

    out_dir = tmp_path / name
    full_report = run_mapping(rxn_path, str(out_dir))
    assert full_report["cost"] == expected["cost"]
    changes = full_report["bond_changes"]
    assert len(changes) == expected["n_changes"]
    assert {c["type"] for c in changes} == expected["types"]
    assert full_report["ambiguous"] == expected.get("ambiguous", False)

    # 独立核验入口重新读回实际文件
    mapped_rxn = out_dir / f"{name}.mapped.rxn"
    mapped_sdf = out_dir / f"{name}.mapped.sdf"
    check = verify_files(reaction, str(mapped_rxn), main.cost)
    assert check["matches_reported_cost"]
    assert check["map_numbers_1_to_n_each_side"]
    sdf_check = verify_sdf(reaction, str(mapped_sdf))
    assert sdf_check["map_numbers_1_to_n_each_side"]

    # 报告 JSON 可独立解析且关键字段齐全
    saved = json.loads((out_dir / f"{name}.report.json").read_text(
        encoding="utf-8"))
    assert saved["verification"]["rxn"]["recomputed_objective"] == main.cost
    assert len(saved["atoms"]) == report["n_heavy_atoms"]["reactant"]


def test_nonconserved_rejected():
    reaction = parse_rxn(os.path.join(FIXTURES, "nonconserved.rxn"))
    with pytest.raises(PrecheckError) as exc:
        precheck(reaction)
    assert "不守恒" in str(exc.value)
    # 原子没有被丢弃：两侧原子都还在模型里
    assert len(reaction.atoms("reactant")) == 3
    assert len(reaction.atoms("product")) == 3


def test_nonconserved_cli_exit_code(tmp_path):
    rc = _run_cli([
        "map", os.path.join(FIXTURES, "nonconserved.rxn"),
        "--out", str(tmp_path)])
    assert rc == 2


def _run_cli(argv):
    from map_reaction.cli import main
    return main(argv)


def test_sdf_aux_matches_rxn_components():
    for name in ("hydrolysis", "substitution", "symmetric", "isotope",
                 "oxidation"):
        reaction = parse_rxn(os.path.join(FIXTURES, f"{name}.rxn"))
        sdf_mols = parse_sdf(os.path.join(FIXTURES, f"{name}.sdf"))
        flat_rxn = [(m.side, m.molecule_index, len(m.atoms), len(m.edges),
                     m.name)
                    for side in ("reactant", "product")
                    for m in reaction.molecules(side)]
        flat_sdf = [(m.side, m.molecule_index, len(m.atoms), len(m.edges),
                     m.name) for m in sdf_mols]
        assert flat_rxn == flat_sdf
