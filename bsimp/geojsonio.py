"""GeoJSON reading/writing with ring normalisation.

Input rings may carry duplicated closing vertices (the fixture material does);
they are normalised here.  All polygons are oriented exterior-CCW /
interiors-CW so that, walking any ring, the face interior is always on the
left.  No coordinate transformation is performed (data is already planar).
"""
from __future__ import annotations

import json

from shapely.geometry import Polygon
from shapely.geometry.polygon import orient


def _norm_ring(coords, eps):
    pts = [(float(p[0]), float(p[1])) for p in coords]
    out = []
    for p in pts:
        if not out or (p[0] - out[-1][0]) ** 2 + (p[1] - out[-1][1]) ** 2 > eps * eps:
            out.append(p)
    while len(out) > 1 and (out[0][0] - out[-1][0]) ** 2 + (
        out[0][1] - out[-1][1]
    ) ** 2 <= eps * eps:
        out.pop()
    if len(out) < 3:
        raise ValueError("ring with fewer than 3 distinct vertices")
    return out


def load_regions(path, eps=1e-8):
    """Read a FeatureCollection of Polygons -> {region_id: shapely Polygon}."""
    with open(path) as f:
        data = json.load(f)
    if data.get("type") != "FeatureCollection":
        raise ValueError(f"{path}: expected a FeatureCollection")
    faces = {}
    for feat in data["features"]:
        name = str(feat["properties"]["region_id"])
        geom = feat["geometry"]
        if geom["type"] != "Polygon":
            raise ValueError(f"region {name}: only Polygon geometry is supported")
        rings = [_norm_ring(r, eps) for r in geom["coordinates"]]
        poly = orient(Polygon(rings[0], rings[1:]), sign=1.0)
        if not poly.is_valid:
            raise ValueError(f"region {name}: input polygon is not valid")
        if name in faces:
            raise ValueError(f"duplicate region_id {name!r}")
        faces[name] = poly
    return faces


def ring_coords(poly):
    """[[exterior pts w/o closing dup], [interior pts ...], ...]"""
    rings = [list(poly.exterior.coords)[:-1]]
    rings += [list(r.coords)[:-1] for r in poly.interiors]
    return [[(float(x), float(y)) for x, y in r] for r in rings]


def count_vertices(faces):
    """Vertices as stored in the file (shared boundaries counted per face)."""
    return sum(len(r) for p in faces.values() for r in ring_coords(p))


def dump_regions(faces, path, props=None):
    feats = []
    for name, poly in faces.items():
        coords = [
            [[x, y] for x, y in list(poly.exterior.coords)],
            *[[[x, y] for x, y in list(r.coords)] for r in poly.interiors],
        ]
        p = {"region_id": name}
        if props and name in props:
            p.update(props[name])
        feats.append(
            {
                "type": "Feature",
                "properties": p,
                "geometry": {"type": "Polygon", "coordinates": coords},
            }
        )
    with open(path, "w") as f:
        json.dump({"type": "FeatureCollection", "features": feats}, f, indent=1)
