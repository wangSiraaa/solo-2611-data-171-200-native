# Word 模板展开器（题 174）

用 Python 3.11 + `python-docx` / `lxml` 对真实 DOCX 做模板展开：读取
`fixtures/template.docx`、`fixtures/records.json`，为每条记录生成独立
`.docx` 副本和一份字段/明细来源清单（manifest）。核心渲染直接操作 OOXML
节点树，使用 `python-docx` 仅作包读写，不调用任何现成模板 render 引擎。

## 快速开始

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 生成到 out/，并写出 out/manifest.json
.venv/bin/python -m template_expand \
    fixtures/template.docx fixtures/records.json out \
    --contract fixtures/template-contract.json

# 独立复检（ZIP/XML + 普通文档读取器 + 源文件摘要）
.venv/bin/python tools/verify_outputs.py \
    fixtures/template.docx fixtures/records.json out
```

CLI 退出码：`0` 正常（个别记录失败只写入 manifest）；`1` 配合
`--strict` 时表示存在记录级失败；`2` 表示模板级错误（整批无法生成）。

测试：`.venv/bin/pip install -r requirements-dev.txt && .venv/bin/python -m pytest tests -q`

## 支持的模板语法（支持边界）

| 能力 | 支持 | 说明 |
| --- | --- | --- |
| 普通段落 / 普通表格单元中的标量 `{{field}}` | ✅ | 逻辑文本跨 `w:r` 识别，映射回原节点替换 |
| 一层 `{{#items}}` … `{{/items}}` 行块 | ✅ | 两行标记须为同一表格的整行，行间样板行按每个明细复制 |
| 明细字段 `{{item.name}}` 等 | ✅ | 也接受 `{{items.name}}`；前缀由样板行决定 |
| 换行 | ✅ | 值中的 `\n` 写成 Word `<w:br/>`，保留在占位符起始 run 中 |
| 空明细 `items: []` | ✅ | 删除标记行与样板行，保留固定表尾 |
| 书签克隆 | ✅ | 每个副本重新分配唯一 `w:id` 与唯一 `name`，开始/结束仍成对 |
| 图片 docPr 克隆 | ✅ | 每个副本获得唯一 `wp:docPr/@id`；图片关系 `r:embed` 可共享 |
| 未命中占位符 / 格式 / 静态文本 / 超链接 | ✅ | 原 run、`rPr`、书签、图片、超链接保持不变 |

**明确不支持**（遇到即带位置报错，不做半成品输出）：

- 页眉/页脚、脚注、批注、文本框、内容控件（`w:sdt`）、嵌套表格中的占位符；
- 嵌套循环、任意表达式（`{{a + b}}`、函数、过滤器）；
- 跨段落、跨超链接、跨 drawing/内容控件边界的占位符；
- 修订痕迹（`w:del` / `w:ins`）、域代码（`instrText`）、图片/页码重算；
- 除 `items` 之外的循环名，及缺省上下文中的 `item.*` 字段。

一段逻辑文本中的占位符若被 Word 拆成多个格式 run，会先拼成逻辑串定位，再
映射回原 `w:t` 节点就地改写：替换文字取**占位符起始 run** 的字符格式，
其余 run 原样保留，不会把整段重建成纯文本。

## 失败语义与记录隔离

每条记录独立渲染；某条失败只影响它自己，其它记录照常产出：

- 不会留下未替换的 `{{…}}`（输出前统一断言，残留即报错）；
- 失败记录**不写** DOCX 文件，只在 manifest 中记录；
- 每条错误携带 `code`、`message`、结构位置 `location`（
  `word/document.xml tbl#0 row#1 cell#0 p#0 off#12` 之类）、`record_id`、
  `field`。

错误码：

| code | 含义 |
| --- | --- |
| `unsupported_structure` | 结构/表达式不支持（跨边界、未知结构、非整行标记等） |
| `unpaired_marker` | `{{#items}}` / `{{/items}}` 缺失、顺序错误、非整行 |
| `unknown_field` | 提供 contract 时，占位符不在声明的标量/明细字段中 |
| `missing_value` | 字段合法但记录缺值（或值非标量） |
| `missing_loop_value` | 记录缺少 `items` 列表或元素不是对象 |

## manifest 结构

```jsonc
{
  "template": "fixtures/template.docx",
  "template_error": null,            // 模板级结构错误时给出，此时所有记录失败
  "records": [
    {
      "record_id": "packet-A",
      "ok": true,
      "output": "out/0001_packet-A.docx",
      "loop_rows": 2,                // 生成的明细行数（样板行×明细数）
      "fields": [
        {"field": "item.name", "kind": "loop_field",
         "location": "word/document.xml tbl#0 row#0",
         "source": "items[0].name"},
        {"field": "customer", "kind": "scalar",
         "location": "word/document.xml body#1 p#1", "source": "customer"}
      ],
      "checks": {"residual_placeholders": 0},
      "errors": []
    }
  ]
}
```

## 设计要点

- `template_expand/ooxml.py`：`ParagraphIndex` 建立逻辑文本与每个字符到
  `w:t`/`w:tab`/`w:br` 节点的映射；`apply_replacements` 按节点分组、一次性
  改写，避免重复改写与顺序错乱；被占位符整体覆盖的非文本节点（如段内换行）
  会被移除。
- `template_expand/engine.py`：解析并校验模板结构（标记配对、整行约束、
  边界、样板行书签成对），再逐记录渲染；行块复制使用原表格子节点索引插入，
  保证 `tblPr`/`tblGrid` 仍在最前。
- `template_expand/identity.py`：深拷贝行内节点后重分配书签 id/name 与
  `wp:docPr@id`，并校验克隆前后书签配对签名不变。
- `template_expand/validate.py`：内存树与落盘包两级校验，独立读取
  `document.xml` 与 `document.xml.rels`。
- `tools/verify_outputs.py`：不依赖渲染内部状态，重新打开 ZIP/XML 与普通
  文档读取器复核，并确认源模板字节摘要不变。

## 文件布局

```
template_expand/      实现（engine / ooxml / identity / validate / api / errors）
tests/                pytest 用例（跨 run、两行明细、空明细、缺字段、书签、图片、负路径）
tools/verify_outputs.py  独立复核脚本
fixtures/             源模板、记录、契约（只读输入）
```

输入 fixture 全程只读；生成文件与 manifest 写入输出目录。
