"""矩形裁切（源 CRS 米坐标内进行，量化之前）。

不直接使用 shapely 的 polygon-polygon intersection：GEOS 3.14 在“裁切框
角落落入面的孔洞内部”这一输入上会静默丢失孔环（已用最小复现确认，
clip_by_rect 同样受影响）。这里改用 Sutherland–Hodgman 逐环裁切：

- 外环与每个孔环分别对矩形四条边做半平面裁切；
- 交点按边吸附到精确的裁切边界值（容差 1e-7 米），消除 1e-13 级浮点
  噪声导致的环自交；
- 当裁切框角落落在源面孔内时，裁切后的孔环会与外环在裁切边上相切，
  OGC 简单要素要求内环严格位于外环内部，因此把贴边孔环节点外扩 1 个
  EPSILON、贴边外环节点外扩 2 个 EPSILON（量化后不可见）；
- 再用裁切后的环重建 Polygon / MultiPolygon，孔结构显式保留；
- 完全落在裁切框外的环返回空；完全在内的环原样返回。

线要素仍可直接用 shapely intersection（线-面求交不涉及孔拓扑）。
"""

from __future__ import annotations

from shapely.geometry import (
    GeometryCollection,
    MultiPolygon,
    Polygon,
)
from shapely.geometry.base import BaseGeometry
from shapely.geometry import box as shapely_box
from shapely.validation import make_valid

#: 边界吸附容差（米）。裁切交点理论上精确位于 minx/maxx/miny/maxy 上，
#: 1e-7 m 远小于最小网格分辨率（z10 约 9.55 m），不影响任何量化结果。
SNAP_TOLERANCE_M = 1e-7

#: 孔/外环在裁切边界上相切时的外扩步长（米）。该情形只发生在裁切框角落
#: 落入源面孔内部时：裁切后孔环与外环共享边界点（孔在瓦片边缘“开口”），
#: OGC 简单要素要求内环严格位于外环内部，相切多边形会被 GEOS 判为自交且
#: 修复时吞掉孔。处理方式：贴边孔环节点外扩 1 个 EPSILON，贴边外环节点
#: 外扩 2 个 EPSILON，使孔环严格包含于外环、两者都伸出原裁切框少许。
#: 2*EPSILON=2e-6 m 远小于任何 zoom 的网格分辨率（z10≈9.55 m），量化到
#: 整数网格后完全不可见，不改变任何 MVT 坐标（build 报告中给出数值证明）。
HOLE_BOUNDARY_EPSILON_M = 1e-6

# 边编号：0=左(minx) 1=右(maxx) 2=下(miny) 3=上(maxy)
_LEFT, _RIGHT, _BOTTOM, _TOP = 0, 1, 2, 3


def _inside(point: tuple[float, float], edge: int, bounds: tuple[float, float, float, float]) -> bool:
    minx, miny, maxx, maxy = bounds
    x, y = point
    if edge == _LEFT:
        return x >= minx - SNAP_TOLERANCE_M
    if edge == _RIGHT:
        return x <= maxx + SNAP_TOLERANCE_M
    if edge == _BOTTOM:
        return y >= miny - SNAP_TOLERANCE_M
    return y <= maxy + SNAP_TOLERANCE_M


def _intersection(
    a: tuple[float, float], b: tuple[float, float], edge: int, bounds: tuple[float, float, float, float]
) -> tuple[float, float]:
    minx, miny, maxx, maxy = bounds
    ax, ay = a
    bx, by = b
    if edge in (_LEFT, _RIGHT):
        boundary = minx if edge == _LEFT else maxx
        t = (boundary - ax) / (bx - ax) if bx != ax else 0.0
        return boundary, ay + t * (by - ay)
    boundary = miny if edge == _BOTTOM else maxy
    t = (boundary - ay) / (by - ay) if by != ay else 0.0
    return ax + t * (bx - ax), boundary


def _snap_to_bounds(point: tuple[float, float], bounds: tuple[float, float, float, float]) -> tuple[float, float]:
    minx, miny, maxx, maxy = bounds
    x, y = point
    if abs(x - minx) <= SNAP_TOLERANCE_M:
        x = minx
    elif abs(x - maxx) <= SNAP_TOLERANCE_M:
        x = maxx
    if abs(y - miny) <= SNAP_TOLERANCE_M:
        y = miny
    elif abs(y - maxy) <= SNAP_TOLERANCE_M:
        y = maxy
    return x, y


