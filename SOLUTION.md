# OSM 有向道路 / 转向图构建器

## 运行

```sh
# 默认读取 fixtures/roads.osm + fixtures/plan.json，写出 fixtures/roads.graph.json
python3 build.py

# 显式指定输入输出
python3 build.py fixtures/roads.osm fixtures/plan.json -o output/roads.graph.json
python3 build.py fixtures/unsupported.osm fixtures/plan.json -o output/unsupported.graph.json

# 指定 --strict 时，若存在 unsupported/ineffective 限制则退出码为 2
python3 build.py fixtures/unsupported.osm fixtures/plan.json --strict

# 测试（仅标准库，Python 3.11）
python3 -m unittest discover -s tests -v
```

`build()` 是对外函数入口，返回可直接 JSON 序列化的 `dict`。无需第三方依赖。

## 处理流程

1. **解析** `nodes/ways/relations`。XML 元素顺序不参与图身份：节点/道路按 ID 建表，输出全部排序。
2. **过滤道路**：只接受 `highway ∈ plan.allowed_highways` 的 way。缺失 `highway`、`highway` 不在
   plan、`nd` 不足 2 个、引用缺失节点/坐标、无法识别的 `oneway` 值都进入 `filtered_ways`
   并带定位诊断，不阻塞其余道路。
3. **物理 segment**：每条 way 的每一对相邻 `nd` 生成一个 segment，身份固定为
   `(way_id, segment_index)`。way 内顺序即 `nd` 顺序；**不按坐标排序、不合并相同坐标**。
4. **有向边**：`oneway=yes/1/true` 只生成正向，`-1` 只生成逆向，`no/0/false` 双向；
   `junction=roundabout` 且无 `oneway` 时按 plan 的 `roundabout_default_oneway`（本 plan 为正向），
   其余无 `oneway` 的 way 双向。长度用 plan 的 `earth_radius_m` 做 haversine。
5. **候选转向**：仅当前一条有向边 `end_node_id` 等于后一条 `start_node_id` 时产生候选。
   连接**只认相同 node ID**；相同坐标不同 ID、几何交叉、`bridge/layer` 都不会造出连接，
   但都会作为 `evidence` 输出。
6. **限制关系**：识别 `from`(way) / `via`(node) / `to`(way)。作用于“从 from way 进入 via”
   的有向边状态：
   - `no_*`：排除匹配 `to` 的后继；
   - `only_*`：只允许匹配 `to` 的后继，多个 `only_*` 取交集；
   - 多个限制总体取交集（`only` 限定集合，`no` 从集合中扣除）；
   - `from=to` 的 `no_u_turn`：只移除刚走过 segment 的**反向**有向边，保留同 way 正向延续与其它分支。
7. **不支持结构**：via-way、conditional、except、未知 restriction 类型、缺 from/via/to、
   缺引用、被过滤 way、from=to 的非 U-turn、via 非端点等，均进入
   `restrictions.unsupported` / `restrictions.ineffective` 并带 relation/way/node 定位，
   绝不静默忽略后宣称完整。
8. **输出自检** `verification`：从输出记录独立重建候选集，重算长度、nd 顺序、方向、
   转向分区、限制决策、关系分类分区、证据完整性。

## 输出结构（`build()` 返回 / `*.graph.json`）

