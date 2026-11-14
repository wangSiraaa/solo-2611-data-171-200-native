"""每 zoom 一套全局整数网格。

核心不变量
----------
对 zoom z 定义一张覆盖整个 Web Mercator 世界（米）的整数网格：

    n      = 2 ** z                      （该 zoom 的瓦片行列数）
    span_m = 2 * W / n                   （一块瓦片的地面跨度，米）
    res_m  = span_m / extent             （一个网格单位，米）
    gx(m)  = round_half_up((m - (-W)) / res_m)
    gy(m)  = round_half_up((m - (+W)) / res_m)      # y 轴向南，米 y 向下为负

其中 W = 20037508.342789244（世界半宽），extent=4096。
瓦片 (x,y) 的局部瓦片坐标与全局网格坐标的关系只是平移：

    local_x = gx - x * extent
    local_y = gy - y * extent

因此同一个世界点在任何瓦片内得到的 gx/gy 完全相同，恰落在瓦片边界上的点
（米坐标恰为 span_m 的整数倍）在相邻瓦片内的局部坐标是 -0/extent 或
extent/0 这类等价表达，量化值不会因处理瓦片不同而不一致。

舍入规则（每 zoom 唯一、确定性）
--------------------------------
half-up，即“半值向正无穷取整”： round_half_up(v) = floor(v + 0.5)。
对负数同样适用（如 -1.5 -> -1）。所有网格坐标为非负整数，选择 half-up
而不是 Python 银行家舍入（round 的 half-to-even），是为了得到确定且
可解释的 tie 行为，并在报告中披露。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

#: Web Mercator 世界半宽（米），来自 plan.json
WORLD_HALF_WIDTH_M = 20037508.342789244
WORLD_WIDTH_M = 2.0 * WORLD_HALF_WIDTH_M


def round_half_up(value: float) -> int:
    """半值向正无穷取整（half-up），区别于 Python 内置的 half-to-even。"""
    return math.floor(value + 0.5)


@dataclass(frozen=True)
class ZoomGrid:
    """一个 zoom 的全局整数网格描述（含实际变换参数，供清单/报告记录）。"""

    z: int
    extent: int
    matrix_size: int          # n = 2**z
    tile_span_m: float        # 一块瓦片的地面跨度（米）
    resolution_m: float       # 一个网格单位对应的米数
    origin_gx: int            # 米坐标 x=-W 处的全局网格 x（应为 0）
    origin_gy: int            # 米坐标 y=+W 处的全局网格 y（应为 0）
    rounding_rule: str

    def to_global_x(self, x_m: float) -> int:
        """米 x -> 全局网格 x（整数）。"""
        return round_half_up((x_m + WORLD_HALF_WIDTH_M) / self.resolution_m)

    def to_global_y(self, y_m: float) -> int:
        """米 y -> 全局网格 y（整数，y 轴向南增大）。"""
        return round_half_up((WORLD_HALF_WIDTH_M - y_m) / self.resolution_m)

    def to_local_x(self, x_m: float, tile_x: int) -> int:
        return self.to_global_x(x_m) - tile_x * self.extent

    def to_local_y(self, y_m: float, tile_y: int) -> int:
        return self.to_global_y(y_m) - tile_y * self.extent

    def local_to_global(self, local_x: int, local_y: int, tile_x: int, tile_y: int) -> tuple[int, int]:
        """瓦片局部坐标 -> 同 zoom 全局网格坐标（verify 解码复原时使用）。"""
        return local_x + tile_x * self.extent, local_y + tile_y * self.extent

    def global_x_to_m(self, gx: int) -> float:
        """全局网格 x -> 其代表的米 x（量化后网格线的位置，误差分析用）。"""
        return -WORLD_HALF_WIDTH_M + gx * self.resolution_m

    def global_y_to_m(self, gy: int) -> float:
        """全局网格 y -> 其代表的米 y。"""
        return WORLD_HALF_WIDTH_M - gy * self.resolution_m


def make_grid(z: int, extent: int) -> ZoomGrid:
    n = 2 ** z
    tile_span_m = WORLD_WIDTH_M / n
    resolution_m = tile_span_m / extent
    grid = ZoomGrid(
        z=z,
        extent=extent,
        matrix_size=n,
        tile_span_m=tile_span_m,
        resolution_m=resolution_m,
        origin_gx=round_half_up((-WORLD_HALF_WIDTH_M + WORLD_HALF_WIDTH_M) / resolution_m),
        origin_gy=round_half_up((WORLD_HALF_WIDTH_M - WORLD_HALF_WIDTH_M) / resolution_m),
        rounding_rule="half-up: floor(v + 0.5), ties toward positive infinity",
    )
    if grid.origin_gx != 0 or grid.origin_gy != 0:
        raise RuntimeError("世界角点未落在网格原点，网格定义存在数值错误")
    return grid


def tile_bounds_m(grid: ZoomGrid, tile_x: int, tile_y: int, buffer_units: int) -> tuple[float, float, float, float]:
    """瓦片（含 buffer）在源 CRS 中的裁切框 (minx, miny, maxx, maxy)，单位米。

    buffer 是整数瓦片单位：先换算成全局网格单位再换算成米，
    保证裁切框边缘与全局网格线对齐，而不是任意米制外扩。
    """
    buffer_m = buffer_units * grid.resolution_m
    minx = -WORLD_HALF_WIDTH_M + tile_x * grid.tile_span_m - buffer_m
    maxx = -WORLD_HALF_WIDTH_M + (tile_x + 1) * grid.tile_span_m + buffer_m
    # XYZ：tile_y 向南增大；局部 y=0 对应瓦片北缘（米 y 较大）
    maxy = WORLD_HALF_WIDTH_M - tile_y * grid.tile_span_m + buffer_m
    miny = WORLD_HALF_WIDTH_M - (tile_y + 1) * grid.tile_span_m - buffer_m
    return minx, miny, maxx, maxy
