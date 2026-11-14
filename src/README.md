# depsvg — 依赖关系图静态 SVG 层次布局生成器

纯 Python 3.11 标准库实现，**不调用 Graphviz / Dagre 等任何现成布局引擎**，
无网页编辑器、无浏览器依赖。读取节点、边与 display-plan，产出：

- 可直接打开的静态 SVG（每个图形元素带 `data-node-id` / `data-edge-id`）
- 与 SVG **同一套坐标对象**序列化出的可复核坐标 JSON
- 独立核验报告（解析实际 SVG 文本，不信任布局器内存状态）
- v1/v2 内联对照 HTML

规模约束：≤25 节点、40 边、一次更新；不做影响分析平台、大图压测、
字体自动量测、交互拖拽。

## 目录

```
src/depsvg/
  model.py        数据模型、输入读取、Tarjan SCC、弱连通分量
  text.py         确定性字符宽度估算、标签换行、几何原语（盒重叠/线段相交）
  layout.py       全量层次布局：SCC 缩点 DAG 分层、通道折线路由、标签占位
  incremental.py  增量布局：冻结旧坐标、刚体平移、位移报告
  svg.py          SVG 渲染（直接画 Layout 几何，杜绝两套坐标）
  verify.py       独立核验：解析 SVG + JSON + 原始输入交叉核对
  report.py       对照 HTML
  cli.py          layout / verify / report 子命令
tests/test_pipeline.py  端到端回归 + 对抗测试（18 项）
src/run.sh        一键复现
outputs/          v1/v2 的 SVG、JSON、核验报告、compare.html
```

## 使用

```bash
cd src
# v1 全量
python3 -m depsvg.cli layout --nodes ../fixtures/nodes-v1.json \
  --edges ../fixtures/edges-v1.csv --plan ../fixtures/display-plan.json \
  --svg ../outputs/v1.svg --json ../outputs/v1.json
# v2 增量（传入 v1 坐标 JSON）
python3 -m depsvg.cli layout --nodes ../fixtures/nodes-v2.json \
  --edges ../fixtures/edges-v2.csv --plan ../fixtures/display-plan.json \
  --old-json ../outputs/v1.json --svg ../outputs/v2.svg --json ../outputs/v2.json
# 独立核验
python3 -m depsvg.cli verify --nodes ... --edges ... --plan ... \
  --svg ../outputs/v2.svg --json ../outputs/v2.json --old-json ../outputs/v1.json
# 对照 HTML
python3 -m depsvg.cli report --v1-json ... --v1-svg ... \
  --v2-json ... --v2-svg ... --out compare.html
```

或直接 `bash src/run.sh`，再运行 `python3 tests/test_pipeline.py`。

## 布局设计

### 1. 有环图的层次处理（不删回边）

1. Tarjan 求强连通分量（SCC），SCC 缩点得到 DAG；
2. 每个弱连通分量内对缩点 DAG 做最长路径松弛得到层号，层水平推进、
   同层节点垂直堆叠；
3. 循环 SCC 用紫色虚线簇框圈出。SCC 内部再做一次 DFS：
   - DFS 树边/前向边走簇框**左侧**走廊（E03 dim→join、E04 join→quality）；
   - 指向祖先的真正回边走簇框**右侧**走廊（E05 quality→dim）。
   回边始终保留在图上并显式绕行，环不会从图中消失。
4. 自环（E14）为节点上方的显式五段 U 形折线，抬升到 SCC 簇框之上，
   标签置于 U 形顶部。

### 2. 节点盒与长标签

- 盒尺寸以输入给定宽高为准，**只能放大不能缩小**；
- 字符宽度按 display-plan 的确定性估算：ASCII=7px、非 ASCII=14px，
  贪心按空格/字符换行；水平留白 `node_padding=18`，垂直留白 4px
  （给定盒高已含业务内边距，避免无意义整体放大；确需放大仍以 info 报告）；
- 边标签盒宽度严格按 `max(36, 7*ASCII + 14*非ASCII)` 计算，verify 复核。

### 3. 边出入口与折线路由

- 起点/终点都在节点盒边界（右/左端口），自环为顶端口；
- 四类通道，**同一通道内每条边（含平行边）分配独立车道，横向错开 16px**，
  任何两条边都不会完全共线重合：
  - 相邻层：层间垂直通道；
  - SCC 内前向：簇框左走廊；回边：簇框右走廊；
  - 跨层长边（E10 raw→archive、E11 external→dim）：组件最右外走廊；
- 平行边（E06/E13 join→agg）端口再做 ±9px 纵向偏移，双箭头可辨；
- 边彼此允许交叉，交叉点只在线上、**绝不渲染圆点或任何类节点图形**，
  每条交叉以 info 给出两条 edge_id 与坐标；
- 标签沿最长线段中点两侧生成最多 12 个有界候选位，避开全部节点盒与
  已放标签；仍冲突时取首位并输出 `edge-label-conflict` 警告（带 edge_id
  与坐标），不会只报“布局成功”。

### 4. 独立组件

弱连通分量各自布局，垂直堆叠、组件间留 90px，底色带标注组件编号与节点数，
组件盒互不相交。

## 增量更新策略与稳定性取舍

- 旧节点坐标默认**逐点冻结**，旧边折线从 v1 JSON 原样恢复、不重新路由；
- 新节点按最长路径锚定层号，x 与所属层列对齐，放在所属组件块下方，
  有界重试寻找不与任何冻结盒重叠的位置；
- 仅当新块与下游独立组件间距不足时，把受影响的**整个组件做一次刚体
  平移**（节点与边连同标签一起移动），并在 `update.moved_nodes` 中逐节点
  报告 from/to、dx/dy 与原因；本数据中仅 catalog、audit 下移 278px；
- 引擎自检 + verify 双重保证：未出现在位移报告里的旧节点坐标必须与 v1
  完全一致，否则报 `unreported-displacement`。

## 核验项（verify，坐标取自实际 SVG 文本）

| 代码 | 含义 | 级别 |
| --- | --- | --- |
| missing/extra-node/edge-json/svg | 身份在 JSON 或 SVG 中缺失/多出 | error |
| edge-identity-mismatch | 边的 source/target 与输入不符 | error |
| svg-json-coordinate/polyline/label/canvas-mismatch | 画的与报的不是同一套坐标 | error |
| node-box-overlap / node-box-touch | 节点盒重叠 / 共边 | error / warning |
| edge-crosses-node | 折线穿过无关节点盒（带交点坐标） | error |
| arrow-endpoint-off-box | 箭头端点未落在源/目标盒边界 | error |
| parallel-edges-overlap | 平行边折线完全重合 | error |
| self-loop-not-routed | 自环退化为非显式折线 | error |
| edge-label-covers-node / edge-label-overlap | 标签压节点 / 标签互压 | error / warning |
| unreported-displacement / false-displacement-report | 增量位移与报告不符 | error |
| old-edge-rerouted | 未移动旧边被重新路由 | warning |
| edge-crossing | 允许的边交叉（定位用，非失败） | info |

`tests/test_pipeline.py` 含对抗用例，逐项构造上述缺陷并断言 verify 能抓到。

## 本数据集结果摘要

- v1：12 节点 14 边，2 个弱连通分量，2 个循环 SCC（dim/join/quality 三环、
  audit 自环），核验 0 error / 0 warning，12 条边交叉 info；
- v2：新增 tax、taxreport 与 E15–E17，旧 10 节点坐标冻结，catalog/audit
  因组件间距不足整体下移 278px（已报告原因），核验 0 error / 0 warning。
