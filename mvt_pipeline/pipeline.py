"""几何裁切、量化与后处理。

处理顺序（对每个 source feature × tile 完全一致）
--------------------------------------------------
1. 在源 CRS（EPSG:3857 米）内用 shapely 与瓦片裁切框（含整数 buffer）求交；
2. 把交点/顶点按该 zoom 的全局整数网格量化为瓦片局部整数坐标；
3. 去除连续重复点；
4. 线：唯一顶点 < 2 -> 线塌缩，记录并丢弃；
5. 面：逐环处理，零面积环丢弃；外环塌缩 -> 整个片段丢弃；
   孔环塌缩 -> 仅丢该孔并显式记录（绝不静默填实）；
6. 按 MVT（y 向下）方向强制外环顺时针、孔逆时针。

裁切框边缘与全局网格线对齐（buffer 由整数瓦片单位换算），因此相邻瓦片
公共边界上的点量化后得到同一个全局网格整数，不依赖各瓦片的局部偏移。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPolygon,
    Polygon,
)
from shapely.geometry.base import BaseGeometry
from shapely.ops import orient as shapely_orient
from shapely.validation import make_valid

from .clipping import clip_lines, clip_polygon
from .grid import ZoomGrid

SUPPORTED_TYPES = ("LineString", "Polygon")


# --------------------------------------------------------------------------- #
# 输入读取与校验
# --------------------------------------------------------------------------- #
def load_features(geojson: dict[str, Any]) -> list[dict[str, Any]]:
    if geojson.get("type") != "FeatureCollection":
        raise ValueError("只支持 FeatureCollection 输入")
    features = geojson["features"]
    for feat in features:
        if feat.get("type") != "Feature":
            raise ValueError(f"非 Feature 成员: {feat!r}")
        geom = feat.get("geometry") or {}
        gtype = geom.get("type")
        if gtype not in SUPPORTED_TYPES:
            raise ValueError(
                f"feature id={feat.get('id')} 类型 {gtype!r} 不受支持，"
                f"仅支持 {SUPPORTED_TYPES}"
            )
        coords = geom.get("coordinates")
        if gtype == "LineString":
            if len(coords) < 2:
                raise ValueError(f"feature id={feat.get('id')} LineString 顶点不足")
        else:
            if not coords or not all(len(ring) >= 4 for ring in coords):
                raise ValueError(f"feature id={feat.get('id')} Polygon 环结构非法（环需闭合且 >=4 点）")
        fid = feat.get("id")
        if not isinstance(fid, int) or isinstance(fid, bool) or fid < 0:
            raise ValueError(f"feature id 必须是非负整数，得到 {fid!r}")
        _validate_properties(feat.get("properties") or {}, fid)
    return features


def _validate_properties(props: dict[str, Any], fid: int) -> None:
    for key, value in props.items():
        if not isinstance(key, str):
            raise ValueError(f"feature id={fid} 属性名必须是字符串")
        if isinstance(value, bool):
            continue
        if isinstance(value, str):
            continue
        if isinstance(value, int) and value >= 0:
            continue
        raise ValueError(
            f"feature id={fid} 属性 {key!r}={value!r} 类型不受支持："
            "仅允许 string / bool / 非负整数"
        )


# --------------------------------------------------------------------------- #
# 证据记录
# --------------------------------------------------------------------------- #
@dataclass
class QuantPoint:
    x_m: float
    y_m: float
    gx: int
    gy: int
    local_x: int
    local_y: int
    err_x_m: float
    err_y_m: float


@dataclass
class RingEvidence:
    role: str                       # "exterior" | "hole"
    part_index: int
    vertex_count: int
    unique_vertices: int
    area_grid2_before: float        # 量化前（米坐标映射到网格浮点坐标后的）有向面积*
    area_grid2_after: int           # 量化后整数环的有向面积（2 倍）
    dropped: bool
    drop_reason: str | None
    points: list[QuantPoint] = field(default_factory=list)


@dataclass
class LineEvidence:
    part_index: int
    vertex_count: int
    unique_vertices: int
    length_grid: float              # 量化后折线长度（网格单位）
    dropped: bool
    drop_reason: str | None
    points: list[QuantPoint] = field(default_factory=list)


@dataclass
class TileFragment:
    """一个 source feature 在一个瓦片内的最终可编码片段。"""

    feature_id: int
    tile: tuple[int, int, int]
    geometry: BaseGeometry          # shapely，局部整数坐标
    geom_kind: str                  # "line" | "polygon"
    line_evidence: list[LineEvidence] = field(default_factory=list)
    ring_evidence: list[RingEvidence] = field(default_factory=list)
    dropped_lines: list[LineEvidence] = field(default_factory=list)
    dropped_rings: list[RingEvidence] = field(default_factory=list)
    dropped_fragments: list[dict[str, Any]] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 裁切（具体实现见 clipping.py：面用逐环裁切以保留孔拓扑）
# --------------------------------------------------------------------------- #
def _extract_polygons(geom: BaseGeometry) -> list[Polygon]:
    if geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return list(geom.geoms)
    if isinstance(geom, GeometryCollection):
        out: list[Polygon] = []
        for sub in geom.geoms:
            out.extend(_extract_polygons(sub))
        return out
    # 低维结果（线/点）不进入面通道
    return []


def _extract_lines(geom: BaseGeometry) -> list[LineString]:
    if geom.is_empty:
        return []
    if isinstance(geom, LineString):
        return [geom] if not geom.is_empty else []
    if isinstance(geom, MultiLineString):
        return [g for g in geom.geoms if not g.is_empty]
    if isinstance(geom, GeometryCollection):
        out: list[LineString] = []
        for sub in geom.geoms:
            out.extend(_extract_lines(sub))
        return out
    return []


# --------------------------------------------------------------------------- #
# 量化与环/线工具
# --------------------------------------------------------------------------- #
def _quantize_ring_coords(
    coords: list[tuple[float, float]],
    grid: ZoomGrid,
    tile_x: int,
    tile_y: int,
) -> list[QuantPoint]:
    points: list[QuantPoint] = []
    for x_m, y_m in coords:
        gx = grid.to_global_x(x_m)
        gy = grid.to_global_y(y_m)
        local_x = gx - tile_x * grid.extent
        local_y = gy - tile_y * grid.extent
        points.append(
            QuantPoint(
                x_m=x_m,
                y_m=y_m,
                gx=gx,
                gy=gy,
                local_x=local_x,
                local_y=local_y,
                err_x_m=grid.global_x_to_m(gx) - x_m,
                err_y_m=grid.global_y_to_m(gy) - y_m,
            )
        )
    return points


def _dedupe_int_coords(points: list[QuantPoint], closed: bool) -> list[QuantPoint]:
    """去除连续重复点；闭合环保证首尾再次闭合。"""
    if not points:
        return points
    out = [points[0]]
    for p in points[1:]:
        if (p.local_x, p.local_y) != (out[-1].local_x, out[-1].local_y):
            out.append(p)
    if closed and len(out) >= 2 and (out[0].local_x, out[0].local_y) != (out[-1].local_x, out[-1].local_y):
        out.append(out[0])
    return out


def _signed_area2(points: list[QuantPoint]) -> int:
    """整数坐标环的有向面积的 2 倍（y 向下坐标系）。正值=顺时针。"""
    area2 = 0
    for i in range(len(points) - 1):
        area2 += points[i].local_x * points[i + 1].local_y - points[i + 1].local_x * points[i].local_y
    return area2


def _float_signed_area2_m(coords: list[tuple[float, float]]) -> float:
    """米坐标环的有向面积 2 倍（量化前证据）。"""
    area2 = 0.0
    for i in range(len(coords) - 1):
        area2 += coords[i][0] * coords[i + 1][1] - coords[i + 1][0] * coords[i][1]
    return area2


def _oriented_ring(points: list[QuantPoint], clockwise: bool) -> list[tuple[int, int]]:
    coords = [(p.local_x, p.local_y) for p in points]
    area2 = sum(
        coords[i][0] * coords[i + 1][1] - coords[i + 1][0] * coords[i][1]
        for i in range(len(coords) - 1)
    )
    want_positive = clockwise  # y 向下：顺时针 <=> 有向面积为正
    if (area2 > 0) != want_positive:
        body = coords[:-1][::-1]
        coords = body + [body[0]]
    return coords


def _separate_touching_rings(
    exterior: list[tuple[int, int]], holes: list[list[tuple[int, int]]], buffer_units: int, extent: int
) -> list[list[tuple[int, int]]]:
    """消除量化后孔环与外环在整数网格上的相切，返回修正后的孔环列表。

    裁切框角落落在源孔内时，量化后的孔环会与外环共享 buffer 边界上的整数
    坐标（孔在瓦片边缘“开口”），OGC 简单要素不允许内环与外环相切。处理：
    把孔环中落在 buffer 边界（local 坐标 <0 或 >extent）上的点沿内法线
    平移 1 个整数单位，外环保持不动。所有调整都发生在 buffer 区内，瓦片
    真实坐标空间 [0, extent] 内的覆盖与边界衔接完全不变，MVT 坐标仍合法。
    """
    fixed_holes: list[list[tuple[int, int]]] = []
    lo, hi = -buffer_units, extent + buffer_units
    for hole in holes:
        fixed = []
        for x, y in hole:
            nx, ny = x, y
            if x <= lo:
                nx = lo + 1
            elif x >= hi:
                nx = hi - 1
            if y <= lo:
                ny = lo + 1
            elif y >= hi:
                ny = hi - 1
            fixed.append((nx, ny))
        # 平移可能产生连续重复点，去重并重新闭合
        deduped = [fixed[0]]
        for p in fixed[1:]:
            if p != deduped[-1]:
                deduped.append(p)
        if deduped[0] != deduped[-1]:
            deduped.append(deduped[0])
        fixed_holes.append(deduped if len(deduped) >= 4 else hole)
    return fixed_holes


# --------------------------------------------------------------------------- #
# 线处理
# --------------------------------------------------------------------------- #
def process_line_feature(
    feature_id: int,
    source_geom: LineString,
    grid: ZoomGrid,
    tile: tuple[int, int, int],
    clip_box: tuple[float, float, float, float],
) -> TileFragment:
    z, tx, ty = tile
    clipped = clip_lines(source_geom, clip_box)
    parts = _extract_lines(clipped)
    fragment = TileFragment(feature_id=feature_id, tile=tile, geometry=GeometryCollection(), geom_kind="line")
    kept_lines: list[LineString] = []

    for idx, part in enumerate(parts):
        raw_coords = list(part.coords)
        points = _dedupe_int_coords(_quantize_ring_coords(raw_coords, grid, tx, ty), closed=False)
        unique = {(p.local_x, p.local_y) for p in points}
        length = 0.0
        for a, b in zip(points, points[1:]):
            length += math.hypot(b.local_x - a.local_x, b.local_y - a.local_y)
        ev = LineEvidence(
            part_index=idx,
            vertex_count=len(raw_coords),
            unique_vertices=len(unique),
            length_grid=length,
            dropped=False,
            drop_reason=None,
            points=points,
        )
        if len(unique) < 2:
            ev.dropped = True
            ev.drop_reason = "量化后唯一顶点不足 2 个，线段塌缩为点"
            fragment.dropped_lines.append(ev)
            continue
        fragment.line_evidence.append(ev)
        kept_lines.append(LineString([(p.local_x, p.local_y) for p in points]))

    if kept_lines:
        fragment.geometry = MultiLineString(kept_lines) if len(kept_lines) > 1 else kept_lines[0]
    return fragment


# --------------------------------------------------------------------------- #
# 面处理
# --------------------------------------------------------------------------- #
def process_polygon_feature(
    feature_id: int,
    source_geom: Polygon,
    grid: ZoomGrid,
    tile: tuple[int, int, int],
    clip_box: tuple[float, float, float, float],
    buffer_units: int,
    extent: int,
) -> TileFragment:
    z, tx, ty = tile
    clipped = clip_polygon(source_geom, clip_box)
    parts = _extract_polygons(clipped)
    fragment = TileFragment(feature_id=feature_id, tile=tile, geometry=GeometryCollection(), geom_kind="polygon")
    kept_polygons: list[Polygon] = []

    for idx, part in enumerate(parts):
        exterior_ev, exterior_coords = _process_ring(
            list(part.exterior.coords), "exterior", idx, grid, tx, ty
        )
        if exterior_ev.dropped:
            fragment.dropped_rings.append(exterior_ev)
            fragment.dropped_fragments.append(
                {
                    "part_index": idx,
                    "reason": "外环量化后零面积（面片段塌缩），整个片段丢弃",
                    "area_grid2_after": exterior_ev.area_grid2_after,
                }
            )
            # 该片段的孔不再单独编码，逐一记录为随片段丢弃
            for hole_idx, hole in enumerate(part.interiors):
                ev, _ = _process_ring(list(hole.coords), "hole", idx, grid, tx, ty)
                ev.drop_reason = "所属面片段外环塌缩，随片段整体丢弃"
                ev.dropped = True
                fragment.dropped_rings.append(ev)
            continue
        fragment.ring_evidence.append(exterior_ev)

        oriented_exterior = _oriented_ring(exterior_coords, clockwise=True)
        oriented_holes: list[list[tuple[int, int]]] = []
        for hole in part.interiors:
            hole_ev, hole_points = _process_ring(list(hole.coords), "hole", idx, grid, tx, ty)
            if hole_ev.dropped:
                # 关键：只丢孔并显式记录，外环仍然保留——不静默填实
                fragment.dropped_rings.append(hole_ev)
                continue
            fragment.ring_evidence.append(hole_ev)
            oriented_holes.append(_oriented_ring(hole_points, clockwise=False))

        # 孔环与外环在 buffer 边界相切时，把孔环贴边点内移 1 整数单位
        oriented_holes = _separate_touching_rings(oriented_exterior, oriented_holes, buffer_units, extent)
        polygon = Polygon(oriented_exterior, oriented_holes)
        if not polygon.is_valid:
            repaired = make_valid(polygon)
            repaired_polys = _extract_polygons(repaired)
            if not repaired_polys:
                fragment.dropped_fragments.append(
                    {"part_index": idx, "reason": "量化后面无效且无法修复", "area_grid2_after": exterior_ev.area_grid2_after}
                )
                continue
            polygon = MultiPolygon(repaired_polys) if len(repaired_polys) > 1 else repaired_polys[0]
        kept_polygons.append(polygon) if isinstance(polygon, Polygon) else kept_polygons.extend(polygon.geoms)

    if kept_polygons:
        fragment.geometry = MultiPolygon(kept_polygons) if len(kept_polygons) > 1 else kept_polygons[0]
    return fragment


def _process_ring(
    raw_coords: list[tuple[float, float]],
    role: str,
    part_index: int,
    grid: ZoomGrid,
    tx: int,
    ty: int,
) -> tuple[RingEvidence, list[QuantPoint]]:
    points = _dedupe_int_coords(_quantize_ring_coords(raw_coords, grid, tx, ty), closed=True)
    unique = {(p.local_x, p.local_y) for p in points}
    area2_after = _signed_area2(points)
    ev = RingEvidence(
        role=role,
        part_index=part_index,
        vertex_count=len(raw_coords),
        unique_vertices=len(unique),
        area_grid2_before=_float_signed_area2_m(raw_coords) / (grid.resolution_m ** 2),
        area_grid2_after=area2_after,
        dropped=False,
        drop_reason=None,
        points=points,
    )
    if len(unique) < 3 or area2_after == 0:
        ev.dropped = True
        ev.drop_reason = (
            f"量化后零面积环（唯一顶点 {len(unique)} 个，有向面积2倍={area2_after}）"
        )
    return ev, points


def process_feature(
    feature: dict[str, Any],
    grid: ZoomGrid,
    tile: tuple[int, int, int],
    clip_box: tuple[float, float, float, float],
    buffer_units: int,
    extent: int,
) -> TileFragment:
    fid = feature["id"]
    geom_dict = feature["geometry"]
    if geom_dict["type"] == "LineString":
        return process_line_feature(fid, LineString(geom_dict["coordinates"]), grid, tile, clip_box)
    polygon = Polygon(geom_dict["coordinates"][0], geom_dict["coordinates"][1:])
    return process_polygon_feature(fid, polygon, grid, tile, clip_box, buffer_units, extent)


def enforce_collection_winding(geometry: BaseGeometry) -> BaseGeometry:
    """编码前统一强制 MVT 方向（外环顺时针、孔逆时针，y 向下）。"""
    if isinstance(geometry, (Polygon, MultiPolygon)):
        # shapely orient sign=1 表示外环有向面积为正；y 向下坐标系中正面积即顺时针
        return shapely_orient(geometry, sign=1.0)
    return geometry
