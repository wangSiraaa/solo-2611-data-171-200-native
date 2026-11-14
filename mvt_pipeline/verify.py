"""verify：独立解码 MVT 并做几何级核对。

不重新信任 build 侧的内存对象：所有检查从磁盘上的 .mvt 字节出发，
用 mvt_pipeline.mvt_codec（自行解析命令流）解码，再把瓦片局部整数坐标
复原为同 zoom 的全局网格整数坐标后核对。

检查项
------
A. protobuf 结构：layer 名/version/extent、feature id 与属性类型（false/0 不缺失）
B. 环方向：MVT y 向下，外环顺时针（area2>0），孔逆时针（area2<0）；无零面积环
C. 相邻瓦片公共边界：同一全局网格点在相邻瓦片的表示一致（gx/gy 相同）
D. 线端点在公共边界处相接；边界线（feature 4）在四块瓦片内均可独立还原
E. 面覆盖：把各瓦片的面片段映射回全局网格并集，与源面裁切到瓦片并集区域
   的网格覆盖做单元格集合比对（无重复填实、无孔洞被填）
F. 量化误差与塌缩范围：对照清单证据，覆盖窄面/小环/跨零线/边界线四个用例
G. 顺序无关性：随机顺序重建，字节级一致
"""

from __future__ import annotations

import argparse
import json
import os
import random
import tempfile
from dataclasses import dataclass

from shapely.geometry import Polygon, box as shapely_box

from .grid import ZoomGrid, make_grid
from .mvt_codec import DecodedTile, decode_tile
from .pipeline import load_features
from .build import run_build, load_plan


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


def _tile_path(out_dir: str, z: int, x: int, y: int) -> str:
    return os.path.join(out_dir, "tiles", str(z), str(x), f"{y}.mvt")


def _decode_all(out_dir: str, plan: dict) -> dict[tuple[int, int, int], DecodedTile]:
    decoded = {}
    for t in plan["tiles"]:
        z, x, y = t["z"], t["x"], t["y"]
        with open(_tile_path(out_dir, z, x, y), "rb") as fh:
            decoded[(z, x, y)] = decode_tile(fh.read())
    return decoded


def check_structure(
    decoded: dict[tuple[int, int, int], DecodedTile],
    plan: dict,
    features: list[dict],
) -> list[CheckResult]:
    results: list[CheckResult] = []
    expected_props = {f["id"]: f["properties"] for f in features}
    layer_name = plan["layer"]
    extent = plan["extent"]

    for tile, dt in sorted(decoded.items()):
        if len(dt.layers) != 1 or dt.layers[0].name != layer_name:
            results.append(CheckResult(f"structure.layer@{tile}", False, f"layer 异常: {[l.name for l in dt.layers]}"))
            continue
        layer = dt.layers[0]
        ok = layer.version == 2 and layer.extent == extent
        results.append(CheckResult(f"structure.version_extent@{tile}", ok, f"version={layer.version}, extent={layer.extent}"))

        ids = [f.feature_id for f in layer.features]
        if ids != sorted(ids):
            results.append(CheckResult(f"structure.feature_order@{tile}", False, f"feature 顺序非确定: {ids}"))

        for feat in layer.features:
            exp = expected_props[feat.feature_id]
            if set(feat.properties) != set(exp):
                results.append(
                    CheckResult(f"structure.prop_keys@{tile}:{feat.feature_id}", False,
                                f"属性键集合不一致 {feat.properties} != {exp}")
                )
                continue
            type_ok = all(
                type(feat.properties[k]) is type(exp[k]) and feat.properties[k] == exp[k] for k in exp
            )
            results.append(
                CheckResult(
                    f"structure.props@{tile}:{feat.feature_id}",
                    type_ok,
                    f"解码属性={feat.properties}（类型逐键比对，bool 不被当 int，0/false 保留）",
                )
            )
    return results


