"""Command line interface: `python -m bsimp simplify|verify ...`."""
from __future__ import annotations

import argparse
import json
import os
import sys

from .geojsonio import count_vertices, dump_regions, load_regions
from .graph import Graph
from .simplify import Simplifier
from .svg import write_overlay
from .verify import verify as run_verify


def _load_plan(path):
    with open(path) as f:
        return json.load(f)


def cmd_simplify(args):
    plan = _load_plan(args.plan)
    eps = plan.get("numeric_tolerance_m", 1e-8)
    faces = load_regions(args.regions, eps)
    graph = Graph(eps).build(faces)
    simp = Simplifier(graph, faces, plan)
    faces_out, evidence = simp.run()

    os.makedirs(args.outdir, exist_ok=True)
    out_geo = os.path.join(args.outdir, "simplified.geojson")
    props = {}
    for name in faces:
        props[name] = {
            "vertices_orig": count_vertices({name: faces[name]}),
            "vertices_new": count_vertices({name: faces_out[name]}),
        }
    dump_regions(faces_out, out_geo, props)

    # independent re-read of what we just wrote, for the overlay + counts
    faces_check = load_regions(out_geo, eps)
    gout = Graph(eps).build(faces_check)
    doc = {
        "input": os.path.abspath(args.regions),
        "plan": plan,
        "totals": {
            "vertices_input": count_vertices(faces),
            "vertices_output": count_vertices(faces_check),
            "unique_nodes_input": len(graph.nodes),
            "unique_nodes_output": len(gout.nodes),
            "shared_arcs": len(graph.arcs),
        },
        "adjacency": sorted("|".join(sorted(p)) for p in graph.adjacency),
        "point_contacts": sorted("|".join(sorted(p)) for p in graph.point_contacts),
        "boundary_loops": [
            {"kind": l["kind"], "vertices": len(l["coords"]), "fixed": True}
            for l in graph.boundary_loops
        ],
        "shared_arcs": evidence,
    }
    ev_path = os.path.join(args.outdir, "evidence.json")
    with open(ev_path, "w") as f:
        json.dump(doc, f, indent=1)
    svg_path = os.path.join(args.outdir, "overlay.svg")
    write_overlay(svg_path, graph, gout, evidence)

    t = doc["totals"]
    print(f"shared arcs: {t['shared_arcs']}")
    print(
        f"vertices as stored: {t['vertices_input']} -> {t['vertices_output']} "
        f"(unique nodes {t['unique_nodes_input']} -> {t['unique_nodes_output']})"
    )
    for e in evidence:
        line = (
            f"  {e['arc_id']:<10} {e['status']:<10} "
            f"{e['n_vertices_orig']:>3} -> {e['n_vertices_new']:<3} "
            f"err={e['error']:.4g} tol={e['tolerance']:.4g}"
        )
        if e["status"] == "kept":
            line += f"  [{e['reason']}]"
        print(line)
    print(f"wrote {out_geo}\nwrote {ev_path}\nwrote {svg_path}")


def cmd_verify(args):
    plan = _load_plan(args.plan)
    evidence = None
    if args.evidence:
        with open(args.evidence) as f:
            evidence = json.load(f)
    report = run_verify(
        args.regions, args.simplified, plan, args.protected, evidence
    )
    text = json.dumps(report, indent=1)
    if args.report:
        with open(args.report, "w") as f:
            f.write(text + "\n")
    for c in report["checks"]:
        print(f"[{c['status']:<4}] {c['name']}" + (f" -- {c['detail']}" if c["detail"] else ""))
    c = report["counts"]
    print(
        f"vertices as stored: {c['input']['vertices_as_stored']} -> "
        f"{c['output']['vertices_as_stored']}; unique nodes "
        f"{c['input']['unique_nodes']} -> {c['output']['unique_nodes']}"
    )
    print(f"verdict: {report['verdict']}")
    if args.report:
        print(f"wrote {args.report}")
    sys.exit(0 if report["verdict"] == "pass" else 1)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="bsimp", description="domain-wide boundary simplification"
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("simplify", help="simplify shared boundary arcs")
    s.add_argument("regions", help="input regions GeoJSON")
    s.add_argument("plan", help="tolerance plan JSON")
    s.add_argument("outdir", help="output directory")
    s.set_defaults(fn=cmd_simplify)

    v = sub.add_parser("verify", help="independently verify a simplified result")
    v.add_argument("regions", help="original regions GeoJSON")
    v.add_argument("simplified", help="simplified regions GeoJSON")
    v.add_argument("plan", help="tolerance plan JSON")
    v.add_argument("--protected", help="protected outer/hole GeoJSON (optional)")
    v.add_argument("--evidence", help="evidence.json from the simplify run (optional)")
    v.add_argument("--report", help="write verification report JSON here")
    v.set_defaults(fn=cmd_verify)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
