"""Arc simplification with domain-wide constraints and rollback.

One geometry is produced per shared arc and fitted back into both adjoining
regions.  A candidate polyline is accepted only if ALL of the following hold
(every check is against the *current* state of the whole graph, never against
one isolated polygon):

1. simple            -- candidate LineString has no self-intersection;
2. deviation         -- symmetric Hausdorff (vertices + 16 samples/segment,
                        both directions) between original and candidate arc
                        stays within the arc tolerance;
3. no crossing       -- candidate meets every other arc / boundary loop only
                        at its own endpoints;
4. node clearance    -- candidate keeps away from every foreign node
                        (junction / point-contact guard);
5. swept area empty  -- the lobes between original and candidate contain no
                        foreign node: this is what stops a chord from moving
                        a protected hole (or any junction) into another face
                        even when the chord itself crosses nothing;
6. faces survive     -- both adjoining regions reassemble to valid polygons
                        with positive area.

On any failure the tolerance is halved and retried; if every retry fails the
original arc is kept (local rollback) instead of accepting a globally
inconsistent result.
"""
from __future__ import annotations

from shapely.geometry import LineString, Point, Polygon
from shapely.ops import polygonize, unary_union

from .geojsonio import count_vertices

# --------------------------------------------------------------------- #
# geometry helpers