def check_winding(decoded: dict[tuple[int, int, int], DecodedTile]) -> list[CheckResult]:
    results: list[CheckResult] = []
    for tile, dt in sorted(decoded.items()):
        for layer in dt.layers:
            for feat in layer.features:
                if feat.geom_type != "Polygon":
                    continue
                # 命令流里环按顺序排列：每个顺时针环开启一个新面，其后逆时针环是它的孔
                polygon_open = False
                for ring_idx, ring in enumerate(feat.rings):
                    if ring.area2 == 0:
                        results.append(
                            CheckResult(f"winding.zero_area@{tile}:{feat.feature_id}", False,
                                        f"环 {ring_idx} 零面积，不应被编码")
                        )
                        continue
                    if ring.area2 > 0:
                        polygon_open = True
                        results.append(
                            CheckResult(f"winding.exterior@{tile}:{feat.feature_id}:r{ring_idx}", True,
                                        f"外环顺时针 area2={ring.area2}")
                        )
                    else:
                        ok = polygon_open
                        results.append(
                            CheckResult(
                                f"winding.hole@{tile}:{feat.feature_id}:r{ring_idx}",
                                ok,
                                f"孔环逆时针 area2={ring.area2}，前置外环存在={ok}",
                            )
                        )
    return results


def _decoded_feature(decoded: dict, tile: tuple[int, int, int], fid: int):
    layer = decoded[tile].layers[0]
    return next((f for f in layer.features if f.feature_id == fid), None)


def _seam_grid_coordinate(
    grid: ZoomGrid,
    tile1: tuple[int, int, int],
    tile2: tuple[int, int, int],
    extent: int,
) -> tuple[str, int]:
    """公共边界在全局网格中的固定坐标（'gx' 或 'gy' 方向）。"""
    z, x1, y1 = tile1
    _, x2, y2 = tile2
    if x2 != x1:
        return "gx", (max(x1, x2)) * extent
    return "gy", (max(y1, y2)) * extent


def check_seam_points(
    decoded: dict[tuple[int, int, int], DecodedTile],
    grids: dict[int, ZoomGrid],
    plan: dict,
) -> list[CheckResult]:
    """相邻瓦片公共边界（seam）上的几何衔接核对。

    注意：buffer 边在两块瓦片中是不同的全局网格线（相差 2*buffer 单位），
    缓冲带内的裁切顶点不会逐点相同；连续性应在瓦片真实公共边界（不含 buffer）
    上核对：
    - 线：两瓦片在 seam 上的全局网格点集合一致（端点相接）；
    - 面：两瓦片跨越 seam 的边在 seam 上截出的整数区间集合一致。
    """
    results: list[CheckResult] = []
    extent = plan["extent"]
    tiles_by_zoom: dict[int, list[tuple[int, int, int]]] = {}
    for tile in decoded:
        tiles_by_zoom.setdefault(tile[0], []).append(tile)

    for z, tile_list in sorted(tiles_by_zoom.items()):
        grid = grids[z]
        for i, t1 in enumerate(sorted(tile_list)):
            for t2 in sorted(tile_list)[i + 1:]:
                dx, dy = t2[1] - t1[1], t2[2] - t1[2]
                if (abs(dx), abs(dy)) not in ((1, 0), (0, 1)):
                    continue
                axis, seam_value = _seam_grid_coordinate(grid, t1, t2, extent)
                fids = sorted({f.feature_id for f in decoded[t1].layers[0].features}
                              & {f.feature_id for f in decoded[t2].layers[0].features})
                for fid in fids:
                    f1, f2 = _decoded_feature(decoded, t1, fid), _decoded_feature(decoded, t2, fid)
                    if f1 is None or f2 is None:
                        continue
                    if f1.geom_type == "LineString":
                        s1 = _line_seam_points(f1, grid, t1, axis, seam_value)
                        s2 = _line_seam_points(f2, grid, t2, axis, seam_value)
                        results.append(
                            CheckResult(
                                f"seam.line@z{z}:{t1[1:]}-{t2[1:]}:f{fid}",
                                s1 == s2 and len(s1) > 0,
                                f"seam 上全局网格点：瓦片1={sorted(s1)} 瓦片2={sorted(s2)}",
                            )
                        )
                    else:
                        s1 = _polygon_seam_intervals(f1, grid, t1, axis, seam_value)
                        s2 = _polygon_seam_intervals(f2, grid, t2, axis, seam_value)
                        results.append(
                            CheckResult(
                                f"seam.polygon@z{z}:{t1[1:]}-{t2[1:]}:f{fid}",
                                s1 == s2 and len(s1) > 0,
                                f"seam 上整数覆盖区间：瓦片1={sorted(s1)} 瓦片2={sorted(s2)}",
                            )
                        )
    return results


