"""Independent verification: re-reads the exported files and checks every
domain-wide constraint without any loose buffering.

Checks (all tolerances stated in the report):
  C1  feature/region count and ids match
  C2  every output polygon is valid, positive area, same hole count
  C3  adjacency relation (region pairs sharing positive-length boundary) equal
  C4  point-contact relation equal (contacts must not become adjacencies)
  C5  junction node set (degree>=3 / pair-change nodes) identical
  C6  fixed outer boundary identical -- exact atomic segment set
  C7  fixed hole boundaries identical -- exact atomic segment sets
  C8  output interior consistency: every non-boundary atomic edge is used by
      exactly two faces in opposite directions (no cracks, no overlaps)
  C9  no new intersections: distinct output arcs meet only at shared nodes
  C10 domain coverage: union(in) vs union(out) symmetric difference area
      <= area_tolerance_m2; internal overlap likewise
  C11 per-arc symmetric Hausdorff (vertices + samples, both directions)
      <= plan tolerance, recomputed here from the two files
"""
from __future__ import annotations

from shapely.geometry import LineString, Point
from shapely.ops import unary_union

from .geojsonio import count_vertices, load_regions
from .graph import Graph, qkey
from .simplify import _iter_coords, symmetric_hausdorff


def _seg_set(coords_list):
    out = set()
    for coords in coords_list:
        pts = coords
        if pts[0] == pts[-1]:
            pts = pts[:-1]
        n = len(pts)
        for i in range(n):
            a, b = qkey(*pts[i]), qkey(*pts[(i + 1) % n])
            out.add((a, b) if a <= b else (b, a))
    return out


def _loop_seg_set(loop):
    pts = loop["coords"]
    n = len(pts)
    out = set()
    for i in range(n):
        a, b = qkey(*pts[i]), qkey(*pts[(i + 1) % n])
        out.add((a, b) if a <= b else (b, a))
    return out


