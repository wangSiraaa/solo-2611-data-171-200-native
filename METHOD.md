# 小 BAM 片段等位提取与精确相位（方法说明）

本目录用 Python 3.11 + pysam 对已给定的双等位杂合 SNV 做两件限定的工作：

1. 从真实短读 BAM 中**提取**每个片段在每个 SNV 上的等位证据；
2. 用**精确最小化** weighted MEC 的目标把每个连通块恢复成两条互补单倍型。

不做 read 比对、不做变异 calling、不做 indel 位点、不做全基因组基准或医学解释。

## 文件

| 文件 | 作用 |
|---|---|
| `phase_small.py` | 主程序：读取 BAM/SNV 表/reference/plan，写全部 CSV/JSON |
| `verify_phase.py` | 独立第二实现：重放证据、逐 CIGAR 手工核对、重解 MEC/反相代价、乱序重放、数量闭合 |
| `audit_tiny.py` | tiny 例的全枚举审计（8 种 4 位单倍型 + 每个相邻对的反相枚举） |
| `.venv/` | Python 3.11 虚拟环境，`pysam==0.22.1`（见 `requirements.txt`） |
| `output/` | 主 BAM 的全部交付表 |
| `output/tiny/` | tiny 例的交付表 + 全枚举审计表 |

### 运行

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 主案例
.venv/bin/python phase_small.py \
  --bam fixtures/reads.bam --variants fixtures/variants.csv \
  --reference fixtures/reference.fa --plan fixtures/plan.json \
  --outdir output

# 独立核验
.venv/bin/python verify_phase.py \
  --bam fixtures/reads.bam --variants fixtures/variants.csv \
  --reference fixtures/reference.fa --outdir output \
  --summary output/verification.json

# tiny 例 + 全枚举
.venv/bin/python phase_small.py \
  --bam fixtures/tiny.bam --variants fixtures/tiny-variants.csv \
  --reference fixtures/reference.fa --plan fixtures/plan.json \
  --outdir output/tiny
.venv/bin/python verify_phase.py \
  --bam fixtures/tiny.bam --variants fixtures/tiny-variants.csv \
  --reference fixtures/reference.fa --outdir output/tiny \
  --summary output/tiny/verification.json
.venv/bin/python audit_tiny.py \
  --bam fixtures/tiny.bam --variants fixtures/tiny-variants.csv \
  --reference fixtures/reference.fa --outdir output/tiny \
  --evidence output/tiny/evidence.csv