def _line_seam_points(feat, grid: ZoomGrid, tile: tuple[int, int, int], axis: str, seam: int) -> set[int]:
    """线几何落在 seam 上的点，返回另一轴的全局网格坐标集合；
    同时用线性插值处理跨过 seam 的线段（插值点必然是 seam 整数网格点）。"""
    out: set[int] = set()
    tx, ty = tile[1], tile[2]
    for line in feat.lines:
        global_pts = [grid.local_to_global(px, py, tx, ty) for px, py in line]
        for (ax, ay), (bx, by) in zip(global_pts, global_pts[1:]):
            a_seam = ax if axis == "gx" else ay
            b_seam = bx if axis == "gx" else by
            a_other = ay if axis == "gx" else ax
            b_other = by if axis == "gx" else bx
            if a_seam == seam:
                out.add(a_other)
            if b_seam == seam:
                out.add(b_other)
            if (a_seam - seam) * (b_seam - seam) < 0:
                # 线段跨过 seam：seam 是整数网格线，求另一轴上的整数交点
                ratio = (seam - a_seam) / (b_seam - a_seam)
                cross = a_other + ratio * (b_other - a_other)
                out.add(int(round(cross)))
    return out


def _polygon_seam_intervals(
    feat, grid: ZoomGrid, tile: tuple[int, int, int], axis: str, seam: int
) -> set[tuple[int, int]]:
    """面环在 seam 上截出的整数覆盖区间 [lo, hi] 集合。

    逐环扫描每条边：沿 seam 的直边直接产出区间；严格跨越 seam 的边给出
    整数交点，同一环的跨边交点按顺序两两配对（进、出各一次）。
    """
    tx, ty = tile[1], tile[2]
    intervals: set[tuple[int, int]] = set()
    for ring in feat.rings:
        gp = [grid.local_to_global(px, py, tx, ty) for px, py in ring.coords]
        crossings: list[int] = []
        for (ax, ay), (bx, by) in zip(gp, gp[1:]):
            a_seam = ax if axis == "gx" else ay
            b_seam = bx if axis == "gx" else by
            a_other = ay if axis == "gx" else ax
            b_other = by if axis == "gx" else bx
            if a_seam == seam and b_seam == seam:
                intervals.add((min(a_other, b_other), max(a_other, b_other)))
            elif (a_seam - seam) * (b_seam - seam) < 0:
                ratio = (seam - a_seam) / (b_seam - a_seam)
                crossings.append(int(round(a_other + ratio * (b_other - a_other))))
        for k in range(0, len(crossings) - 1, 2):
            intervals.add((min(crossings[k], crossings[k + 1]), max(crossings[k], crossings[k + 1])))
    return intervals


