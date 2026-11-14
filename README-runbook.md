# pdfpack — 多源 PDF 选页组装工具

按 `selection.json` 把两个（或多个）源 PDF 的选定页面交错组装成新资料包，
并把原书签、链接的跳转目标按**来源页身份**（而非旧数字页号）迁移到输出页。
仅处理普通内部 GoTo 跳转与已有命名目标；边界见 [SCOPE.md](SCOPE.md)。

## 环境

- Python 3.11
- 依赖：`pypdf>=5,<7`（见 `requirements.txt`）

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 用法

### 组装

```sh
.venv/bin/python assemble_pdf.py \
  --selection fixtures/selection.json \
  --output build/combined.pdf \
  --report build/report.json
```

- 资格检查失败时退出码为 1，不写 combined PDF，报告 `status` 为 `refused`，
  并在 `fatal_diagnostics` 中给出全部原因（未知页、重复选择、加密、JS、
  GoToR、表单等）。
- 成功时退出码为 0，产出真实 PDF 与映射/诊断 JSON。

### 独立验证

验证器从磁盘重新打开产物，自行解析每个直接/命名目标，不复用组装器内部状态：

```sh
.venv/bin/python verify_pdf.py \
  --combined build/combined.pdf \
  --report build/report.json \
  --fixtures-dir fixtures \
  --out build/verification.json
```

全部检查通过退出码为 0，并打印检查条数；任一失败退出码为 1。

### 测试

```sh
.venv/bin/python -m unittest discover -s tests -v
```

## selection.json 约定

```json
{
  "sources": {"A": "source-A.pdf", "B": "source-B.pdf"},
  "pages": [
    {"source": "A", "page": 1},
    {"source": "B", "page": 2},
    {"source": "A", "page": 3},
    {"source": "B", "page": 1}
  ]
}
```

- 页号为 1 基；每个 `(source, page)` 至多出现一次。
- `sources` 中的相对路径相对 selection 文件所在目录解析。

## 报告（report.json）内容

| 字段 | 含义 |
| --- | --- |
| `page_mapping` | 每个输出页对应的来源、来源 1 基页号、来源页对象号、页面几何 |
| `named_destinations` | 每个原命名目标：发布为 `<source>:<name>` 还是 `target_omitted`，及输出页 |
| `links` | 每条链接注释：原目标、矩形、`retargeted`/`target_omitted`、新目标 |
| `outline` | 每个书签节点：保留（含输出页）/遗漏（含后代重挂数量） |
| `removed_actions` | 被移除的悬空可跳转动作及原目标、矩形 |
| `diagnostics` | 各类计数汇总 |

## 代码结构

```
pdfpack/
  errors.py        诊断代码与 AssemblyError
  destinations.py  单源内显式/命名/字典目标的身份级解析
  model.py         预检查：加密/表单/JS/GoToR、链接与书签建模
  assemble.py      资格检查 -> 身份映射 -> 克隆净化 -> 重写跳转/名称树/大纲
  verify.py        独立复读产物的全量校验
assemble_pdf.py    组装 CLI
verify_pdf.py      验证 CLI
tests/             unittest 测试与合成问题 PDF 的辅助器
```
