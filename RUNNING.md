# 有限关系规则求解器 — 运行说明

本目录实现对 `README.md` 所述无函数 Datalog 子集的 `check` / `derive` /
`explain`，仅依赖 Python 3.11 标准库（`tests/` 使用 `unittest`）。不使用
PostgreSQL；数据库无关的纯内存实现。

## 文件

| 文件 | 作用 |
| --- | --- |
| `datalog.py` | 单文件实现：解析/校验、分层、半朴素求值、证明与解释、朴素基准 |
| `tests/test_datalog.py` | 回归测试（循环、重复事实、跨层否定、坏程序、重排不变性、tiny 基准、证明重放） |
| `fixtures/` | 题目提供的基础事实、schema、规则、查询与两个坏程序 |

## 命令

均在仓库根目录执行。输出为 JSON 到 stdout；`--out FILE` 写入文件。

```sh
# 1) 只做静态校验（schema / 规则安全 / 分层），不求值
python3 datalog.py check \
  --schema fixtures/schema.json --rules fixtures/rules.json \
  --facts fixtures/facts.jsonl

# 2) 求值：分层关系、每轮 delta、连接候选统计、全部派生证明
python3 datalog.py derive \
  --schema fixtures/schema.json --rules fixtures/rules.json \
  --facts fixtures/facts.jsonl --queries fixtures/queries.json

# 3) 解释查询（存在则给证明，不存在则报告饱和后的未导出状态）
python3 datalog.py explain \
  --schema fixtures/schema.json --rules fixtures/rules.json \
  --facts fixtures/facts.jsonl --queries fixtures/queries.json

# 4) 用 tiny 材料核对：逐关系比较半朴素闭包与独立的朴素全量闭包
python3 datalog.py verify-tiny \
  --schema fixtures/schema.json --rules fixtures/tiny-rules.json \
  --facts fixtures/tiny-facts.jsonl

# 5) 回归测试
python3 -m unittest discover -s tests -v
```

退出码：`0` 成功；`1` 校验失败（坏程序 / 非法输入）或核验不一致；`2` 参数错误。
`check` 与 `derive` 遇到坏程序都会以 `"status": "invalid"` 与逐条 `errors`
（含 `code`、`rule_id`、负环证据链 `details.cycle`）退出，不进行求值。

## 实现要点

### 输入与校验（`check`）

* 原子项：以 `?` 开头的非空字符串是变量，其余按原字符串常量比较。头单原子，
  `positive`/`negative` 列表均按合取解释；至少一个正原子。
* 校验项：JSON 形状、schema 元数（含事实与查询）、谓词角色（extensional 只来自
  输入、不得作规则头；intensional 不接受输入事实）、规则 id 唯一、谓词与规则
  数量上限（8 / 20）、变量安全。
* **变量安全**：头变量与负体变量必须出现在正体中（重复位置与常量都遵守）；违反
  者整程序拒绝，例如 `bad-unsafe.json` 同时报 `rule.unsafe_head_variable` 与
  `rule.unsafe_negative_variable`。
* **分层**：按"体谓词 → 头谓词"的有向依赖图（正边、负边分别标注）求 SCC。
  同层内不得存在负边；一旦出现，即使当前数据为空也让程序失败，并在
  `details.cycle` 中给出经过该负边的具体规则证据（`bad-negative-cycle.json`
  的 n1/n2 互负依赖）。仅正边的环允许（`reach` 的自递归）。extensional 谓词合并
  为第 0 层基础事实；派生层按凝聚 DAG 的拓扑序编号。

### 半朴素求值（`derive`）

每个层先跑**非递归基础规则**一轮（第 1 轮），再对**递归规则**做半朴素 delta
迭代：

* 每一轮只考虑"至少一个递归体原子读本轮 delta、其余读当前完整关系"的变体
  （`delta_variants` 记录变体与 delta 大小）。若某递归体原子的 delta 为空，该
  变体跳过；多递归原子规则会为每个非空 delta 位置生成一个变体，可能重复匹配，
  由候选去重吸收，绝不把"历史上所有组合"再算一遍。