def clip_ring(
    ring_coords: list[tuple[float, float]], bounds: tuple[float, float, float, float]
) -> list[tuple[float, float]]:
    """Sutherland–Hodgman 裁切单个闭合环，返回闭合坐标列表；完全在外则返回 []。"""
    if not ring_coords:
        return []
    output = list(ring_coords[:-1])  # 去掉重复的闭合点
    for edge in range(4):
        if not output:
            return []
        inputs = output
        output = []
        start = inputs[-1]
        for end in inputs:
            end_in = _inside(end, edge, bounds)
            start_in = _inside(start, edge, bounds)
            if end_in:
                if not start_in:
                    output.append(_snap_to_bounds(_intersection(start, end, edge, bounds), bounds))
                output.append(end)
            elif start_in:
                output.append(_snap_to_bounds(_intersection(start, end, edge, bounds), bounds))
            start = end
    if not output:
        return []
    # 所有点统一吸附到裁切边界：消除不同环在同一条裁切边上 1e-13 级的浮点
    # 表示差异，避免“孔环角点落在外环边外侧”这类伪自交
    output = [_snap_to_bounds(p, bounds) for p in output]
    # 去除连续重复点（吸附后可能产生），再闭合
    deduped = [output[0]]
    for p in output[1:]:
        if p != deduped[-1]:
            deduped.append(p)
    if len(deduped) >= 3 and deduped[0] != deduped[-1]:
        deduped.append(deduped[0])
    return deduped if len(deduped) >= 4 else []


def _ring_touches_bounds(
    ring_coords: list[tuple[float, float]], bounds: tuple[float, float, float, float]
) -> bool:
    minx, miny, maxx, maxy = bounds
    return any(
        abs(x - minx) <= SNAP_TOLERANCE_M
        or abs(x - maxx) <= SNAP_TOLERANCE_M
        or abs(y - miny) <= SNAP_TOLERANCE_M
        or abs(y - maxy) <= SNAP_TOLERANCE_M
        for x, y in ring_coords
    )


def _shift_off_boundary(
    ring_coords: list[tuple[float, float]], bounds: tuple[float, float, float, float], distance: float
) -> list[tuple[float, float]]:
    """把落在裁切边界上的节点沿外法线方向平移 distance（米）。"""
    minx, miny, maxx, maxy = bounds
    shifted: list[tuple[float, float]] = []
    for x, y in ring_coords:
        nx, ny = x, y
        if abs(x - minx) <= SNAP_TOLERANCE_M:
            nx = minx - distance
        elif abs(x - maxx) <= SNAP_TOLERANCE_M:
            nx = maxx + distance
        if abs(y - miny) <= SNAP_TOLERANCE_M:
            ny = miny - distance
        elif abs(y - maxy) <= SNAP_TOLERANCE_M:
            ny = maxy + distance
        shifted.append((nx, ny))
    return shifted


def _only_polygons(geom: BaseGeometry) -> list[Polygon]:
    if geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return list(geom.geoms)
    if isinstance(geom, GeometryCollection):
        out: list[Polygon] = []
        for sub in geom.geoms:
            out.extend(_only_polygons(sub))
        return out
    return []


def clip_polygon(polygon: Polygon, bounds: tuple[float, float, float, float]) -> BaseGeometry:
    """带孔面的矩形裁切，保留孔拓扑。"""
    clipped_exterior = clip_ring(list(polygon.exterior.coords), bounds)
    if not clipped_exterior:
        return Polygon()  # 空面
    eps = HOLE_BOUNDARY_EPSILON_M
    clipped_holes: list[list[tuple[float, float]]] = []
    hole_touches_boundary = False
    for hole in polygon.interiors:
        clipped_hole = clip_ring(list(hole.coords), bounds)
        if not clipped_hole:
            continue
        if _ring_touches_bounds(clipped_hole, bounds):
            hole_touches_boundary = True
            clipped_hole = _shift_off_boundary(clipped_hole, bounds, eps)
        clipped_holes.append(clipped_hole)
    if hole_touches_boundary:
        # 外环外扩 2eps，保证外扩后的孔环仍严格位于外环内部
        clipped_exterior = _shift_off_boundary(clipped_exterior, bounds, 2 * eps)
    result = Polygon(clipped_exterior, clipped_holes)
    if not result.is_valid:
        repaired = make_valid(result)
        polys = _only_polygons(repaired)
        if not polys:
            return Polygon()
        result = MultiPolygon(polys) if len(polys) > 1 else polys[0]
    return result


def clip_lines(geom: BaseGeometry, bounds: tuple[float, float, float, float]) -> BaseGeometry:
    """线要素裁切：线-矩形求交，不涉及孔拓扑，直接使用 shapely。"""
    return geom.intersection(shapely_box(*bounds))
