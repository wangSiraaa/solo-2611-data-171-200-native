# 离线重建工具（siterebuild）

从真实 WARC 与 cutoff 计划重建指定时点可离线浏览的静态站点，产出捕获来源表与缺口报告。不访问公网，不执行页面 JavaScript。Python 3.11，依赖见 `requirements.txt`（warcio / beautifulsoup4 / tinycss2）。

## 安装

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 三个入口

### rebuild — 重建并导出

```sh
.venv/bin/python -m siterebuild rebuild --plan fixtures/plan.json --warc fixtures/site.warc --out out
```

对计划中的每个 cutoff 产出一个目录 `out/<cutoff>/`：

- `site/` — 可离线浏览的静态站点（入口 `site/index.html`），含 `_gaps/` 下的本地缺口说明页；
- `captures.json` — 捕获来源表：每个资源的规范 URL、本地路径、所选记录身份（Record-ID/类型/WARC-Date/当前响应头）、payload 来源（self / refers-to / digest-match 及支持记录）、重定向链、原始与重写摘要（sha1 base32，分开记录）；
- `gaps.json` — 缺口报告：原因（`missing-revisit-reference` / `revisit-reference-cycle` / `revisit-digest-mismatch` / `revisit-no-digest-source` / `no-capture-before-cutoff` / `redirect-cycle` / `missing-fragment-target` / `record-digest-mismatch`）、链与逐条引用来源；
- `references.json` — 每个 HTML/CSS 的逐条引用及处置（rewritten / external / gap / skipped / self-fragment），含 base 的处理记录；
- `summary.json` — 计数摘要。

### serve — 本地 HTTP 服务

```sh
.venv/bin/python -m siterebuild serve --site out/2026-01-02T12-00-00Z/site --port 8000
```

绑定 127.0.0.1，端口可选（0 表示自动分配），仅静态服务，不访问远程。

### verify — 无浏览器核验

```sh
.venv/bin/python -m siterebuild verify --plan fixtures/plan.json --warc fixtures/site.warc --out out --inventory fixtures/capture-inventory.json
```

通过本地 HTTP 请求与解析导出文件核验：引用闭合（HTML 的 a/link/img、CSS 的 url()/@import）、无指向原站的活引用、导出摘要与原始摘要、共享字节（revisit 复用内容与支持记录一致）、fragment 目标存在或已记录为缺口、缺口页可达且原因不塌缩；跨 cutoff 核对路径稳定性与共享捕获字节一致。`--no-http` 可跳过 HTTP 爬取。结果写入 `out/verify-report.json`，失败时退出码为 1。

## 处理要点

- **捕获选择**：取 WARC-Date ≤ cutoff 的最新记录；同时间取 Record-ID 字典序最小者。选择失败（如 revisit 引用丢失）不会退回更旧的成功内容。
- **revisit**：显式 `WARC-Refers-To` 为权威且必须可解析（不做 digest 兜底）；无该头时按 `WARC-Payload-Digest` 在 cutoff 前的 response payload 中找同内容来源（可多支持记录）。复用前以 sha1 base32 核对实际字节；只复用 payload，响应头身份保留当前捕获。
- **URL 身份**：小写 scheme/host、去默认端口与 fragment、解析点段；保留百分号编码（含 %2F）与 query 原顺序。fragment 作为跳转位置保留，重定向 Location 无 fragment 时保留来路 fragment。
- **遍历**：从计划入口出发，仅处理 HTML（a[href]、link[href]、img[src]、第一个有效 base[href]）与 CSS（url()、@import）的引用；CSS 相对地址基于 CSS 自身 URL，不沿用父 HTML 的 base。
- **导出路径**：按规范 URL 稳定分配，query 编入文件名（`report?view=a` → `site/report~view=a.html`），不同 query 不会互相覆盖；冲突时追加内容哈希。
- **缺口**：丢失引用、引用环、摘要不符、无可用捕获、跳转环、缺 fragment 分别保留原因；引用缺口重写为本地说明页（带 `data-gap-reason` 与原始引用），绝不静默留空或访问原站。
