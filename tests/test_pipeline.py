"""回归测试：量化后塌缩的孔环必须显式报告，不能静默填实。

用一个比 z10 网格（约 9.55 m/unit）还小的合成孔（2m×2m），验证：
- 外环正常编码；
- 孔环在量化后零面积，进入 dropped_rings 并带瓦片/原因；
- 不会把孔静默删除后仍宣称完整；
- 全局网格在任意瓦片顺序下产生相同整数（顺序无关性单元测试）。

直接调用管线函数，不写瓦片到磁盘。
"""

from __future__ import annotations

from shapely.geometry import Polygon

from mvt_pipeline.grid import make_grid, tile_bounds_m
from mvt_pipeline.pipeline import process_polygon_feature

EXTENT = 4096
BUFFER = 16


def test_collapsing_hole_is_reported_not_silently_filled() -> None:
    grid = make_grid(10, EXTENT)
    # 外环 200m×200m（可存活），中心孔 2m×2m（远小于一个网格单位，必然塌缩）
    polygon = Polygon(
        [(-100, -100), (100, -100), (100, 100), (-100, 100), (-100, -100)],
        [[(-1, -1), (-1, 1), (1, 1), (1, -1), (-1, -1)]],
    )
    tile = (10, 512, 512)  # 包含原点的瓦片
    clip_box = tile_bounds_m(grid, tile[1], tile[2], BUFFER)
    fragment = process_polygon_feature(99, polygon, grid, tile, clip_box, BUFFER, EXTENT)

    # 外环必须存活
    assert not fragment.geometry.is_empty, "外环不应因小孔塌缩而整体丢弃"
    kept_roles = [e.role for e in fragment.ring_evidence]
    assert kept_roles == ["exterior"], f"存活环应只有外环，得到 {kept_roles}"

    # 孔必须出现在 dropped_rings，且带明确原因
    assert len(fragment.dropped_rings) == 1, "塌缩孔环必须恰好登记 1 条"
    dropped = fragment.dropped_rings[0]
    assert dropped.role == "hole"
    assert dropped.dropped is True
    assert dropped.area_grid2_after == 0
    assert "零面积" in dropped.drop_reason
    assert fragment.tile == tile, "记录必须标明所在瓦片"


def test_global_grid_is_tile_order_independent() -> None:
    grid = make_grid(9, EXTENT)
    # 同一个米坐标点在任意两块瓦片内量化到相同的全局网格整数
    x_m, y_m = 123.456, -789.012
    gx_a = grid.to_global_x(x_m)
    gy_a = grid.to_global_y(y_m)
    for tx in (255, 256):
        for ty in (255, 256):
            lx = grid.to_local_x(x_m, tx)
            ly = grid.to_local_y(y_m, ty)
            back_x, back_y = grid.local_to_global(lx, ly, tx, ty)
            assert (back_x, back_y) == (gx_a, gy_a), "全局网格坐标不应随瓦片变化"


def test_round_half_up_tie_behavior() -> None:
    from mvt_pipeline.grid import round_half_up

    # half-up：半值向正无穷，确定性且与 Python 内置 half-to-even 不同
    assert round_half_up(2.5) == 3
    assert round_half_up(-2.5) == -2
    assert round_half_up(0.5) == 1
    assert round_half_up(-0.5) == 0


if __name__ == "__main__":
    test_collapsing_hole_is_reported_not_silently_filled()
    test_global_grid_is_tile_order_independent()
    test_round_half_up_tie_behavior()
    print("全部回归测试通过")