| 键 | 内容 |
| --- | --- |
| `format`, `format_version`, `plan` | 格式标识与实际生效的 plan |
| `summary` | 计数、`default_u_turn_allowed`、`complete_for_supported_scope` |
| `nodes` | 仅被接受 way 使用的节点：`id/lat/lon/有向出入度/` |
| `physical_segments` | 每个物理 segment：`id/way_id/segment_index/start_node_id/end_node_id/length_m` |
| `directed_edges` | 每条有向边：物理 segment 身份、`direction`、起止节点、长度、坐标、`reverse_edge_id`、`bridge/layer/roundabout` 证据 |
| `allowed_turns` | `from_edge/to_edge/via_node_id`、双方 way/segment/方向、`connection_proof`（同一 node ID）、已检查的 only/no relation 及其目标集合 |
| `restricted_turns` | 被限制转移，附 `relation_ids`、`restriction(_kinds)`、完整 `relation_source(s)`（成员与 tag） |
| `restrictions.applied` | 每条生效限制的 from/via/to、进入边、目标边、逐进入边效果 |
| `restrictions.unsupported` | 不支持/缺引用结构及 `codes`、定位字段 |
| `restrictions.ineffective` | 结构可解析但因 oneway 等导致无进入边/无出边、无法作用的关系 |
| `restrictions.ignored_non_restriction_relations` | 明确非 restriction 且无 restriction 痕迹的关系（不属于“跳过限制”） |
| `filtered_ways` | 被过滤道路及原因 |
| `unsupported_restrictions`, `ineffective_restrictions` | 上述两类清单的顶层副本，便于单独消费 |
| `evidence` | 相同坐标不同 ID（未合并）、无公共节点几何相交、`bridge/layer` 标签 |
| `diagnostics` | 全部定位诊断（way/relation/node 定位键） |
| `provenance` | 被接受 way 的原始 `nds` 与 tags；被过滤 way ID |
| `edge_sources` | 每条有向边回查原 way、原 segment 的 `nd` 对 |
| `verification` | 自检结果（`ok/checks/errors`） |
| `graph_identity_hash` | 仅由 nodes/segments/edges/turns 计算的图身份；不受诊断差异影响 |
| `content_hash` | 整个文档（除 verification/自身）的哈希，包含诊断 |

`graph_identity_hash` 在 `roads.osm` 与 `unsupported.osm` 上相同：后者只多了一条应被诊断的
via-way 关系，却不应改变道路/转向图本身。`content_hash` 则不同，因为多出一条诊断。

## 本 fixture 的结果

`fixtures/roads.osm`：

- 接受 9 条 way（way 90 `footway` 被过滤），17 个物理 segment，28 条有向边，21 个节点。
- 候选转向 49 = 允许 44 + 受限 5；默认 U-turn 允许。
- 应用 3 条限制：
  - `100 no_left_turn`：进入 `e-10-1-f`（way10 正向到达 node 3）→ 禁 `e-30-0-f`（way30）。
    同状态的 way20/way40 出口仍允许。
  - `101 only_right_turn`：进入 `e-20-0-r`（way20 逆向到达 node 3）→ 只允许 `e-40-0-f`；
    同时排除 `e-10-1-r`、`e-20-0-f`、`e-30-0-f`。
  - `102 no_u_turn`：`from=to=way10`，仅移除 `e-10-1-f → e-10-1-r`（反向 segment），
    保留 `e-10-1-f → e-20-0-f`、`e-10-1-f → e-40-0-f` 等继续前进。
- `way 80` 为 `oneway=-1`，只生成 `32→31`、`33→32` 两条逆向边。
- `way 60` 环岛默认单向正向，四个 segment 依 way 内 nd 顺序首尾相接。
- `way 50`（bridge=yes, layer=1）与东西向道路几何相交但无公共 node：`evidence` 报告该交点，
  不生成任何跨 way 转向。
- `node 3` 与 `node 13` 坐标相同但 ID 不同：`evidence` 记录二者未合并，边/转向分属不同节点。

`fixtures/unsupported.osm`：图完全同上，另有 `relation 199`（via=way30）进入
`restrictions.unsupported`，`complete_for_supported_scope=False`。

## 验证

`tests/test_build.py` 共 42 个用例（`python3 -m unittest discover -s tests -v` 全绿），包括：

- 由输出边**独立枚举**候选转向并与 `allowed_turns ∪ restricted_turns` 对比；
  四条臂的小路口手算 `4×4` 交叉积 + 死端 U-turn。
- 逐条检查 `100/101/102` 及 only/no 组合、交集、逆向 oneway、环岛。
- 每条边回查原 way 的相邻 `nd`；长度重算；方向与 `oneway` 一致。
- 相同坐标不同 ID、无公共点相交、bridge/layer 均不产生连接。
- 打乱 XML 元素顺序后 `content_hash` / `graph_identity_hash` 不变。
- 各类不支持结构的定位诊断与 `--strict` 退出码。

## 明确不做

不做 GPS 匹配、路线优化、在线地图、地图前端、城市规模压测、速度模型或导航结论；
不猜测未在 plan 中声明的 access、条件或复杂中间 way 语义。
