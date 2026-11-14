"""命令行入口：map / verify 两个子命令。"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .changes import summarize
from .precheck import precheck
from .reader import parse_rxn
from .solver import objective_value, solve
from .verifier import verify_files, verify_sdf
from .writer import write_rxn, write_sdf


def run_mapping(rxn_path: str, output_dir: str) -> dict:
    """读取 -> 预检 -> 求解 -> 写文件 -> 独立核验，返回报告字典。"""
    reaction = parse_rxn(rxn_path)
    precheck_report = precheck(reaction)
    main, alternate, n_optimal = solve(reaction)

    # 求解器自检：按定义独立重算，防止增量计费实现偏差。
    assert objective_value(reaction, main) == main.cost
    if alternate is not None:
        assert objective_value(reaction, alternate) == alternate.cost

    os.makedirs(output_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(rxn_path))[0]
    mapped_rxn = os.path.join(output_dir, f"{stem}.mapped.rxn")
    mapped_sdf = os.path.join(output_dir, f"{stem}.mapped.sdf")
    report_path = os.path.join(output_dir, f"{stem}.report.json")

    write_rxn(reaction, main, mapped_rxn)
    write_sdf(reaction, main, mapped_sdf)

    rxn_check = verify_files(reaction, mapped_rxn, main.cost)
    sdf_check = verify_sdf(reaction, mapped_sdf)
    report = summarize(reaction, main, alternate, n_optimal, precheck_report)
    report["verification"] = {"rxn": rxn_check, "sdf": sdf_check}
    with open(report_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="教学级 RXN atom-map：最小化键级差绝对值之和"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    map_parser = sub.add_parser("map", help="求解并写出 mapped RXN/SDF")
    map_parser.add_argument("rxn")
    map_parser.add_argument("--out", default="output")

    verify_parser = sub.add_parser(
        "verify", help="对已写出的 mapped RXN 做独立读回核验")
    verify_parser.add_argument("rxn", help="原始 RXN")
    verify_parser.add_argument("mapped_rxn")
    verify_parser.add_argument("cost", type=int)

    args = parser.parse_args(argv)
    if args.command == "map":
        try:
            report = run_mapping(args.rxn, args.out)
        except ValueError as exc:
            print(f"[拒绝处理] {exc}", file=sys.stderr)
            return 2
        print(json.dumps({
            "source": report["source_rxn"],
            "cost": report["cost"],
            "ambiguous": report["ambiguous"],
            "n_optimal_mappings_found": report["n_optimal_mappings_found"],
            "bond_changes": report["bond_changes"],
            "verification": report["verification"],
        }, ensure_ascii=False, indent=2))
        return 0

    reaction = parse_rxn(args.rxn)
    precheck(reaction)
    print(json.dumps(
        verify_files(reaction, args.mapped_rxn, args.cost),
        ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
