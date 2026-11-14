"""独立 MVT 解码器。

只依赖 protobuf 生成类 vector_tile_pb2 来解析线格式，MVT 几何命令流
（MoveTo/LineTo/ClosePath、zigzag 差分、环分组、属性 tag）全部在本模块
自行实现，不调用 mapbox_vector_tile.decode，以满足“独立解码”要求。

坐标约定：瓦片按 y_coord_down 编码（本管线 build 侧固定如此），解码得到
的 (x, y) 即瓦片局部整数坐标，y 轴向南增大。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mapbox_vector_tile.Mapbox import vector_tile_pb2 as vt
from mapbox_vector_tile.utils import CMD_BITS, CMD_LINE_TO, CMD_MOVE_TO, CMD_SEG_END, zig_zag_decode

GEOM_TYPES = {0: "UNKNOWN", 1: "Point", 2: "LineString", 3: "Polygon"}


@dataclass
class DecodedRing:
    coords: list[tuple[int, int]]               # 闭合环（首尾点相同）
    area2: int                                  # 有向面积 2 倍（y 向下；正=顺时针）


@dataclass
class DecodedFeature:
    feature_id: int
    geom_type: str
    lines: list[list[tuple[int, int]]] = field(default_factory=list)
    rings: list[DecodedRing] = field(default_factory=list)
    properties: dict[str, object] = field(default_factory=dict)


@dataclass
class DecodedLayer:
    name: str
    extent: int
    version: int
    features: list[DecodedFeature]


@dataclass
class DecodedTile:
    layers: list[DecodedLayer]


_VALUE_FIELDS = (
    "string_value",
    "float_value",
    "double_value",
    "int_value",
    "uint_value",
    "sint_value",
    "bool_value",
)


def _parse_value(value: Any) -> object:
    # proto2 的 value 消息没有 oneof，各 *_value 为独立 optional 字段
    for field_name in _VALUE_FIELDS:
        if value.HasField(field_name):
            return getattr(value, field_name)
    raise ValueError("MVT value 未设置任何取值字段")


def _signed_area2(coords: list[tuple[int, int]]) -> int:
    area2 = 0
    for i in range(len(coords) - 1):
        area2 += coords[i][0] * coords[i + 1][1] - coords[i + 1][0] * coords[i][1]
    return area2


def _parse_geometry(commands: list[int], geom_type_int: int) -> tuple[list[list[tuple[int, int]]], list[DecodedRing]]:
    """把 MVT 命令流还原为绝对局部整数坐标。"""
    cursor_x = cursor_y = 0
    current: list[tuple[int, int]] = []
    line_parts: list[list[tuple[int, int]]] = []
    rings: list[DecodedRing] = []
    i = 0
    n = len(commands)

    while i < n:
        header = commands[i]
        i += 1
        cmd = header & ((1 << CMD_BITS) - 1)
        count = header >> CMD_BITS

        if cmd == CMD_MOVE_TO:
            if current:
                if geom_type_int == 3:
                    raise AssertionError("多边形在 ClosePath 前出现新的 MoveTo")
                line_parts.append(current)
                current = []
            for _ in range(count):
                dx = zig_zag_decode(commands[i])
                dy = zig_zag_decode(commands[i + 1])
                i += 2
                cursor_x += dx
                cursor_y += dy
                current.append((cursor_x, cursor_y))
        elif cmd == CMD_LINE_TO:
            for _ in range(count):
                dx = zig_zag_decode(commands[i])
                dy = zig_zag_decode(commands[i + 1])
                i += 2
                cursor_x += dx
                cursor_y += dy
                current.append((cursor_x, cursor_y))
        elif cmd == CMD_SEG_END:
            if not current:
                raise AssertionError("ClosePath 没有可闭合的环")
            current.append(current[0])
            rings.append(DecodedRing(coords=list(current), area2=_signed_area2(current)))
            current = []
        else:
            raise ValueError(f"未知 MVT 命令 id={cmd}")

    if current:
        line_parts.append(current)
    return line_parts, rings


def decode_tile(pbf: bytes) -> DecodedTile:
    tile = vt.tile()
    tile.ParseFromString(pbf)
    decoded_layers: list[DecodedLayer] = []

    for layer in tile.layers:
        features: list[DecodedFeature] = []
        for feature in layer.features:
            if len(feature.tags) % 2 != 0:
                raise AssertionError(f"layer={layer.name} feature={feature.id} tag 数量为奇数")
            props: dict[str, object] = {}
            for k in range(0, len(feature.tags), 2):
                key = layer.keys[feature.tags[k]]
                props[key] = _parse_value(layer.values[feature.tags[k + 1]])

            geom_type_int = feature.type
            gtype = GEOM_TYPES.get(geom_type_int, "UNKNOWN")
            line_parts, rings = _parse_geometry(list(feature.geometry), geom_type_int)
            features.append(
                DecodedFeature(
                    feature_id=feature.id,
                    geom_type=gtype,
                    lines=line_parts,
                    rings=rings,
                    properties=props,
                )
            )
        decoded_layers.append(
            DecodedLayer(name=layer.name, extent=layer.extent, version=layer.version, features=features)
        )
    return DecodedTile(layers=decoded_layers)