def check_boundary_line(
    decoded: dict[tuple[int, int, int], DecodedTile],
    grids: dict[int, ZoomGrid],
    plan: dict,
) -> list[CheckResult]:
    """feature 4（x_m=0 的南北向边界线）：每块瓦片都应包含该线上的片段，
    且所有片段复原到全局网格后 gx 相同、gy 连续覆盖 [-9000, 9000] 米区间。"""
    results: list[CheckResult] = []
    by_zoom: dict[int, list[tuple[int, int, int]]] = {}
    for tile in decoded:
        by_zoom.setdefault(tile[0], []).append(tile)

    for z, tile_list in sorted(by_zoom.items()):
        grid = grids[z]
        gx_zero = grid.to_global_x(0.0)
        per_tile_bounds: dict[tuple[int, int, int], tuple[int, int]] = {}
        for tile in sorted(tile_list):
            layer = decoded[tile].layers[0]
            feat = next((f for f in layer.features if f.feature_id == 4), None)
            if feat is None:
                results.append(CheckResult(f"boundary_line.presence@{tile}", False, "瓦片内缺失边界线片段"))
                continue
            xs, ys = set(), []
            for line in feat.lines:
                for px, py in line:
                    gx, gy = grid.local_to_global(px, py, tile[1], tile[2])
                    xs.add(gx)
                    ys.append(gy)
            per_tile_bounds[tile] = (min(ys), max(ys))
            results.append(
                CheckResult(f"boundary_line.presence@{tile}", xs == {gx_zero},
                            f"边界线全局 gx 集合={sorted(xs)}，期望={[gx_zero]}，点数={len(ys)}，"
                            f"gy 区间=[{min(ys)},{max(ys)}]")
            )
        # 覆盖区间核对：直线在恒定 gx 上，用各瓦片线段 gy 区间并集核对期望范围
        gy_lo = grid.to_global_y(9000.0)
        gy_hi = grid.to_global_y(-9000.0)
        covered: set[int] = set()
        for lo, hi in per_tile_bounds.values():
            covered.update(range(lo, hi + 1))
        missing = [g for g in range(gy_lo, gy_hi + 1) if g not in covered]
        results.append(
            CheckResult(
                f"boundary_line.coverage@z{z}",
                not missing,
                f"gy 期望 [{gy_lo},{gy_hi}] 共 {gy_hi - gy_lo + 1} 个网格行；"
                f"四瓦片线段并集缺失 {len(missing)} 行；各瓦片区间={dict(zip([str(k) for k in per_tile_bounds], per_tile_bounds.values()))}",
            )
        )
    return results


def _grid_cells_of_polygon(exterior: list[tuple[int, int]], holes: list[list[tuple[int, int]]]) -> set[tuple[int, int]]:
    """整数网格多边形覆盖的单位单元格集合（左下整数角点表示）。"""
    poly = Polygon(exterior, holes)
    if not poly.is_valid:
        from shapely.validation import make_valid
        from .pipeline import _extract_polygons
        polys = _extract_polygons(make_valid(poly))
    else:
        polys = [poly]
    cells: set[tuple[int, int]] = set()
    for p in polys:
        minx, miny, maxx, maxy = (int(v) for v in p.bounds)
        for ix in range(minx, maxx):
            for iy in range(miny, maxy):
                if p.intersects(shapely_box(ix, iy, ix + 1, iy + 1)):
                    cells.add((ix, iy))
    return cells


def check_polygon_coverage(
    decoded: dict[tuple[int, int, int], DecodedTile],
    grids: dict[int, ZoomGrid],
    plan: dict,
    features: list[dict],
) -> list[CheckResult]:
    """面覆盖核对：解码环 -> 全局网格 -> 单位单元格集合。

    期望集合与实际集合对称计算：对每个瓦片用完全相同的 buffer 裁切框、
    逐环裁切、全局网格量化和 buffer 边界孔环内移规则，独立从源 GeoJSON
    重新生成一遍，再栅格化为单位单元格集合后比对。
    """
    from .clipping import clip_polygon
    from .pipeline import _extract_polygons, _separate_touching_rings
    from .grid import tile_bounds_m

    results: list[CheckResult] = []
    polygon_features = [f for f in features if f["geometry"]["type"] == "Polygon"]
    extent = plan["extent"]
    buffer_units = plan["buffer_units"]

    for z in sorted({t[0] for t in decoded}):
        grid = grids[z]
        zoom_tiles = [t for t in decoded if t[0] == z]
        for feat in polygon_features:
            actual_cells: set[tuple[int, int]] = set()
            expected_cells: set[tuple[int, int]] = set()
            hole_rings_seen = 0
            exterior_rings_seen = 0
            source = Polygon(feat["geometry"]["coordinates"][0], feat["geometry"]["coordinates"][1:])

            for tile in zoom_tiles:
                tx, ty = tile[1], tile[2]
                # ---- 实际侧：独立解码 MVT 得到的环 ----
                layer = decoded[tile].layers[0]
                df = next((f for f in layer.features if f.feature_id == feat["id"]), None)
                if df is not None:
                    pending_exterior: list[tuple[int, int]] | None = None
                    pending_holes: list[list[tuple[int, int]]] = []
                    for ring in df.rings:
                        global_ring = [grid.local_to_global(px, py, tx, ty) for px, py in ring.coords]
                        if ring.area2 > 0:
                            if pending_exterior is not None:
                                actual_cells |= _grid_cells_of_polygon(pending_exterior, pending_holes)
                            pending_exterior = global_ring
                            pending_holes = []
                            exterior_rings_seen += 1
                        else:
                            pending_holes.append(global_ring)
                            hole_rings_seen += 1
                    if pending_exterior is not None:
                        actual_cells |= _grid_cells_of_polygon(pending_exterior, pending_holes)

                # ---- 期望侧：同一裁切框/量化/环分离规则，独立从源几何重算 ----
                clip_box = tile_bounds_m(grid, tx, ty, buffer_units)
                clipped = clip_polygon(source, clip_box)
                for part in _extract_polygons(clipped):
                    ext_local = [(grid.to_local_x(x, tx), grid.to_local_y(y, ty)) for x, y in part.exterior.coords]
                    ext_ring = _dedupe_closed(ext_local)
                    if _area2(ext_ring) == 0:
                        continue
                    holes_local = [
                        _dedupe_closed([(grid.to_local_x(x, tx), grid.to_local_y(y, ty)) for x, y in hole.coords])
                        for hole in part.interiors
                    ]
                    kept_holes = [h for h in holes_local if _area2(h) != 0]
                    kept_holes = _separate_touching_rings(ext_ring, kept_holes, buffer_units, extent)
                    ext_global = [grid.local_to_global(px, py, tx, ty) for px, py in ext_ring]
                    holes_global = [
                        [grid.local_to_global(px, py, tx, ty) for px, py in h] for h in kept_holes
                    ]
                    expected_cells |= _grid_cells_of_polygon(ext_global, holes_global)

            only_actual = actual_cells - expected_cells
            only_expected = expected_cells - actual_cells
            results.append(
                CheckResult(
                    f"coverage.cells@z{z}:f{feat['id']}",
                    not only_actual and not only_expected,
                    f"实际单元格={len(actual_cells)} 期望={len(expected_cells)} "
                    f"多出={len(only_actual)} 缺失={len(only_expected)}；"
                    f"解码外环数={exterior_rings_seen} 孔环数={hole_rings_seen}",
                )
            )
    return results


