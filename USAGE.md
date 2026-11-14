# 离线发布审计工具 — 使用说明

`release_audit.py`(Python 3.11, 仅标准库 + git 命令行)读取给定 Git bundle 与发布计划,
产出净变更清单、来源证据与可复核 HTML。输入仓库只读: 工具只把 bundle 克隆到临时目录,
不修改输入、不推送。

## 命令

```bash
# 1) 审计: 解析提交 DAG, 产出净差量包 + 来源证据 + HTML 报告
python3 release_audit.py audit  --bundle fixtures/repo.bundle \
    --plan fixtures/release-plan.json --out out

# 2) 验证: 从 base 树在独立临时目录重放净差量, 逐文件核对 target 树身份
python3 release_audit.py verify --bundle fixtures/repo.bundle \
    --plan fixtures/release-plan.json --out out
```

`verify` 退出码: 全部一致为 0, 存在不一致为 1。

## 产物

| 文件 | 内容 |
|---|---|
| `out/netdiff.json` | 净差量包(自包含): 基线/目标 blob、行级净片段、二进制 blob 身份与内容 |
| `out/netdiff.patch` | 净差量的 unified diff 形式, 供人工复核 |
| `out/audit.json`   | 全量审计数据: 历史提交、净片段、涉及提交、父边对照、判定理由 |
| `out/report.html`  | 可复核报告(净变更 / 历史说明 / 重放验证 / 判定方法) |
| `out/verify.json`  | 逐文件重放核对结果 |

## 判定规则(证据充分性)

- **两种产物分开**: 历史新增提交集合(`release-base..release-target` 可达性)与
  最终树差异是两种产物; 发布前被撤销的修改只在历史说明中可见, 不计入净变更数量。
- **引入边**: 片段来源在区间全部提交上搜索"父无 → 子有"的边, 不凭第一个命中定来源。
- **合并对照**: 仅一方父版本含有 ⇒ 来自该分支; 双方父版本均含而 merge-base 不含 ⇒
  两分支独立引入(`multiple_candidate_origins`); 所有父版本均不含 ⇒
  合并解决阶段引入(`merge_resolution`)。
- **删除对称规则**: 非合并提交上的消失边是删除事件; 合并边上的消失若可由区间内更早的
  非合并删除解释则记为"沿用", 否则记为合并解决阶段删除。被删内容保留 base 路径与行范围。
- **重命名**: 内容相似度(difflib ratio, 确定性阈值 0.55); 多个同等候选保留歧义,
  按删除+新增处理并标注。
- **二进制**: 按计划 `binary_files` 清单(及 NUL 启发式)判定, 仅报告 blob 身份。
- 状态词汇采用发布计划的 `source_statuses`: `single_branch` /
  `multiple_candidate_origins` / `merge_resolution` / `unresolved`
  (`moved_base_content` 为对枚举的扩展, 表示内容来自 base 的移动/复制)。

## 给定材料审计结论(fixtures/repo.bundle)

- `service.txt`(由 `app.txt` 重命名, 相似度 0.8): `mode: balanced` 为合并解决引入
  (合并 `b5d83f0` 的两父分别为 `fast`/`safe`); `mode: base` 的删除有
  **两个独立来源**(`65e5203`、`5570222`); `obsolete: remove-this` 由 `65e5203`
  删除, 两个合并边上的消失均为沿用。
- `ambiguous.txt` 的 `shared: enabled` 由 `a7c5183` 与 `36591ef` **独立引入相同文本**
  (合并两父均含、merge-base 不含)。
- `notes.txt` 的 `branch-b: accepted` 来自单一提交 `66c5408`。
- `logo.bin` 为二进制修改, 仅报告 blob 身份(`eaf36c1d` → `b737a8c1`)。
- `1e2868f` 的 timeout 实验在 `9fbcd3b` 被撤销: 仅历史可见, 不计入净变更。
- `verify`: 4/4 文件重放一致 —— 净差量包可完整复现 target 树。

这些正是只拼接 `git log`/`git diff`/`git blame` 无法说明的情形:
target 的 blame 看不到已删除的 `obsolete`/`mode: base` 行, 也无法区分
"两分支独立引入同一文本"与"合并解决产物"。

## 附加测试

`make_demo_repo.sh` 构造一个合成仓库(`demo.bundle`, 计划 `plan.json`),
覆盖给定仓库未出现的工具路径: 歧义重命名并列(同等相似度候选)、二进制新增、
整文件移动/复制、变更再撤销。结果在 `out-demo/`。
