"""交叉验证：独立解码器(mvt_codec) 与 mapbox_vector_tile.decode 结果一致性。

两个解码器实现完全独立：本项目的 mvt_codec 自行解析命令流，库解码器是
第三方代码。对每块瓦片比对 feature id、属性、几何顶点（库默认 y 向上，
本项目 y 向下，比较前做 extent-y 翻转）。
"""

from __future__ import annotations

import glob
import os

import mapbox_vector_tile

from mvt_pipeline.mvt_codec import decode_tile


def main() -> int:
    tile_paths = sorted(glob.glob("output/tiles/*/*/*.mvt"))
    failures = 0
    for path in tile_paths:
        pbf = open(path, "rb").read()
        mine = decode_tile(pbf)
        # 库解码器：geojson 模式，y_coord_down=False（默认），输出 y 向上
        lib = mapbox_vector_tile.decode(pbf)
        layer_name = mine.layers[0].name
        lib_features = {f["id"]: f for f in lib[layer_name]["features"]}
        my_features = {f.feature_id: f for f in mine.layers[0].features}
        assert set(lib_features) == set(my_features), f"{path} feature id 集合不一致"
        extent = mine.layers[0].extent
        for fid, mf in my_features.items():
            lf = lib_features[fid]
            assert mf.properties == lf["properties"], f"{path} f{fid} 属性不一致"
            if mf.geom_type == "LineString":
                my_pts = sorted({p for line in mf.lines for p in line})
                lib_pts = sorted(
                    {
                        (x, extent - y)
                        for line in _lib_line_parts(lf["geometry"])
                        for x, y in line
                    }
                )
                if my_pts != lib_pts:
                    failures += 1
                    print(f"FAIL {path} f{fid} 线顶点不一致")
            else:
                my_rings = sorted(tuple(r.coords) for r in mf.rings)
                geom = lf["geometry"]
                polygons = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
                lib_rings = sorted(
                    tuple((x, extent - y) for x, y in ring)
                    for polygon in polygons
                    for ring in polygon
                )
                if my_rings != lib_rings:
                    failures += 1
                    print(f"FAIL {path} f{fid} 面环不一致")
    total = len(tile_paths)
    print(f"交叉验证：{total} 块瓦片，独立解码器与库解码器几何/属性全部一致" if failures == 0
          else f"交叉验证失败 {failures} 项")
    return 1 if failures else 0


def _lib_line_parts(geometry: dict):
    if geometry["type"] == "LineString":
        return [geometry["coordinates"]]
    return geometry["coordinates"]


if __name__ == "__main__":
    raise SystemExit(main())
