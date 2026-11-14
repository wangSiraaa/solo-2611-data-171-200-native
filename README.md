# 离线 MVT 瓦片生产（EPSG:3857 米坐标，含孔面/跨瓦片边界）

把少量 LineString 与含孔 Polygon 从 EPSG:3857 米坐标离线编码为真实 MVT 瓦片。
不接任何地图服务、不做重投影/简化/反经线处理；只生成 `fixtures/plan.json` 给定的瓦片。

## 运行

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m mvt_pipeline build     # 生产瓦片 + 清单 + 几何报告
.venv/bin/python -m mvt_pipeline verify    # 独立解码并做几何级核对
PYTHONPATH=. .venv/bin/python scripts/cross_check.py   # 与第三方库解码器交叉验证
PYTHONPATH=. .venv/bin/python -m pytest tests/ -q      # 回归测试（含小孔塌缩）
```

## 设计要点

### 1. 每 zoom 一套全局整数网格（`mvt_pipeline/grid.py`）

- `n=2^z`，瓦片地面跨度 `span=2W/n`，网格分辨率 `res=span/extent`（W 为世界半宽）。
- 全局网格坐标：
  - `gx = floor(((x_m + W) / res) + 0.5)`
  - `gy = floor(((W - y_m) / res) + 0.5)`（y 轴向南，符合 XYZ）
- 唯一舍入规则 **half-up（半值向正无穷）**，区别于 Python 内置的 half-to-even，tie 行为确定且已在报告披露。
- 瓦片局部坐标只是平移：`local = global - tile * extent`。同一米坐标点在任意瓦片内得到相同全局整数，
  不存在每瓦片各自的偏移或舍入规则。恰落在瓦片边界上的点在相邻瓦片中是等价表达（如 0 与 extent）。

### 2. 裁切与量化顺序（`mvt_pipeline/pipeline.py`、`clipping.py`）

1. **先在源 CRS（米）内裁切**，再按全局网格量化；不使用编码库自带的 per-tile quantize。
2. **buffer 是整数瓦片单位**（16），仅经 `res` 换算成米来确定裁切框，局部坐标允许范围 `[-16, 4112]`；
   buffer 绝不参与任何米制偏移。
3. 面裁切不用 shapely/GEOS 的 polygon-polygon intersection：实测 GEOS 3.14 在“裁切框角落落入孔内”时
   会静默丢孔（`clip_by_rect` 同样受影响，`tests/` 与代码注释中有最小复现说明）。改用自实现的
   Sutherland–Hodgman 逐环裁切，孔结构显式保留。
4. 量化后去连续重复点；线唯一顶点 <2 判塌缩；面环零面积判塌缩。外环塌缩丢整个片段，
   **孔环塌缩只丢该孔并逐条登记原因与所在瓦片，外环保留**——绝不静默填实。
5. 按 MVT（y 向下）强制外环顺时针、孔逆时针，再交 `mapbox_vector_tile.encode` 输出真实 protobuf。

### 3. 裁切边界上的孔拓扑（量化不可见的两次调整）

裁切框角落落在源孔内时，孔环会与外环在裁切边上相切，而 OGC 简单要素禁止内环与外环相切：

- 米空间：贴边孔环节点外扩 1e-6 m、贴边外环节点外扩 2e-6 m，使孔严格位于外环内；
  该位移远小于 z10 半格（4.777 m），量化后不可见（报告含数值证明）。
- 整数网格空间：若量化后仍在 buffer 边界整数坐标相切，把孔环贴边点向内平移 1 个整数单位；
  只影响 buffer 区，瓦片真实空间 `[0, extent]` 内的覆盖与公共边界衔接不变。
- 两次调整都不删除环；详见 `output/report/geometry_report.md` 第 7 节。

### 4. 独立验证（`mvt_pipeline/verify.py`、`mvt_codec.py`）

- `mvt_codec.py` 只用 protobuf 生成类解析线格式，MVT 命令流（MoveTo/LineTo/ClosePath、zigzag 差分、
  环分组、属性 tag）全部自行实现，不调用库的 decode。
- 解码后把局部整数复原为同 zoom 全局网格整数，核对：
  - layer/version/extent、feature id 与属性类型（`false`、`0` 不缺失，bool 不被当 int）；
  - 环方向（外环顺时针、孔逆时针、无零面积环）；
  - 相邻瓦片公共边界上的线端点集合与面覆盖区间完全一致；
  - 边界线（feature 4）在四瓦片内 gx 恒定、gy 区间连续无缺失；
  - 面单位单元格覆盖与独立从源几何重算的期望集合逐格相等（含孔）；
  - 量化误差不超过半格、窄面/孔塌缩记录、随机瓦片顺序重建逐字节一致。
- `scripts/cross_check.py` 再与 `mapbox_vector_tile.decode` 的结果交叉比对，双解码器几何/属性一致。

## 产物（`output/`）

```
tiles/z/x/y.mvt              真实 protobuf MVT
manifest/tile_z_x_y.json     每瓦片清单：来源 feature、裁切框、变换、逐环/逐线量化证据
manifest/manifest.json       全量清单（含每 zoom 网格参数与舍入规则）
report/geometry_report.md    量化误差、塌缩范围、孔洞处置、拓扑调整、provenance
report/geometry_report.json  同上的机器可读版本
verify_report.txt            91 项核对结果
```

## 范围限制

仅支持 LineString 与含孔 Polygon；属性仅 string/bool/非负整数。不做地图简化、重投影、
反经线、在线缓存或样式；不面向全国数据或大规模压测。
