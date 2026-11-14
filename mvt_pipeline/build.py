"""build：离线 MVT 生产入口。

输出（默认 output/）
--------------------
tiles/z/x/y.mvt         真实 protobuf MVT 瓦片
manifest/tile_z_x_y.json 每瓦片清单（来源 feature、片段、变换、证据）
manifest/manifest.json   全量清单
report/geometry_report.md 人类可读几何报告（量化误差、塌缩、孔洞处置）
report/geometry_report.json 机器可读报告
"""

from __future__ import annotations

import argparse
import json
import os
import random
from dataclasses import asdict
from typing import Any

import mapbox_vector_tile

from .grid import ZoomGrid, make_grid, tile_bounds_m
from .pipeline import (
    TileFragment,
    enforce_collection_winding,
    load_features,
    process_feature,
)


def load_plan(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        plan = json.load(fh)
    required = ("source_crs", "world_half_width_m", "extent", "buffer_units", "layer", "tiles")
    for key in required:
        if key not in plan:
            raise ValueError(f"plan.json 缺少字段 {key!r}")
    if plan["source_crs"] != "EPSG:3857":
        raise ValueError("本管线仅支持 EPSG:3857 米坐标输入")
    return plan


def _point_to_dict(p) -> dict[str, Any]:
    return {
        "x_m": p.x_m,
        "y_m": p.y_m,
        "global_grid": [p.gx, p.gy],
        "tile_local": [p.local_x, p.local_y],
        "quant_error_m": [round(p.err_x_m, 6), round(p.err_y_m, 6)],
    }


def fragment_to_manifest(frag: TileFragment) -> dict[str, Any]:
    return {
        "source_feature_id": frag.feature_id,
        "tile": {"z": frag.tile[0], "x": frag.tile[1], "y": frag.tile[2]},
        "encoded_geometry_type": frag.geometry.geom_type if not frag.geometry.is_empty else None,
        "line_evidence": [asdict(ev) | {"points": [_point_to_dict(p) for p in ev.points]} for ev in frag.line_evidence],
        "ring_evidence": [asdict(ev) | {"points": [_point_to_dict(p) for p in ev.points]} for ev in frag.ring_evidence],
        "dropped_lines": [asdict(ev) | {"points": [_point_to_dict(p) for p in ev.points]} for ev in frag.dropped_lines],
        "dropped_rings": [asdict(ev) | {"points": [_point_to_dict(p) for p in ev.points]} for ev in frag.dropped_rings],
        "dropped_fragments": frag.dropped_fragments,
    }


def _grid_to_dict(grid: ZoomGrid) -> dict[str, Any]:
    return {
        "z": grid.z,
        "extent": grid.extent,
        "matrix_size": grid.matrix_size,
        "tile_span_m": grid.tile_span_m,
        "resolution_m": grid.resolution_m,
        "origin_global_grid": [grid.origin_gx, grid.origin_gy],
        "rounding_rule": grid.rounding_rule,
        "global_grid_formula": {
            "gx": "floor(((x_m + W) / resolution_m) + 0.5)",
            "gy": "floor(((W - y_m) / resolution_m) + 0.5)",
            "local": "local_x = gx - tile_x * extent; local_y = gy - tile_y * extent",
        },
    }


def build_tile(
    tile: tuple[int, int, int],
    features: list[dict[str, Any]],
    grid: ZoomGrid,
    extent: int,
    buffer_units: int,
    layer_name: str,
) -> tuple[bytes, list[TileFragment], dict[str, Any]]:
    z, tx, ty = tile
    clip_box = tile_bounds_m(grid, tx, ty, buffer_units)
    fragments: list[TileFragment] = []
    encode_features: list[dict[str, Any]] = []

    for feature in features:  # features 已按 id 排序，保证瓦片内编码顺序确定
        frag = process_feature(feature, grid, tile, clip_box, buffer_units, extent)
        if frag.geometry.is_empty:
            fragments.append(frag)
            continue
        geometry = enforce_collection_winding(frag.geometry)
        fragments.append(frag)
        encode_features.append(
            {
                "id": feature["id"],
                "properties": feature["properties"],
                "geometry": geometry,
            }
        )

    pbf = mapbox_vector_tile.encode(
        [{"name": layer_name, "features": encode_features}],
        default_options={
            "extents": extent,
            "y_coord_down": True,
            "quantize_bounds": None,
            "check_winding_order": False,
            "on_invalid_geometry": None,
        },
    )

    transform = {
        "tile": {"z": z, "x": tx, "y": ty},
        "scheme": "XYZ, y increases southward",
        "clip_box_m": {"minx": clip_box[0], "miny": clip_box[1], "maxx": clip_box[2], "maxy": clip_box[3]},
        "buffer_units_tile_space": buffer_units,
        "buffer_m": buffer_units * grid.resolution_m,
        "local_coordinate_range": [-buffer_units, extent + buffer_units],
        "note": "buffer 为整数瓦片单位，经 resolution_m 换算成米仅用于裁切框；量化一律走全局网格",
    }
    return pbf, fragments, transform


def run_build(
    plan_path: str,
    geojson_path: str,
    out_dir: str,
    tile_order: list[tuple[int, int, int]] | None = None,
) -> dict[str, Any]:
    plan = load_plan(plan_path)
    features = sorted(load_features(json.load(open(geojson_path, encoding="utf-8"))), key=lambda f: f["id"])
    extent = int(plan["extent"])
    buffer_units = int(plan["buffer_units"])
    layer_name = plan["layer"]

    zooms = sorted({t["z"] for t in plan["tiles"]})
    grids = {z: make_grid(z, extent) for z in zooms}
    tiles = tile_order or [(t["z"], t["x"], t["y"]) for t in plan["tiles"]]

    tiles_dir = os.path.join(out_dir, "tiles")
    manifest_dir = os.path.join(out_dir, "manifest")
    os.makedirs(manifest_dir, exist_ok=True)

    full_manifest: dict[str, Any] = {
        "layer": layer_name,
        "source_crs": plan["source_crs"],
        "world_half_width_m": plan["world_half_width_m"],
        "extent": extent,
        "buffer_units": buffer_units,
        "grids": {str(z): _grid_to_dict(grids[z]) for z in zooms},
        "source_features": [
            {"id": f["id"], "geometry_type": f["geometry"]["type"], "properties": f["properties"]} for f in features
        ],
        "tiles": [],
    }

    payloads: dict[tuple[int, int, int], bytes] = {}
    for z, tx, ty in tiles:
        tile = (z, tx, ty)
        pbf, fragments, transform = build_tile(tile, features, grids[z], extent, buffer_units, layer_name)
        payloads[tile] = pbf
        tile_dir = os.path.join(tiles_dir, str(z), str(tx))
        os.makedirs(tile_dir, exist_ok=True)
        with open(os.path.join(tile_dir, f"{ty}.mvt"), "wb") as fh:
            fh.write(pbf)

        tile_manifest = {
            "tile": {"z": z, "x": tx, "y": ty},
            "transform": transform,
            "grid": _grid_to_dict(grids[z]),
            "fragments": [fragment_to_manifest(f) for f in fragments if not f.geometry.is_empty],
            "empty_or_dropped": [fragment_to_manifest(f) for f in fragments if f.geometry.is_empty],
            "provenance_note": "同一 source feature 可在相邻瓦片 buffer 中重复出现，这些是同一来源的裁切片段，不是新地物",
        }
        with open(os.path.join(manifest_dir, f"tile_{z}_{tx}_{ty}.json"), "w", encoding="utf-8") as fh:
            json.dump(tile_manifest, fh, ensure_ascii=False, indent=2)
        full_manifest["tiles"].append(tile_manifest)

    with open(os.path.join(manifest_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(full_manifest, fh, ensure_ascii=False, indent=2)

    from .report import build_reports

    build_reports(full_manifest, out_dir, grids)
    return full_manifest, payloads


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="离线 MVT 瓦片生产（build）")
    parser.add_argument("--plan", default="fixtures/plan.json")
    parser.add_argument("--features", default="fixtures/features.geojson")
    parser.add_argument("--out", default="output")
    parser.add_argument(
        "--determinism-seed",
        type=int,
        default=176,
        help="若 >0，额外以随机瓦片/地物顺序在临时目录重建，逐字节比对（顺序无关性自检）",
    )
    args = parser.parse_args(argv)

    manifest, payloads = run_build(args.plan, args.features, args.out)

    if args.determinism_seed > 0:
        import tempfile

        plan = json.load(open(args.plan, encoding="utf-8"))
        order = [(t["z"], t["x"], t["y"]) for t in plan["tiles"]]
        rng = random.Random(args.determinism_seed)
        rng.shuffle(order)
        with tempfile.TemporaryDirectory() as tmp:
            _, shuffled_payloads = run_build(args.plan, args.features, tmp, tile_order=order)
        mismatch = [t for t in payloads if payloads[t] != shuffled_payloads[t]]
        if mismatch:
            raise AssertionError(f"瓦片输出依赖生成顺序，不一致瓦片: {mismatch}")
        print(f"[determinism] 随机顺序重建 {len(payloads)} 块瓦片，字节级完全一致")

    n_tiles = len(manifest["tiles"])
    print(f"[build] 完成：{n_tiles} 块瓦片 -> {args.out}/tiles，清单 -> {args.out}/manifest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