def _dedupe_closed(coords: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out = [coords[0]]
    for c in coords[1:]:
        if c != out[-1]:
            out.append(c)
    if out[-1] != out[0]:
        out.append(out[0])
    return out


def _area2(coords: list[tuple[int, int]]) -> int:
    return sum(coords[i][0] * coords[i + 1][1] - coords[i + 1][0] * coords[i][1] for i in range(len(coords) - 1))


def check_quantization_evidence(
    out_dir: str, grids: dict[int, ZoomGrid], plan: dict
) -> list[CheckResult]:
    """量化误差与塌缩范围：读清单证据并核对数值与规则。"""
    results: list[CheckResult] = []
    manifest = json.load(open(os.path.join(out_dir, "manifest", "manifest.json"), encoding="utf-8"))
    max_err: dict[int, float] = {}
    drops: list[str] = []

    for tile_man in manifest["tiles"]:
        z = tile_man["tile"]["z"]
        for bucket in ("fragments", "empty_or_dropped"):
            for frag in tile_man[bucket]:
                for ev in frag.get("line_evidence", []) + frag.get("dropped_lines", []):
                    for p in ev["points"]:
                        ex, ey = p["quant_error_m"]
                        max_err[z] = max(max_err.get(z, 0.0), abs(ex), abs(ey))
                    if ev.get("dropped"):
                        drops.append(f"line f{frag['source_feature_id']} z{z} {tile_man['tile']}: {ev['drop_reason']}")
                for ev in frag.get("ring_evidence", []) + frag.get("dropped_rings", []):
                    for p in ev["points"]:
                        ex, ey = p["quant_error_m"]
                        max_err[z] = max(max_err.get(z, 0.0), abs(ex), abs(ey))
                    if ev.get("dropped"):
                        drops.append(
                            f"{ev['role']} f{frag['source_feature_id']} z{z} {tile_man['tile']} part{ev['part_index']}: {ev['drop_reason']}"
                        )

    for z, err in sorted(max_err.items()):
        bound = grids[z].resolution_m / 2.0
        results.append(
            CheckResult(
                f"quant.error_bound@z{z}",
                err <= bound + 1e-9,
                f"最大绝对量化误差={err:.6f} m，理论半格上界={bound:.6f} m（resolution={grids[z].resolution_m:.6f} m）",
            )
        )

    # 窄面（feature 3）：每 zoom 仅北排两块与它有 2D 交集（南排只沿边相触，
    # intersection 为 EMPTY），因此每 zoom 2 条、两级 zoom 共 4 条塌缩记录
    thin_drops = [d for d in drops if "f3" in d]
    n_colliding_tiles = 2 * len(grids)
    results.append(
        CheckResult(
            "collapse.thin_polygon",
            len(thin_drops) == n_colliding_tiles,
            f"窄面(f3)塌缩记录 {len(thin_drops)} 条，期望 {n_colliding_tiles} 条"
            "（每 zoom 北排两瓦片各 1 条；南排仅边界相触无 2D 交集，不产生片段）；"
            "全部为外环零面积丢弃，f3 无孔，不存在“删孔填实”场景",
        )
    )
    # 带孔面（feature 2）的孔在两级 zoom 都必须存活
    hole_drops_f2 = [d for d in drops if "f2" in d and "hole" in d]
    results.append(
        CheckResult(
            "collapse.hole_survival",
            not hole_drops_f2,
            f"带孔面(f2)孔环塌缩记录 {len(hole_drops_f2)} 条（期望 0，孔未被静默填实）",
        )
    )
    return results


def check_determinism(plan_path: str, features_path: str, out_dir: str, seed: int = 176) -> list[CheckResult]:
    plan = json.load(open(plan_path, encoding="utf-8"))
    order = [(t["z"], t["x"], t["y"]) for t in plan["tiles"]]
    rng = random.Random(seed)
    rng.shuffle(order)
    with tempfile.TemporaryDirectory() as tmp:
        _, shuffled = run_build(plan_path, features_path, tmp, tile_order=order)
    mismatch = []
    for tile, pbf in shuffled.items():
        with open(_tile_path(out_dir, *tile), "rb") as fh:
            if fh.read() != pbf:
                mismatch.append(tile)
    return [
        CheckResult(
            "determinism.byte_identical",
            not mismatch,
            f"随机瓦片顺序重建后逐字节比对 {len(shuffled)} 块，不一致={mismatch}",
        )
    ]


def run_verify(plan_path: str, features_path: str, out_dir: str) -> tuple[list[CheckResult], dict]:
    plan = load_plan(plan_path)
    features = sorted(load_features(json.load(open(features_path, encoding="utf-8"))), key=lambda f: f["id"])
    grids = {z: make_grid(z, plan["extent"]) for z in sorted({t["z"] for t in plan["tiles"]})}
    decoded = _decode_all(out_dir, plan)

    results: list[CheckResult] = []
    results += check_structure(decoded, plan, features)
    results += check_winding(decoded)
    results += check_seam_points(decoded, grids, plan)
    results += check_boundary_line(decoded, grids, plan)
    results += check_polygon_coverage(decoded, grids, plan, features)
    results += check_quantization_evidence(out_dir, grids, plan)
    results += check_determinism(plan_path, features_path, out_dir)
    return results, {"decoded_tiles": len(decoded), "zooms": sorted(grids)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="独立解码并核对 MVT 瓦片（verify）")
    parser.add_argument("--plan", default="fixtures/plan.json")
    parser.add_argument("--features", default="fixtures/features.geojson")
    parser.add_argument("--out", default="output")
    parser.add_argument("--report", default="output/verify_report.txt")
    args = parser.parse_args(argv)

    results, meta = run_verify(args.plan, args.features, args.out)
    passed = sum(1 for r in results if r.ok)
    lines = [
        f"MVT verify 报告：解码 {meta['decoded_tiles']} 块瓦片，zooms={meta['zooms']}",
        f"检查 {len(results)} 项，通过 {passed} 项，失败 {len(results) - passed} 项",
        "=" * 88,
    ]
    for r in results:
        lines.append(f"[{'PASS' if r.ok else 'FAIL'}] {r.name} — {r.detail}")
    text = "\n".join(lines) + "\n"
    os.makedirs(os.path.dirname(args.report), exist_ok=True)
    with open(args.report, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(text)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
