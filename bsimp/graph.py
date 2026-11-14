"""Unified planar graph: nodes, atomic edges, boundary loops, shared arcs.

Build steps
-----------
1. Every ring vertex of every face becomes a node (quantised at 1e-9, far
   below the plan's numeric tolerance).
2. T-junction split: wherever a node of any ring lies in the interior of
   another ring's segment (distance < topo_eps), the segment is split.
   After this pass, segments meet only at common nodes -- a T-junction is a
   real graph node, never a vertex floating on an edge.
3. Atomic edges are keyed by node-id pairs.  An edge used by two faces is a
   *shared* edge; an edge used by one face belongs to the outer domain
   boundary or to a protected hole loop (both fixed).
4. Shared edges are chained into *arcs* between break nodes.  A node is a
   break node unless exactly two edges meet there and both belong to the
   same face pair -- so degree>=3 junctions, T-junctions and points where
   the neighbouring face pair changes all become fixed arc endpoints.
5. Face relations: two faces are *adjacent* iff they share at least one
   atomic edge (positive-length common boundary).  Two faces that share a
   node but no edge are a *point contact* -- never upgraded to adjacency.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations

from .geojsonio import ring_coords

QUANT = 9  # node quantisation decimals (1e-9 grid)


def qkey(x, y):
    return (round(x, QUANT), round(y, QUANT))


def _pt_seg(px, py, ax, ay, bx, by):
    """Return (squared distance, parameter t) of point to segment."""
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    if l2 == 0.0:
        return (px - ax) ** 2 + (py - ay) ** 2, 0.0
    t = ((px - ax) * dx + (py - ay) * dy) / l2
    tc = min(1.0, max(0.0, t))
    cx, cy = ax + tc * dx, ay + tc * dy
    return (px - cx) ** 2 + (py - cy) ** 2, t


@dataclass
class Edge:
    key: tuple  # (lo_node_id, hi_node_id)
    uses: list = field(default_factory=list)  # (face, ring_idx, fwd)

    @property
    def faces(self):
        return sorted({u[0] for u in self.uses})

    @property
    def is_shared(self):
        return len(self.faces) == 2


@dataclass
class Arc:
    """A maximal chain of shared atomic edges between two break nodes."""

    aid: str
    pair: tuple  # sorted face names
    nodes: list  # ordered node ids, start..end
    edges: list  # atomic edge keys along the chain
    left_face: str = None
    right_face: str = None
    coords: list = None  # current geometry (starts as original)
    orig: list = None

    @property
    def closed(self):
        return self.nodes[0] == self.nodes[-1]


class Graph:
    def __init__(self, topo_eps=1e-8):
        self.topo_eps = topo_eps
        self.nodes = {}  # id -> (x, y)
        self.node_ids = {}  # quantised coord -> id
        self.edges = {}  # (lo, hi) -> Edge
        self.face_rings = {}  # face -> [[(edge_key, fwd), ...], ...] per ring
        self.arcs = []  # shared arcs
        self.edge_to_arc = {}  # edge key -> Arc
        self.boundary_loops = []  # {"kind": outer|hole, "nodes": [...]}
        self.adjacency = set()  # frozenset({f1, f2})
        self.point_contacts = set()  # frozenset({f1, f2})

    # ------------------------------------------------------------------ #
    def node_id(self, x, y):
        k = qkey(x, y)
        nid = self.node_ids.get(k)
        if nid is None:
            nid = len(self.nodes)
            self.node_ids[k] = nid
            self.nodes[nid] = (float(x), float(y))
        return nid

    def build(self, faces):
        rings = {name: ring_coords(p) for name, p in faces.items()}
        # pass 1: register every vertex as a node
        for rlist in rings.values():
            for r in rlist:
                for x, y in r:
                    self.node_id(x, y)
        # pass 2: T-junction split + atomic edges
        for name, rlist in rings.items():
            for ri, r in enumerate(rlist):
                seq = self._split_ring(r)
                ring_seq = []
                for a, b in seq:
                    key = (a, b) if a < b else (b, a)
                    e = self.edges.get(key)
                    if e is None:
                        e = self.edges[key] = Edge(key)
                    e.uses.append((name, ri, a == key[0]))
                    ring_seq.append((key, a == key[0]))
                self.face_rings.setdefault(name, []).append(ring_seq)
        self._chain_boundary_loops()
        self._chain_shared_arcs()
        self._relations()
        return self

    # ------------------------------------------------------------------ #
    def _split_ring(self, ring):
        """Split ring segments at every node lying on them -> directed node pairs."""
        eps2 = self.topo_eps**2
        out = []
        n = len(ring)
        for i in range(n):
            x1, y1 = ring[i]
            x2, y2 = ring[(i + 1) % n]
            ida, idb = self.node_id(x1, y1), self.node_id(x2, y2)
            splits = [(0.0, ida)]
            for nid, (nx, ny) in self.nodes.items():
                if nid in (ida, idb):
                    continue
                d2, t = _pt_seg(nx, ny, x1, y1, x2, y2)
                if d2 < eps2 and 1e-12 < t < 1.0 - 1e-12:
                    splits.append((t, nid))
            splits.append((1.0, idb))
            splits.sort()
            for (_, a), (_, b) in zip(splits, splits[1:]):
                if a != b:
                    out.append((a, b))
        return out

    # ------------------------------------------------------------------ #
    def _incident(self, only_shared=False):
        inc = {nid: [] for nid in self.nodes}
        for key, e in self.edges.items():
            if only_shared and not e.is_shared:
                continue
            inc[key[0]].append(key)
            inc[key[1]].append(key)
        return inc

    @staticmethod
    def _pair_of(edge):
        return tuple(sorted(edge.faces))

    def _chain_boundary_loops(self):
        bedges = [k for k, e in self.edges.items() if not e.is_shared]
        if len(bedges) != sum(len(e.uses) for e in self.edges.values() if not e.is_shared):
            raise ValueError("boundary edge used by more than one face/ring")
        inc = {nid: [] for nid in self.nodes}
        for k in bedges:
            inc[k[0]].append(k)
            inc[k[1]].append(k)
        visited = set()
        loops = []
        for e0 in bedges:
            if e0 in visited:
                continue
            nodes = [e0[0], e0[1]]
            visited.add(e0)
            while nodes[-1] != nodes[0]:
                nxt = [k for k in inc[nodes[-1]] if k not in visited]
                if len(nxt) != 1:
                    raise ValueError("boundary edges do not form clean loops")
                k = nxt[0]
                visited.add(k)
                nodes.append(k[0] if k[1] == nodes[-1] else k[1])
            loops.append(nodes[:-1])
        # classify: largest absolute signed area = outer domain boundary
        def signed_area(nodes):
            pts = [self.nodes[n] for n in nodes]
            return 0.5 * sum(
                x1 * y2 - x2 * y1
                for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1])
            )

        loops.sort(key=lambda ns: -abs(signed_area(ns)))
        for i, ns in enumerate(loops):
            kind = "outer" if i == 0 else "hole"
            if kind == "outer" and signed_area(ns) < 0:
                ns = ns[::-1]
            if kind == "hole" and signed_area(ns) > 0:
                ns = ns[::-1]
            self.boundary_loops.append(
                {"kind": kind, "nodes": ns, "coords": [self.nodes[n] for n in ns]}
            )

    # ------------------------------------------------------------------ #
    def _chain_shared_arcs(self):
        inc_all = self._incident()
        inc_sh = self._incident(only_shared=True)

        def is_break(nid):
            es = inc_all[nid]
            if len(es) != 2:
                return True
            return len({self._pair_of(self.edges[k]) for k in es}) != 1

        visited = set()
        pair_count = {}
        for e0 in [k for k, e in self.edges.items() if e.is_shared]:
            if e0 in visited:
                continue
            pair = self._pair_of(self.edges[e0])
            nodes, ekeys = [e0[0], e0[1]], [e0]
            visited.add(e0)
            # extend forward
            while nodes[-1] != nodes[0] and not is_break(nodes[-1]):
                nxt = [
                    k
                    for k in inc_sh[nodes[-1]]
                    if k not in visited and self._pair_of(self.edges[k]) == pair
                ]
                if len(nxt) != 1:
                    break
                k = nxt[0]
                visited.add(k)
                ekeys.append(k)
                nodes.append(k[0] if k[1] == nodes[-1] else k[1])
            # extend backward
            while nodes[0] != nodes[-1] and not is_break(nodes[0]):
                nxt = [
                    k
                    for k in inc_sh[nodes[0]]
                    if k not in visited and self._pair_of(self.edges[k]) == pair
                ]
                if len(nxt) != 1:
                    break
                k = nxt[0]
                visited.add(k)
                ekeys.insert(0, k)
                nodes.insert(0, k[1] if k[0] == nodes[0] else k[0])
            pair_count[pair] = pair_count.get(pair, 0) + 1
            aid = "|".join(pair)
            if pair_count[pair] > 1:
                aid = f"{aid}#{pair_count[pair]}"
            arc = Arc(aid=aid, pair=pair, nodes=nodes, edges=ekeys)
            arc.coords = [self.nodes[n] for n in nodes]
            arc.orig = list(arc.coords)
            self._set_sides(arc)
            self.arcs.append(arc)
            for k in ekeys:
                self.edge_to_arc[k] = arc
        self.arcs.sort(key=lambda a: a.aid)

    def _set_sides(self, arc):
        """left/right face relative to arc direction (start -> end)."""
        n0, n1 = arc.nodes[0], arc.nodes[1]
        key = (n0, n1) if n0 < n1 else (n1, n0)
        for face, _ri, fwd in self.edges[key].uses:
            goes_forward = fwd == (n0 == key[0])
            # rings are oriented so the face interior is on the left of travel
            if goes_forward:
                arc.left_face = face
            else:
                arc.right_face = face

    # ------------------------------------------------------------------ #
    def _relations(self):
        for arc in self.arcs:
            self.adjacency.add(frozenset(arc.pair))
        inc = self._incident()
        for nid, ekeys in inc.items():
            faces = set()
            for k in ekeys:
                faces.update(self.edges[k].faces)
            for f1, f2 in combinations(sorted(faces), 2):
                if frozenset((f1, f2)) not in self.adjacency:
                    self.point_contacts.add(frozenset((f1, f2)))

    # ------------------------------------------------------------------ #
    def break_nodes(self):
        """Node ids where arcs end (degree != 2 or face-pair change)."""
        inc = self._incident()
        out = set()
        for nid, ekeys in inc.items():
            if len(ekeys) != 2:
                out.add(nid)
            elif len({self._pair_of(self.edges[k]) for k in ekeys}) != 1:
                out.add(nid)
        return out

    def arc_by_pair(self, pair):
        return [a for a in self.arcs if a.pair == tuple(sorted(pair))]
