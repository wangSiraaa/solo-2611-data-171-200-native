# 离线堆栈溯源工具（offline-symbolize）

把发布后 JavaScript 的打包行列，离线还原为原始源文件的行列，并给出**每一跳的定位依据**与可浏览的单文件 HTML。
纯 Node 20 实现、**零第三方依赖**（Base64 VLQ 自行解码），不启动浏览器、不联网、不读取任何远程 URL，也不支持 JS 以外的语言。

## 支持范围（刻意受限的 Source Map v3 子集）

- basic map：顶层 `sources` / `sourceRoot` / `sourcesContent` / `mappings`；
- indexed map：`sections` 且每节为**内嵌** map（inline section）；外部 URL section 直接报错；
- 映射链最多两跳（`manifest.max_hops = 2`）：生成文件 → 中间文件 → 原源文件；
- 不做编译器、异常聚合平台、代码修复或任意远程 URL 读取。

## 命令

工作目录为仓库根目录，Node 版本 `>=20`（开发与验证使用 v20.20.2）。

```sh
# 1) 生成 JSON + HTML 报告（对三份栈分别执行）
node tools/symbolize/bin/symbolize.js symbolize \
  --manifest fixtures/manifest.json \
  --stack fixtures/basic.cjs.stack.txt
node tools/symbolize/bin/symbolize.js symbolize \
  --manifest fixtures/manifest.json \
  --stack fixtures/indexed.cjs.stack.txt
node tools/symbolize/bin/symbolize.js symbolize \
  --manifest fixtures/manifest.json \
  --stack fixtures/synthetic-boundary.stack.txt

# 2) 重新执行材料中的 Node 程序，与采集记录比对用户帧
node tools/symbolize/bin/symbolize.js verify \
  --manifest fixtures/manifest.json --fixture-root "$PWD"

# 3) 手写小型映射的独立核验（节首行/后续行、无映射边界、UTF-16 列、VLQ 向量）
node tools/symbolize/bin/symbolize.js selftest
```

参数：

| 参数 | 说明 |
| --- | --- |
| `--manifest` | manifest 路径，默认 `fixtures/manifest.json` |
| `--stack` | 栈文本文件；其中的 `<FIXTURE_ROOT>` 占位与绝对路径都会被归一 |
| `--fixture-root` | 材料根目录，默认取 manifest 上两级 |
| `--alias` | 占位别名，默认 `<FIXTURE_ROOT>` |
| `--out-json` / `--out-html` | 自定义输出路径；默认写入 `<fixture-root>/reports/` |

输出：`reports/<栈名>.report.json` 与 `reports/<栈名>.report.html`，浏览器直接 `file://` 打开即可，无外部资源。

## 关键语义（与 manifest 对齐，均为本工具自行实现）

1. **坐标换算**：Node 栈行列 1 基 → map 行列 0 基；列按 **UTF-16 code unit** 计算，非 BMP 字符（如 `🧭`）占两个单元。
2. **片段选择**：同一**生成行**上选 `genCol ≤ 目标列` 的最后一个片段；覆盖区间为 `[seg.genCol, 下一片段.genCol)`，末片段到行尾。
3. **一字段片段**是显式“无映射”，命中即中断此前映射；空行/无片段行不得借用上一行位置。
4. **indexed 选节**：按 `(line,column)` 词序选 offset 不大于目标的最后一节；**section 的 column 偏移只在节首行扣除**，后续行保留原列。
5. **路径身份**：`map 所在目录 + sourceRoot + sources[i]` 组合，再校验落在 `allowed_roots` 内；拒绝绝对路径、协议 URL 与 `..` 越界；**不使用 basename 猜文件**。
6. **两级映射**：第一跳片段给出的原始行列**原样**作为第二张 map 的输入，不按生成列差值插值；每跳保存 map 摘要、section、选中片段与坐标换算。
7. **源内容**：物理文件优先；物理缺失时才允许用**该 map** 的 `sourcesContent` 顶替；两者皆无时只给位置证据，**不伪造摘录**。
8. **链路异常**分别标注：map 缺失/损坏、无映射区、片段非法、source 身份越界、链路循环；未知帧与 Node 内部帧保留原顺序与原因。

## 状态码含义

| status | 含义 |
| --- | --- |
| `located` | 到达原源文件且拿到源内容（磁盘或 sourcesContent） |
| `located-no-content` | 位置成立但物理文件与 sourcesContent 均缺失，无摘录 |
| `unmapped` | 命中一字段无映射片段、空行或早于行首片段 |
| `map-missing` | manifest 声明了 map，但文件缺失或无法解析 |
| `map-not-declared` | 生成文件不在 manifest.maps 中 |
| `node-internal` | `node:` 协议内部帧，按要求保留不解析 |
| `unknown` | 文件在 fixture 根之外，无法匹配本地身份 |
| `cycle` / `hop-limit` / `invalid-segment` / `source-forbidden` | 循环、跳数上限、片段越界、source 身份越界 |

## 目录

```
bin/symbolize.js          CLI：symbolize / verify / selftest
lib/utf16.js              UTF-16 单元工具
lib/vlq.js                Base64 VLQ 解码与 mappings 解析（自实现）
lib/sourcemap.js          basic/indexed 解析、选节、片段选择与覆盖判断
lib/resources.js          manifest、allowed_roots 身份组合、内容来源
lib/stack.js              Node 栈解析（右端锚定，兼容空格/括号路径）
lib/engine.js             逐帧逐跳溯源引擎（含循环检测）
lib/report-html.js        单文件 HTML 报告
lib/selftest.js           手写映射的独立核验
tools-crosscheck/python_vlq_oracle.py   与 Node 独立的 Python VLQ 预言机
```

## 独立核验

- `selftest` 不引用 fixtures 的“答案”，用编码器现场生成小型 mappings，断言：VLQ 向量与往返、一字段中断、空行不借用、indexed 节首行扣列/后续行不扣/词序选节、代理对 UTF-16 列、片段覆盖区间。
- `tools-crosscheck/python_vlq_oracle.py` 是独立编写的 Python 3.11 解码预言机；对四张真实 map 的全部片段，已与 Node 输出做逐字段 diff 确认一致。
- `verify` 重新 `node assets/basic.cjs`、`node assets/indexed.cjs`，按 `capture.json` 仅比对用户帧（Node 版本内部帧差异可接受），归一化完整栈落盘到 `reports/*.rerun.txt`。