def douglas_peucker(coords, tol):
    """Classic Douglas-Peucker; returns kept coordinate list (endpoints fixed)."""
    n = len(coords)
    if n <= 2:
        return list(coords)
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        (ax, ay), (bx, by) = coords[i], coords[j]
        dx, dy = bx - ax, by - ay
        l2 = dx * dx + dy * dy
        best, bi = -1.0, None
        for k in range(i + 1, j):
            px, py = coords[k]
            if l2 == 0.0:
                d2 = (px - ax) ** 2 + (py - ay) ** 2
            else:
                t = ((px - ax) * dx + (py - ay) * dy) / l2
                t = min(1.0, max(0.0, t))
                d2 = (px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2
            if d2 > best:
                best, bi = d2, k
        if best > tol * tol:
            keep[bi] = True
            stack.append((i, bi))
            stack.append((bi, j))
    return [c for c, k in zip(coords, keep) if k]


def _sampled_points(coords, per_seg):
    pts = list(coords)
    for (x1, y1), (x2, y2) in zip(coords, coords[1:]):
        for s in range(1, per_seg):
            t = s / per_seg
            pts.append((x1 + t * (x2 - x1), y1 + t * (y2 - y1)))
    return pts


def symmetric_hausdorff(coords_a, coords_b, per_seg=16):
    """Max of both directed distances, sampling every segment (not just
    retained vertices): the error metric required by the plan."""
    la, lb = LineString(coords_a), LineString(coords_b)

    def directed(la, lb, coords):
        return max(Point(p).distance(lb) for p in _sampled_points(coords, per_seg))

    return max(directed(la, lb, coords_a), directed(lb, la, coords_b))


def _iter_coords(geom):
    if geom.is_empty:
        return
    if hasattr(geom, "coords"):
        yield from geom.coords
    elif hasattr(geom, "geoms"):
        for g in geom.geoms:
            yield from _iter_coords(g)


# --------------------------------------------------------------------- #
# simplifier


class Simplifier:
    def __init__(self, graph, faces, plan):
        self.g = graph
        self.faces = faces
        self.plan = plan
        self.topo_eps = plan.get("numeric_tolerance_m", 1e-8)
        self.area_eps = plan.get("area_tolerance_m2", 1e-7)
        self.clearance = plan.get(
            "node_clearance_m", max(1e-6, 100.0 * self.topo_eps)
        )
        self.default_tol = plan["max_symmetric_hausdorff_m"]
        self.retry_factor = plan.get("retry_factor", 0.5)
        self.max_retries = plan.get("max_retries", 12)
        self.overrides = plan.get("arc_overrides", {})
        self.evidence = []

    # ------------------------------------------------------------------ #
    def tolerance_for(self, arc):
        ov = self.overrides.get(arc.aid) or self.overrides.get("|".join(arc.pair))
        return float(ov["tolerance"]) if ov else float(self.default_tol)

    def run(self):
        # deterministic order: longest (most vertices) arcs first
        order = sorted(self.g.arcs, key=lambda a: (-len(a.nodes), a.aid))
        for arc in order:
            self._simplify_arc(arc)
        faces_out = {name: self._build_face(name) for name in self.faces}
        self._global_sanity(faces_out)
        return faces_out, self.evidence

    # ------------------------------------------------------------------ #
    def _simplify_arc(self, arc):
        ev = {
            "arc_id": arc.aid,
            "faces": list(arc.pair),
            "left_face": arc.left_face,
            "right_face": arc.right_face,
            "traversal": self._traversal(arc),
            "start_node": arc.orig[0],
            "end_node": arc.orig[-1],
            "n_vertices_orig": len(arc.orig),
            "tolerance": self.tolerance_for(arc),
            "attempts": [],
        }
        self.evidence.append(ev)
        if len(arc.orig) <= 2:
            ev.update(
                status="kept", reason="already minimal (2 nodes)",
                n_vertices_new=len(arc.orig), error=0.0,
            )
            return
        if arc.closed:
            ev.update(
                status="kept", reason="closed-loop arc not simplified",
                n_vertices_new=len(arc.orig), error=0.0,
            )
            return
        tol = self.tolerance_for(arc)
        for attempt in range(self.max_retries):
            cand = douglas_peucker(arc.orig, tol)
            if len(cand) == len(arc.orig):
                ev["attempts"].append(
                    {"tolerance": tol, "result": "no removable vertices"}
                )
                break
            ok, reason = self._check(arc, cand, tol)
            ev["attempts"].append(
                {
                    "tolerance": tol,
                    "n_vertices": len(cand),
                    "result": "accepted" if ok else "rejected",
                    **({} if ok else {"reason": reason}),
                }
            )
            if ok:
                arc.coords = cand
                ev.update(
                    status="simplified",
                    n_vertices_new=len(cand),
                    error=symmetric_hausdorff(arc.orig, cand),
                )
                return
            tol *= self.retry_factor
        ev.update(
            status="kept",
            reason="all candidates rejected; original arc kept "
            f"(last: {ev['attempts'][-1].get('reason', 'no removable vertices')})",
            n_vertices_new=len(arc.orig),
            error=0.0,
        )

    # ------------------------------------------------------------------ #
    def _check(self, arc, cand, tol):
        cand_line = LineString(cand)
        # 1. self-intersection
        if not cand_line.is_simple:
            return False, "candidate polyline self-intersects"
        # 2. deviation (symmetric, sampled)
        err = symmetric_hausdorff(arc.orig, cand)
        if err > tol * (1 + 1e-9) + 1e-12:
            return False, f"symmetric Hausdorff {err:.6g} exceeds tolerance {tol:.6g}"
        # 3. crossing other arcs / fixed loops
        ends = [Point(cand[0]), Point(cand[-1])]
        end_ok = 10 * self.topo_eps
        others = [(a.aid, LineString(a.coords)) for a in self.g.arcs if a is not arc]
        others += [
            (f"fixed:{loop['kind']}", LineString(loop["coords"] + loop["coords"][:1]))
            for loop in self.g.boundary_loops
        ]
        for name, other in others:
            inter = cand_line.intersection(other)
            for x, y in _iter_coords(inter):
                if min(Point(x, y).distance(e) for e in ends) > end_ok:
                    return False, f"crosses/touches {name} at ({x:.6g},{y:.6g})"
        # 4. node clearance (junction / point-contact guard)
        own = set(arc.nodes)
        for nid, (nx, ny) in self.g.nodes.items():
            if nid in own:
                continue
            d = cand_line.distance(Point(nx, ny))
            if d < self.clearance:
                return False, (
                    f"passes within {d:.6g} of node ({nx:.6g},{ny:.6g}); "
                    "junction/point-contact guard"
                )
        # 5. swept-area check: lobes between original and candidate must not
        #    contain any foreign node (protected hole vertices, junctions...)
        orig_line = LineString(arc.orig)
        merged = unary_union([orig_line, cand_line])
        for lobe in polygonize(merged):
            if lobe.area <= self.area_eps:
                continue
            boundary = lobe.boundary
            for nid, (nx, ny) in self.g.nodes.items():
                if nid in own:
                    continue
                p = Point(nx, ny)
                if lobe.covers(p) and boundary.distance(p) > 10 * self.topo_eps:
                    return False, (
                        f"swept region contains node ({nx:.6g},{ny:.6g}); "
                        "a hole/junction would change containing region"
                    )
        # 6. both adjoining faces survive
        for fname in arc.pair:
            poly = self._build_face(fname, override={arc.aid: cand})
            if not poly.is_valid or poly.area <= self.area_eps:
                return False, f"region {fname} invalid or collapsed after reassembly"
        return True, ""

    # ------------------------------------------------------------------ #
    def _traversal(self, arc):
        """Which way each face's ring walks the arc: forward or reversed."""
        pos = {nid: i for i, nid in enumerate(arc.nodes)}
        out = {}
        for face in arc.pair:
            for ring in self.g.face_rings[face]:
                hits = [
                    (i, key)
                    for i, (key, _fwd) in enumerate(ring)
                    if self.g.edge_to_arc.get(key) is arc
                ]
                if hits:
                    i, key = hits[0]
                    fwd = ring[i][1]
                    a, b = (key[0], key[1]) if fwd else (key[1], key[0])
                    out[face] = "forward" if pos[a] < pos[b] else "reversed"
                    break
        return out

    # ------------------------------------------------------------------ #
    def _build_face(self, name, override=None):
        override = override or {}
        rings = []
        for ring in self.g.face_rings[name]:
            pts = []
            i = 0
            while i < len(ring):
                key, fwd = ring[i]
                arc = self.g.edge_to_arc.get(key)
                if arc is None:  # fixed boundary edge: original geometry
                    pa, pb = self.g.nodes[key[0]], self.g.nodes[key[1]]
                    seg = [pa, pb] if fwd else [pb, pa]
                    pts.extend(seg if not pts else seg[1:])
                    i += 1
                    continue
                j = i
                while j < len(ring) and self.g.edge_to_arc.get(ring[j][0]) is arc:
                    j += 1
                coords = override.get(arc.aid, arc.coords)
                pos = {nid: k for k, nid in enumerate(arc.nodes)}
                a, b = (key[0], key[1]) if fwd else (key[1], key[0])
                seg = coords if pos[a] < pos[b] else coords[::-1]
                pts.extend(seg if not pts else seg[1:])
                i = j
            # drop consecutive duplicates, close
            clean = []
            for p in pts:
                if not clean or p != clean[-1]:
                    clean.append(p)
            if clean[0] != clean[-1]:
                clean.append(clean[0])
            rings.append(clean)
        return Polygon(rings[0], rings[1:])

    # ------------------------------------------------------------------ #
    def _global_sanity(self, faces_out):
        """Cheap in-process guard; the authoritative check is `verify`."""
        u_in = unary_union(list(self.faces.values()))
        u_out = unary_union(list(faces_out.values()))
        diff = u_in.symmetric_difference(u_out).area
        if diff > self.area_eps:
            raise RuntimeError(
                f"domain coverage changed by {diff:.6g} m^2 after simplification"
            )
        overlap = sum(p.area for p in faces_out.values()) - u_out.area
        if overlap > self.area_eps:
            raise RuntimeError(f"internal overlap of {overlap:.6g} m^2 introduced")


# --------------------------------------------------------------------- #
def vertex_totals(faces):
    return count_vertices(faces)