```

## 1. 过滤与按身份配对

- 过滤：`unmapped`、`secondary`、`supplementary`、无 CIGAR、`MAPQ < 20`（阈值取自 `plan.json`）。
  被过滤记录逐条写进 `phase_result.json` 的 `diagnostics`，并带原因。
  主案例中唯一的 secondary（`frag02`，flag 355，MAPQ 5）在此被剔除。
- **配对只按 `query_name` 的 read1/read2 身份**，绝不按相邻行。
  主 BAM 中 `frag01` 在行 0 与 2、`frag09` 在行 6 与 11、`frag15` 在行 20 与 22，
  `frag02` 还有一条被过滤的记录夹在中间；全部仍按名字正确配对。
- 完整性诊断：同名重复 read1/read2、缺失一端、非 read1/read2 的 primary 记录都会记录为
  `duplicate_mate` / `incomplete_fragment` / `unpaired_flag`，不继续使用该片段，
  也不补造等位。

## 2. CIGAR → query 碱基与质量

对每个 `片段 × 位点`，把 1-based 参考坐标换算成 CIGAR 局部参考偏移后逐 op 前移：

| op | 参考 | query | 处理 |
|---|---|---|---|
| `M` / `=` / `X` | 消耗 | 消耗 | 命中则 `query_index = q + (local - r)` |
| `I` / `S` | 不消耗 | 消耗 | 只推进 query，从而后续 `M` 的 query 索引正确偏移 |
| `D` / `N` | 消耗 | 不消耗 | 命中则**无碱基**，记 `deletion` / `refskip` |
| `H` / `P` | — | — | 跳过 |

关键点：

- **reverse flag 不会让已按比对方向存储的 SEQ 再次反向互补。** 程序只读
  `rec.query_sequence[qi]` / `rec.query_qualities[qi]` 的原始值。主 BAM 中的
  `frag01 read2`（flag 147）等所有反向 mate 都按存储方向直接取碱基。
- 端点落在比对区间外 → `outside`（无覆盖），记录参考区间与原因。
- 参考碱基与表中 `allele0` 不一致 → `ref_mismatch`；此处仅诊断，因为本任务不 calling。
- 碱基不属于 `{allele0, allele1}` → `nonref_alt`；`Q < 20` → `low_base_qual`。
  以上状态都**保留原碱基/原质量**，绝不填成 0 等位。

每条原始观测（两端各自一行）写入 `end_observations.csv`：fragment、variant、mate、
flag、reverse、MAPQ、CIGAR、参考起点、**query 索引、原碱基、原 Q**、状态与原因。

## 3. 片段内 mate 合并（固定质量规则）

同一片段同一位点最多产生**一条**证据，规则严格取自 `plan.json`：

| 情形 | 等位 | 权重 | decision |
|---|---|---|---|
| 两端都无效 | 无 | 无 | `no_call` |
| 只有一端有效 | 该端等位 | 该端 Q | `single_read1/2` |
| 两端有效且一致 | 一致等位 | `max(Q1,Q2)` | `mates_agree` |
| 两端有效且冲突，`|Q1-Q2| >= 10` | 高 Q 的等位 | 质量差 `|Q1-Q2|` | `adjudicated` |
| 两端有效且冲突，质量差 < 10 | 无 | 无 | `undecided` |

因此重叠 mate **只贡献一份位点证据**，不会把两端权重相加。主案例中的具体表现：

- `frag09 v2`：read2 落在区间外 → `single_read1`，权重 30；
- `frag09 v5`：read1 落在区间外 → `single_read2`，权重 30；
- `frag09 v3/v4`：两端都 Q5 → `no_call`（低质量，不参与 MEC）；
- `frag10 v3`：read1 A/Q40 vs read2 G/Q20，差 20 → `adjudicated` 为 A，权重 20；
- `frag10 v4`：A/Q30 vs G/Q35，差 5 → `undecided`，不参与 MEC；
- `frag15 v7`：read1 A/Q5（低质量）且 read2 恰好落在 `24M1D28M` 的 D 上 → `no_call`。

合并表 `evidence.csv` 每行都带两端完整的来源字段（CIGAR、query 索引、碱基、Q、状态），
因此任一证据都能回到原 read。`corrected_observations.csv` 汇总了
`single_read1/2`、`adjudicated`、`undecided` 这几类非普通一致观测，便于审阅。
`locus_diagnostics.csv` 给出每个位点的可用证据数、decision/end 状态计数，
以及是否进入连通块（无覆盖位点绝不会被写成 0 等位）。

## 4. 连通块

- 仅用**有效共覆盖**建边：存在某片段在两个位点都给出可用（非 None）证据，才连边。
- 单点不因距离与别人相连；无可用证据的位点留给诊断表。
- 不按基因组距离强行连边，所以本数据得到 3 块：
  `v1..v8`、`v9..v11`、`v12`。
- 每块规模均满足 `max_variants_per_block=12`、`max_fragments_per_block=40`；
  超出时会写 `block_limit_exceeded` 诊断而不是悄悄拆分。

## 5. 精确 weighted MEC

对块内单倍型 `h`（长度 = 位点数），片段代价为

```
loss(f) = min( Σ_{位点∈f} w·[h_i ≠ a_i] ,  Σ_{位点∈f} w·[1-h_i ≠ a_i] )
```

块目标为 `Σ_f loss(f)`。求解是**对全部 2^(n-1) 种赋值的穷举**（首位固定 `h_0 = 0`
以消除整体交换），而不是逐边多数票。并列最优时取字典序最小者作为稳定解
（仅用于确定性，不赋予概率含义）。

片段归属：比较 `mismatch_to_h` 与 `mismatch_to_complement`，较小者得到 `h` / `c`；
相等时记为 `u`（`unassigned_tie`），保留未定，不强行指派。

主案例结果：

| 块 | 位点 | MEC | 主 h |
|---|---|---|---|
| `v1..v8` | 8 | 0 | `01001101` |
| `v9..v11` | 3 | 30 | `001` |
| `v12` | 1 | 0 | `0` |

`v9..v11` 的 30 来自 `frag12`：它在 `v9=0, v10=1` 上对两条相位都同等不一致，
归属为 `u`，但仍按定义贡献 30 的 MEC；`frag11` 与 `frag13` 各自完全支持一条相位。
这正是“整体求解”与“逐边多数票”会给出不同结论的情形。

## 6. 相邻位点反相 probe

对块内**位置相邻**的每一对位点 `(i, i+1)`：
强制 `h_i XOR h_{i+1} = 1 - (主解相对相位)`，重新穷举最小化，输出
`forced_cost` 与 `increment = forced_cost - main_cost`。

- `increment = 0` 表示存在**同代价替代解**，`same_cost=True`，程序保留一份同值替代
  （`alternative_haplotype`），不要求枚举所有最优解。
- `increment > 0` 只说明强制反相需要多付的整数代价；**不把代价差当概率**。
- 主案例中 `v9–v10` 的增量恰为 0（`010` 与主解 `001` 代价同为 30，是一份同值替代）；
  其余相邻对与 tiny 例的所有相邻对增量均 > 0。验证脚本会对每个 probe 独立枚举并核对
  `forced_cost`、`increment`、`same_cost` 与替代解的最优性。

## 7. 交付物

`output/`（及 `output/tiny/` 同构）：

| 文件 | 内容 |
|---|---|
| `end_observations.csv` | 每个 mate 的原始观测（含诊断状态） |
| `evidence.csv` | 每片段每位点合并后的唯一证据 + 两端来源 |
| `corrected_observations.csv` | 发生裁决/单端/未定的证据 |
| `blocks.csv` | 连通块、成员、片段数、MEC、主单倍型 |
| `phase_calls.csv` | 每位点的单倍型取值 |
| `fragment_losses.csv` | 每块内每片段的 loss、归属与两侧不一致和 |
| `local_probes.csv` | 每相邻对反相后的最小代价与增量 |
| `locus_diagnostics.csv` | 每位点可用证据与状态计数、是否入块 |
| `phase_result.json` | 全部结构（含过滤诊断、片段状态、block/probe 明细、计数） |
| `verification.json` | 独立核验结果（`ok` + 记录） |
| `output/tiny/tiny_enumeration*.csv/json`, `tiny_probe_enumeration.csv` | 全枚举审计 |

## 8. 验证做了什么

`verify_phase.py`（不 import 主求解器）与 `audit_tiny.py` 覆盖：

- 过滤/按名配对重放；
- **逐 CIGAR 手工核对**：`2I`（frag01 v1/v2 的 query 8/30）、`4S/3S`（frag02 v4 的 query 52）、
  `=/X`（frag07 v1/v3）、`24M1D28M`（frag15 v7 无碱基、v8 query 43）、反向 mate（frag01 read2）；
- 逐行重放整张 evidence 表（allele/weight/decision 全字段一致，主案例 180 行、tiny 12 行）；
- 对 evidence 行里的 read/CIGAR/query 索引/碱基/Q 重新从 BAM 取回逐项比对；
- 重新建块并独立穷举 MEC：主解代价一致且主解属于最优解集合；
- 每块片段 loss 逐片段一致且 `Σ loss == MEC`；
- 每个相邻 probe 独立枚举，核对 `forced_cost`、`increment`、`same_cost` 与替代解最优性；
- **BAM 记录顺序打乱**后重放（`/tmp/reads_shuf.bam` 等）与正式输出逐字节一致，证明身份不依赖行序；
- tiny 全枚举：8 个候选单倍型中唯一最优 `0100`，三个相邻对反相增量为 30/60/60。

两份 `verification.json` 均为 `"ok": true`，无问题项。
