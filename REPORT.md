# RDF Dataset 规范化与判等 — 设计说明

## 1. 词项身份(按 README / plan.json)

| 词项 | 身份键 |
|---|---|
| IRI | 原始字符串, 不做任何归一 |
| literal | (解码后词法值, datatype, 小写语言标签); plain 视为 `xsd:string`; **不做数值归一** (`"01"^^xsd:integer` ≠ `"1"^^xsd:integer`) |
| 空白节点 | 标签仅在本文件内有效, 跨文件不可比较 |
| 图 | 默认图与任何命名图不同; 图名可以是空白节点, 与主语/宾语空白节点同属一个 dataset 的节点集合 |

重复四元组折叠为集合。

解析器为自实现的 N-Quads 解析器(`rdfcanon/nqparser.py`)。不用 rdflib 直接解析的两点原因: 其默认 `NORMALIZE_LITERALS=True` 会把 `"01"^^xsd:integer` 归一成 `"1"`(可关, 但依赖全局状态); 且 rdflib 会把文件中的空白标签替换为内部 id, 丢失映射输出所需的原标签。`tests/test_rdfcanon.py::TestParserCrossCheck` 用 rdflib(关闭归一后)对全部 fixtures 交叉核对, 两种解析路径给出的规范字节完全一致。

## 2. 规范四元组格式(公开格式)

规范形式是**完整四元组信息**的文本序列化, 不只是邻域哈希:

* 行 = `S P O [G] .`, 单空格分隔, 默认图省略第四项; 每行以 `\n` 结尾。
* IRI: `<...>`, 控制字符/DEL/空格及 `"<>\^`{|}` 转义为 `\uXXXX` / `\UXXXXXXXX`。
* literal: `"词法值"` 内 `\` `"` 反斜杠转义, `\t \n \r` 短形式, 其余控制字符 `\uXXXX`; 非 ASCII 原样输出(UTF-8)。后跟 `^^<datatype>` 或 `@lang`(小写)。plain literal 一律显式写成 `^^<http://www.w3.org/2001/XMLSchema#string>`。
* 空白节点: `_:c0` … `_:c{n-1}`, 编号由下文算法确定(n ≤ 8, 单位数)。
* 全部行按 UTF-8 字节序排序后连接; 输出字节 = UTF-8 编码; 另报 SHA256(hex)。

## 3. 规范化算法

### 3.1 关系邻域精化(多轮, 至不动点)

每个空白节点的签名 = `(当前颜色, 排序后的邻接描述符多重集)`。节点 b 在一条四元组中的描述符逐槽位记录: 自身出现的槽位、其余槽位的基项身份键(IRI / literal / 默认图)或其他空白节点的**当前颜色**。图槽位与主/谓/宾同样参与, 因此命名图及图名空白节点都进入邻域。签名只含基项与结构位置, 不含原标签与语句顺序。签名包含自身颜色, 划分单调变细, 至多 n 轮收敛。—— 只做一轮邻居哈希不足以区分(例如规则结构要传播多跳), 故迭代到不动点。

### 3.2 剩余对称: 个体化-精化搜索

精化后若仍有同颜色单元(如 C6 环、C3∪C3, 全部节点同度同色), 进入搜索:

* **分支规则**: 取颜色最小的非单点单元; 对单元内**每个**节点依次赋予新的最小颜色(个体化), 重新精化后递归。
* **搜索状态**: 当前颜色向量(一个划分)+ 个体化路径。每层至少分裂一个单元, 深度 ≤ n, 分支因子 ≤ 当前单元大小。n ≤ 8 时最坏为 K8 的 40320 个叶子, 实测约 4 秒; fixtures 与常规数据为毫秒级。
* **叶子**: 离散着色给出规范编号(按颜色排序), 序列化; 所有叶子取**字节最小**者为规范形式。
* **平局**(自同构产生多份相同最小序列化): 按 `(原标签, 规范编号)` 字典序取最小的一份标号。这只影响输出的节点映射选哪一份, 不影响规范字节。

把同颜色节点直接合并是错的(C6 与 C3∪C3 全部节点同色但不同构), 必须靠搜索后的完整序列化区分 —— `symmetric.nq` vs `nonisomorphic.nq` 正是此例。

### 3.3 重命名不变性

初始颜色全相同; 签名是排序后的多重集; 序列化对行排序; 个体化对单元内所有节点**对称枚举**并取全局最小。重命名空白标签或调换语句顺序只会置换内部下标, 候选序列化**集合**不变, 故最小值(规范字节与 SHA256)不变。验证: 每个 fixture 25 次随机换标签+换序, 规范字节全部一致(150/150)。

## 4. compare

* 判等依据规范字节相等(等价于 SHA256 相等, 实现按字节比较)。
* 等价时: 两边同一规范编号的空白节点互相对应, 给出 `bnode_mapping_a_to_b`(原标签对原标签); 并给出每条原语句(按行号, 重复语句各自列出)到规范语句的来源映射。对称图允许存在多份正确对应, 按 3.2 的平局规则稳定选一份; 所选映射经**逐四元组重放**核验(作用于 A 的四元组集合必须精确等于 B), 结果记入 `verification.mapping_replay_ok`。
* 非等价时: 保留两份规范形式, 输出规范四元组的集合差(`only_in_a` / `only_in_b`)。不声称这是最小编辑, 也不对实体相似性做任何猜测。

## 5. 验证

`tests/test_rdfcanon.py`(18 项, 全部通过):

* fixtures: base≡renamed、base≢changed(仅差 `"01"` vs `"1"` 词法)、symmetric≡symmetric-renamed、symmetric≢nonisomorphic(同度非同构)。
* 词项身份: 数值词法不归一、plain≡xsd:string、语言标签大小写不敏感、默认图≠命名图、图名空白节点参与结构、重复语句折叠。
* 微型图独立核对: n=3 全部 64 个有向图两两对照(2016 对) + 环/环并/路径族 + 300 组随机数据集(≤6 节点, 含各形 literal 与命名图), 判定结果与**独立暴力枚举全部双射**的判等器完全一致; 等价对全部逐四元组重放通过。
* 随机重命名: fixtures 每个 25 次 + 60 个随机数据集各 5 次换标签/换序, 规范字节不变。
* 解析器与 rdflib 交叉核对(见 §1)。

`verify.py` 重新生成全部交付物并输出上述核对的摘要(`out/verification-summary.json`)。

## 6. 限制

* 范围按 plan.json: ≤ 8 空白节点、≤ 60 四元组; 超出报错。不做 SPARQL、网页抓取、RDF 推理。
* 最坏情况(K8 级全对称)约 4 秒; 未实现完整 URDNA2015, 格式为本报告 §2 的公开自定义格式。

## 7. 运行

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python cli.py canonicalize fixtures/base.nq --out out/
.venv/bin/python cli.py compare fixtures/base.nq fixtures/renamed.nq --out out/
.venv/bin/python -m unittest discover -s tests   # 测试
.venv/bin/python verify.py                        # 生成交付物 + 独立复核
```

交付物(`out/`): 每份 fixture 的 `*.canonical.nq`(规范 N-Quads)与 `*.canonical.json`(SHA256 + 规范标号 + 诊断); 四组对比的 `compare-*.json`(映射 + 语句来源映射, 或规范差异); `verification-summary.json`。