* 候选按事实去重：已存在事实与同轮已入选的候选都不再产生新事实。多种证明时
  按 `(rule id, 绑定, 正前提)` 的稳定序保留最小者，因此事实或规则重排不改变
  事实集合与证明选择。
* 停止条件：某轮没有任何新事实即饱和，该空轮作为停止轮记录；非递归层在基础轮
  后再记一个空轮作为停止点。每层 `stable_at_round` 给出停止轮。
* **否定**只读取严格低层的已完成关系（`check_negative` 在运行时再次断言该
  谓词所在层 `<` 头所在层），不会因为某事实尚未推导出来就提前判其不存在。
* 统计：每轮每规则的 `join_probes`（实际 (绑定, 元组) 探测次数）、
  `complete_matches`（完整匹配实例）、`duplicate_matches`（候选去重命中）、
  `negative_rejected`（被负查询否决）、`new_facts`；顶层汇总各轮。

### 证明与解释（`derive --queries` / `explain`）

* 每个派生事实保存一份证明：`rule_id`、完整变量绑定、按体位置的
  `(atom_index, fact)` 正前提（标注 `input`/`derived`）、每条负查询的
  `checked_stratum`、`stratum_complete`、`missing` 与当时关系大小。
* 证明有限且无环：新事实只在正前提都已存在于更早轮次（或输入事实）时才记录，
  因此 `reach` 这类循环可达关系不会产生循环证明。`reach(A, A)` 借助
  `reach(A, B)`（第 1 轮）与 `link(B, A)` 得到，不是自引用。
* 查询存在：输出证明与可读的证明树（可 `explain` 命令内直接阅读）。查询不存在：
  `status = "not_derived"`、`saturated = true`，说明该层已到不动点且无正证明，
  **绝不编造正证明**。
* `verify_proofs` 对所有派生事实逐条重放：头部实例化、正前提覆盖且真的在对应
  关系中、派生前提严格早于结论、负原子的元数/所在层严格更低且在最终关系缺失
  （即引用的是已闭合层）。任何不一致进入 `problems`，`ok=false`。
  `derive` 与 `explain` 输出均附带 `proof_verification` / `verification`。

### 独立正确性基准（`verify-tiny`）

`naive_full_closure` 是刻意独立的实现：不使用 delta、不使用主程序的连接器，
逐层地把该层所有规则对**完整关系**反复应用直到无变化；只在低层闭包完成后才
检查否定。`verify-tiny` 逐谓词比较半朴素结果与朴素闭包，全部一致才 `ok=true`，
同时重放全部证明。tiny 数据是 `a→b→c` 加回边 `b→a`，`reach` 闭包为
`{a,b}×{a,b,c}`（6 条），两个实现一致。

### 题目 fixtures 结果摘要

* 基础事实 13 条（含 1 条重复 `link(S1,A)`，去重后 13 唯一事实、7 条 link）。
* 层：第 0 层 extensional（`banned/link/supplier/suspended`）；第 1 层 `reach`
  （正递归，3 个生产轮 + 1 停止轮，delta 7/7/1/0）；第 2 层 `allowed`；
  第 3 层 `eligible`；第 4 层 `review`。汇总：6 个生产轮、206 次连接探测、
  41 个完整匹配、5 次重复候选、3 次被负查询否决、33 个派生事实。
* `reach` 15 条（A/B 双向环产生自达与互相可达）、`allowed` 14 条（`banned(S1,W)`
  排除一条）、`eligible = {S2}`（S3 被 suspended、S4 无路径到 W）、
  `review = {S1,S3,S4}`。
* 查询：`eligible(S2)` 导出自 r4；`review(S1)` 导出自 r5（`eligible(S1)` 在已闭
  合的更低层缺失）；`reach(A,A)` 由环导出；`eligible(S4)` 未导出，报告饱和。
* 输入事实/规则重排后事实集合与证明选择一致（测试覆盖）。