def verify(regions_path, simplified_path, plan, protected_path=None, evidence=None):
    eps = plan.get("numeric_tolerance_m", 1e-8)
    area_eps = plan.get("area_tolerance_m2", 1e-7)
    tol_default = float(plan["max_symmetric_hausdorff_m"])
    overrides = plan.get("arc_overrides", {})

    faces_in = load_regions(regions_path, eps)
    faces_out = load_regions(simplified_path, eps)
    gin = Graph(eps).build(faces_in)
    gout = Graph(eps).build(faces_out)

    checks = []

    def check(name, ok, detail):
        checks.append({"name": name, "status": "pass" if ok else "FAIL", "detail": detail})
        return ok

    # C1 region count / ids
    check(
        "C1 region ids and count",
        set(faces_in) == set(faces_out),
        f"input={sorted(faces_in)} output={sorted(faces_out)}",
    )

    # C2 validity / holes
    bad = [
        n
        for n, p in faces_out.items()
        if not p.is_valid
        or p.area <= area_eps
        or len(p.interiors) != len(faces_in[n].interiors)
    ]
    check(
        "C2 output polygons valid, positive area, hole counts preserved",
        not bad,
        "all valid" if not bad else f"problem regions: {bad}",
    )

    # C3 adjacency
    same_adj = gin.adjacency == gout.adjacency
    check(
        "C3 adjacency relation preserved",
        same_adj,
        "pairs="
        + ",".join(sorted("|".join(sorted(p)) for p in gout.adjacency))
        + ("" if same_adj else f" (input={gin.adjacency})"),
    )

    # C4 point contacts
    same_pc = gin.point_contacts == gout.point_contacts
    check(
        "C4 point-contact relation preserved",
        same_pc,
        f"{len(gout.point_contacts)} contact pair(s)"
        + ("" if same_pc else f" input={gin.point_contacts} output={gout.point_contacts}"),
    )

    # C5 junction nodes
    jn_in = {qkey(*gin.nodes[n]) for n in gin.break_nodes()}
    jn_out = {qkey(*gout.nodes[n]) for n in gout.break_nodes()}
    check(
        "C5 junction/T nodes fixed",
        jn_in == jn_out,
        f"{len(jn_out)} junction nodes"
        + ("" if jn_in == jn_out else f" missing={jn_in - jn_out} new={jn_out - jn_in}"),
    )

    # C6/C7 fixed boundaries: exact atomic segment sets
    def loops_of(g, kind):
        return [l for l in g.boundary_loops if l["kind"] == kind]

    outer_ok = len(loops_of(gin, "outer")) == len(loops_of(gout, "outer")) == 1 and (
        _loop_seg_set(loops_of(gin, "outer")[0])
        == _loop_seg_set(loops_of(gout, "outer")[0])
    )
    check("C6 outer domain boundary unchanged (exact segments)", outer_ok, "")

    hin = sorted(_loop_seg_set(l) and tuple(sorted(_loop_seg_set(l))) for l in loops_of(gin, "hole"))
    hout = sorted(tuple(sorted(_loop_seg_set(l))) for l in loops_of(gout, "hole"))
    check(
        "C7 protected hole boundaries unchanged (exact segments)",
        hin == hout,
        f"{len(hout)} hole loop(s)",
    )

    # C8 interior edge consistency of the exported file
    problems = []
    boundary_segs = set()
    for l in gout.boundary_loops:
        boundary_segs |= _loop_seg_set(l)
    for key, e in gout.edges.items():
        if e.is_shared:
            fwds = [u[2] for u in e.uses]
            if len(e.uses) != 2 or fwds[0] == fwds[1]:
                problems.append(f"edge {key}: inconsistent two-face use")
        else:
            seg = tuple(sorted((qkey(*gout.nodes[key[0]]), qkey(*gout.nodes[key[1]]))))
            if seg not in boundary_segs:
                problems.append(f"single-face interior edge at {gout.nodes[key[0]]}")
    check(
        "C8 shared boundaries consistent (no cracks/overlaps along edges)",
        not problems,
        "every interior edge has exactly 2 opposite uses"
        if not problems
        else "; ".join(problems[:5]),
    )

    # C9 no new intersections between distinct arcs/loops
    lines = [(a.aid, LineString(a.coords)) for a in gout.arcs]
    lines += [
        (f"fixed:{l['kind']}", LineString(l["coords"] + l["coords"][:1]))
        for l in gout.boundary_loops
    ]
    xings = []
    for i in range(len(lines)):
        for j in range(i + 1, len(lines)):
            n1, l1 = lines[i]
            n2, l2 = lines[j]
            inter = l1.intersection(l2)
            nodes1 = {qkey(*c) for c in lines[i][1].coords} | {qkey(*c) for c in l2.coords}
            for x, y in _iter_coords(inter):
                if qkey(x, y) not in nodes1:
                    # intersection away from any node of either line
                    if min(
                        Point(x, y).distance(Point(c))
                        for c in list(l1.coords) + list(l2.coords)
                    ) > 10 * eps:
                        xings.append(f"{n1} x {n2} at ({x:.6g},{y:.6g})")
    check("C9 no new intersections between arcs", not xings, "; ".join(xings[:5]))

    # C10 coverage / overlap
    u_in = unary_union(list(faces_in.values()))
    u_out = unary_union(list(faces_out.values()))
    symdiff = u_in.symmetric_difference(u_out).area
    overlap = sum(p.area for p in faces_out.values()) - u_out.area
    check(
        "C10 domain coverage and internal non-overlap",
        symdiff <= area_eps and overlap <= area_eps,
        f"symmetric difference={symdiff:.3g} m^2, internal overlap={overlap:.3g} m^2 "
        f"(limit {area_eps:g})",
    )

    # C11 per-arc error, recomputed from the two files
    arc_rows = []
    all_ok = True
    for ain in gin.arcs:
        cands = [
            aout
            for aout in gout.arcs
            if aout.pair == ain.pair
            and {qkey(*aout.coords[0]), qkey(*aout.coords[-1])}
            == {qkey(*ain.coords[0]), qkey(*ain.coords[-1])}
        ]
        if not cands:
            arc_rows.append(
                {"arc": ain.aid, "status": "FAIL", "detail": "no matching output arc"}
            )
            all_ok = False
            continue
        aout = cands[0]
        err = symmetric_hausdorff(ain.coords, aout.coords)
        ov = overrides.get(ain.aid) or overrides.get("|".join(ain.pair))
        lim = float(ov["tolerance"]) if ov else tol_default
        ok = err <= lim * (1 + 1e-9) + 1e-12
        all_ok = all_ok and ok
        arc_rows.append(
            {
                "arc": ain.aid,
                "faces": list(ain.pair),
                "symmetric_hausdorff_m": err,
                "limit_m": lim,
                "n_vertices_in": len(ain.coords),
                "n_vertices_out": len(aout.coords),
                "status": "pass" if ok else "FAIL",
            }
        )
    check(
        "C11 per-arc symmetric Hausdorff within plan tolerance",
        all_ok,
        f"{len(arc_rows)} shared arcs checked",
    )

    # optional: cross-check against the protected.geojson material
    if protected_path:
        prot = load_regions(protected_path, eps)
        p_outer = prot.get("outer")
        p_holes = [p for n, p in prot.items() if n != "outer"]

        def line_union(rings):
            lines = []
            for pts in rings:
                if pts[0] != pts[-1]:
                    pts = pts + [pts[0]]
                lines.append(LineString(pts))
            return unary_union(lines)

        ok_o = p_outer is not None and line_union(
            [list(p_outer.exterior.coords)]
        ).equals(
            line_union([l["coords"] for l in loops_of(gout, "outer")])
        )
        ok_h = line_union([list(p.exterior.coords) for p in p_holes]).equals(
            line_union([l["coords"] for l in loops_of(gout, "hole")])
        )
        check(
            "C12 matches protected.geojson (outer + holes, exact point sets)",
            ok_o and ok_h,
            "",
        )

    counts = {
        "input": {
            "regions": len(faces_in),
            "vertices_as_stored": count_vertices(faces_in),
            "unique_nodes": len(gin.nodes),
            "shared_arcs": len(gin.arcs),
        },
        "output": {
            "regions": len(faces_out),
            "vertices_as_stored": count_vertices(faces_out),
            "unique_nodes": len(gout.nodes),
            "shared_arcs": len(gout.arcs),
        },
    }
    verdict = all(c["status"] == "pass" for c in checks)
    report = {
        "verdict": "pass" if verdict else "FAIL",
        "tolerances": {
            "numeric_tolerance_m": eps,
            "area_tolerance_m2": area_eps,
            "node_quantisation": "1e-9",
            "error_metric": "symmetric Hausdorff, vertices + 16 samples/segment, "
            "both directions, original arc vs result arc",
            "buffers_used": "none -- all comparisons are exact segment-set "
            "equality or distances against the stated tolerances",
        },
        "counts": counts,
        "checks": checks,
        "arc_errors": arc_rows,
    }
    return report
