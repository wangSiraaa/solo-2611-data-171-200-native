"""SVG overlay: original vs simplified boundaries, nodes and fixed loops."""
from __future__ import annotations

from .graph import qkey


def _fmt(v):
    return f"{v:.2f}".rstrip("0").rstrip(".")


def write_overlay(path, gin, gout, evidence):
    xs, ys = [], []
    for _nid, (x, y) in gin.nodes.items():
        xs.append(x)
        ys.append(y)
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    margin, scale = 60.0, 720.0 / max(maxx - minx, 1e-9)
    W = margin * 2 + (maxx - minx) * scale
    H = margin * 2 + (maxy - miny) * scale

    def tr(p):
        x, y = p
        return (margin + (x - minx) * scale, margin + (maxy - y) * scale)

    def path_of(coords):
        pts = [tr(p) for p in coords]
        return "M" + " L".join(f"{_fmt(x)} {_fmt(y)}" for x, y in pts)

    status = {e["arc_id"]: e["status"] for e in evidence}
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_fmt(W)}" height="{_fmt(H)}" '
        f'viewBox="0 0 {_fmt(W)} {_fmt(H)}">',
        '<rect width="100%" height="100%" fill="white"/>',
    ]
    # hole fill
    for loop in gin.boundary_loops:
        if loop["kind"] == "hole":
            d = path_of(loop["coords"] + loop["coords"][:1]) + " Z"
            parts.append(f'<path d="{d}" fill="#fde7e9" stroke="none"/>')
    # original shared arcs (underlay)
    for arc in gin.arcs:
        parts.append(
            f'<path d="{path_of(arc.orig)}" fill="none" stroke="#9aa0a6" '
            f'stroke-width="3.2" stroke-linecap="round"/>'
        )
    # fixed boundary loops
    for loop in gout.boundary_loops:
        d = path_of(loop["coords"] + loop["coords"][:1])
        parts.append(
            f'<path d="{d}" fill="none" stroke="#188038" stroke-width="1.6"/>'
        )
    # simplified shared arcs
    for arc in gout.arcs:
        st = status.get(arc.aid, "kept")
        colour = "#1a73e8" if st == "simplified" else "#f29900"
        dash = "" if st == "simplified" else ' stroke-dasharray="5 3"'
        parts.append(
            f'<path d="{path_of(arc.coords)}" fill="none" stroke="{colour}" '
            f'stroke-width="1.4"{dash}/>'
        )
    # removed vertices (grey open circles)
    kept = set()
    for arc in gout.arcs:
        kept |= {qkey(*c) for c in arc.coords}
    for arc in gin.arcs:
        for c in arc.orig:
            if qkey(*c) not in kept:
                x, y = tr(c)
                parts.append(
                    f'<circle cx="{_fmt(x)}" cy="{_fmt(y)}" r="2.4" fill="none" '
                    f'stroke="#9aa0a6" stroke-width="1"/>'
                )
    # junction nodes (red squares)
    for nid in gin.break_nodes():
        x, y = tr(gin.nodes[nid])
        parts.append(
            f'<rect x="{_fmt(x - 3)}" y="{_fmt(y - 3)}" width="6" height="6" '
            f'fill="#d93025"/>'
        )
    # arc labels at original midpoint
    for arc in gin.arcs:
        mx, my = tr(arc.orig[len(arc.orig) // 2])
        parts.append(
            f'<text x="{_fmt(mx + 4)}" y="{_fmt(my - 4)}" font-size="10" '
            f'font-family="monospace" fill="#5f6368">{arc.aid}</text>'
        )
    # legend
    lx, ly = 12, 16
    legend = [
        ("#9aa0a6", 3.2, "original shared arc"),
        ("#1a73e8", 1.4, "simplified arc (one geometry, both regions)"),
        ("#f29900", 1.4, "arc kept (rollback)"),
        ("#188038", 1.6, "fixed outer / hole boundary"),
        ("#d93025", 0, "junction / T node (fixed)"),
        ("#9aa0a6", 0, "removed vertex"),
    ]
    parts.append(
        f'<rect x="{lx - 6}" y="{ly - 12}" width="300" height="{14 * len(legend) + 10}" '
        f'fill="white" stroke="#dadce0"/>'
    )
    for i, (c, w, label) in enumerate(legend):
        y = ly + i * 14
        if w:
            parts.append(
                f'<line x1="{lx}" y1="{y}" x2="{lx + 22}" y2="{y}" stroke="{c}" '
                f'stroke-width="{w}"/>'
            )
        elif c == "#d93025":
            parts.append(f'<rect x="{lx + 8}" y="{y - 3}" width="6" height="6" fill="{c}"/>')
        else:
            parts.append(
                f'<circle cx="{lx + 11}" cy="{y}" r="2.4" fill="none" stroke="{c}"/>'
            )
        parts.append(
            f'<text x="{lx + 28}" y="{y + 3}" font-size="10" font-family="monospace" '
            f'fill="#3c4043">{label}</text>'
        )
    parts.append("</svg>")
    with open(path, "w") as f:
        f.write("\n".join(parts))
