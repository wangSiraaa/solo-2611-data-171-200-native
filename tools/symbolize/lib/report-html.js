'use strict';

// 单文件 HTML 报告：无外部资源、无需浏览器构建，直接 file:// 打开。
// 展示：摘要、逐帧卡片（生成片段、每跳 map 摘要/节/片段表/覆盖判断、原源片段或缺失原因）。

function esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

const STATUS_LABEL = {
  located: '已定位（有源内容）',
  'located-no-content': '已定位（源内容缺失）',
  unmapped: '无映射区',
  'map-missing': 'map 缺失/损坏',
  'invalid-segment': '片段非法',
  'source-forbidden': 'source 身份越界',
  'node-internal': 'Node 内部帧',
  unknown: '未知帧',
  'map-not-declared': 'manifest 未声明 map',
  'chain-end': '链路结束',
  cycle: '链路循环',
  'hop-limit': '达到跳数上限',
};

// 在目标单元处生成独立的 marker 行；非 BMP 字符占两个单元，caret 定位到所属 code point 起点。
function markerRow(lineText, targetUnit) {
  const cps = [...lineText];
  let unit = 0;
  let caretCp = 0;
  for (let i = 0; i < cps.length; i += 1) {
    if (unit >= targetUnit) { caretCp = i; break; }
    unit += cps[i].length;
    caretCp = i + 1;
  }
  // 等宽字体下按 code point 数补空格（代理对是单一字形，占一个字形宽度）
  const pad = ' '.repeat(caretCp);
  return `<tr class="marker-row"><td class="ln"></td><td class="code marker">${esc(pad)}^ <span class="meta">UTF-16 列 ${targetUnit}（1 基 ${targetUnit + 1}）</span></td></tr>`;
}

function excerptBlock(title, ex, origin) {
  if (!ex) {
    return `<div class="excerpt missing"><h4>${esc(title)}</h4><p class="warn">无可用文本（不伪造摘录）</p></div>`;
  }
  const rows = ex.rows
    .map((r) => {
      const lineRow = `<tr class="${r.target === null ? '' : 'target-line'}"><td class="ln">${r.line1}</td><td class="code">${esc(r.text)}</td></tr>`;
      const marker = r.target === null ? '' : markerRow(r.text, r.target);
      return lineRow + marker;
    })
    .join('');
  return `<div class="excerpt">
    <h4>${esc(title)} <span class="meta">${origin ? `内容来源：${esc(origin)} · ` : ''}列按 UTF-16 单元，目标 ${ex.line1}:${ex.column1}（0 基 ${ex.line0}:${ex.column0}）</span></h4>
    <table>${rows}</table>
  </div>`;
}

function segmentTable(hop) {
  if (!hop.lineSegments || hop.lineSegments.length === 0) {
    return '<p class="meta">该生成行无任何片段。</p>';
  }
  const rows = hop.lineSegments
    .map((s) => {
      const cls = hop.selection && hop.selection.segmentIndex === s.index ? 'selected' : '';
      const map = s.fields === 1
        ? '<span class="tag unmapped-tag">一字段·无映射</span>'
        : `→ src#${s.sourceIndex} ${s.sourceLine0}:${s.sourceColumn0}`;
      return `<tr class="${cls}"><td>${s.index}</td><td>${s.genCol0}</td><td>${s.fields}</td><td>${map}</td></tr>`;
    })
    .join('');
  return `<table class="segments">
    <thead><tr><th>#</th><th>genCol(0基)</th><th>字段数</th><th>映射</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

function hopBlock(hop) {
  const cov = hop.coverage;
  const covHtml = cov
    ? `<div class="coverage ${cov.targetCovered ? 'ok' : 'warnbox'}">
        <strong>片段覆盖判断：</strong>选中片段覆盖区间 ${esc(cov.coveredRange)}；
        目标节内列 ${cov.targetColumn0}（0 基）${cov.targetCovered ? '落在区间内，覆盖成立' : '未被覆盖'}。
        ${cov.oneField ? '该片段为一字段无映射片段，显式中断此前映射。' : ''}
        ${cov.nextSegmentIsOneField ? '下一片段是一字段无映射片段，越过它即进入无映射区。' : ''}
      </div>`
    : '';
  const out = hop.output
    ? `<div class="hop-output"><strong>本跳输出：</strong>${esc(hop.output.sourceIdentity)}
       :${hop.output.line1}:${hop.output.column1}（0 基 ${hop.output.line0}:${hop.output.column0}）
       ${hop.output.name ? `· name=${esc(hop.output.name)}` : ''}
       <div class="meta">source 组合：map目录 + sourceRoot=${esc(hop.output.sourceRoot || '（空）')} + 条目=${esc(hop.output.sourceEntry)}</div>
       </div>`
    : `<div class="warnbox"><strong>本跳未产出位置：</strong>${esc(hop.reason || '')}</div>`;
  const sec = hop.section
    ? `<div class="section"><strong>indexed 节选择：</strong>section #${hop.section.index}，offset ${hop.section.offset.line0}:${hop.section.offset.column0}
       <div class="meta">${esc(hop.section.why)}</div>
       <div class="meta">坐标换算：${esc(hop.coordinate.rule)}；目标${hop.coordinate.targetIsFirstSectionLine ? '在' : '不在'}节首行 → 节内 ${hop.coordinate.localLine0}:${hop.coordinate.localColumn0}（0 基）</div></div>`
    : '';
  return `<details class="hop" ${hop.output ? '' : 'open'}>
    <summary>第 ${hop.hopNo} 跳 · ${esc(hop.mapIdentity)} · ${hop.loaded ? (hop.output ? '映射成功' : '未映射') : '加载失败'}</summary>
    <div class="meta map-summary">map 摘要：<pre>${esc(JSON.stringify(hop.summary, null, 2))}</pre></div>
    ${sec}
    <h5>同生成行片段与选择</h5>
    ${segmentTable(hop)}
    ${hop.selection
      ? `<p class="meta">选择规则：${esc(hop.selection.rule)}${hop.selection.reason ? `；${esc(hop.selection.reason)}` : ''}</p>`
      : '<p class="meta">map 未成功加载，无片段选择。</p>'}
    ${covHtml}
    ${out}
  </details>`;
}

function frameCard(frame) {
  const finalHtml = frame.finalLocation
    ? excerptBlock(
        `原源：${frame.finalLocation.identity}`,
        frame.finalLocation.excerpt,
        frame.finalLocation.contentOrigin
      )
    : '<div class="excerpt missing"><h4>原源片段</h4><p class="warn">未到达原源位置。</p></div>';
  return `<section class="frame status-${esc(frame.status)}">
    <header>
      <span class="frame-index">#${frame.index}</span>
      <span class="status ${esc(frame.status)}">${esc(STATUS_LABEL[frame.status] || frame.status)}</span>
      <code class="fn">${esc(frame.functionName || '(匿名/无函数名)')}</code>
      <code class="loc">${esc(frame.identity || frame.file)}:${frame.generated.line1}:${frame.generated.column1}</code>
    </header>
    ${frame.reason ? `<p class="reason">${esc(frame.reason)}</p>` : ''}
    ${excerptBlock(`生成代码：${frame.identity || frame.file}`, frame.generatedExcerpt, frame.generatedContentOrigin)}
    ${frame.hops.length ? `<h4 class="hops-title">逐跳证据（共 ${frame.hops.length} 跳）</h4>` : ''}
    <div class="hops">${frame.hops.map(hopBlock).join('')}</div>
    ${finalHtml}
  </section>`;
}

function render(report) {
  const counts = report.summary.statusCounts;
  const summaryItems = Object.entries(counts)
    .map(([k, v]) => `<li><span class="status ${esc(k)}">${esc(STATUS_LABEL[k] || k)}</span> × ${v}</li>`)
    .join('');
  return `<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>离线堆栈溯源报告 - ${esc(report.stackFile)}</title>
<style>
  body { font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; margin: 0; background: #0f1420; color: #d6deeb; }
  main { max-width: 1180px; margin: 0 auto; padding: 24px; }
  h1 { font-size: 20px; } h2 { font-size: 16px; } h4 { margin: 12px 0 6px; } h5 { margin: 10px 0 4px; color: #82aaff; }
  .meta { color: #7f8da3; font-size: 12px; }
  .top { background: #172033; border: 1px solid #233049; border-radius: 10px; padding: 16px; margin-bottom: 20px; }
  .top ul { margin: 8px 0 0; padding-left: 18px; }
  .frame { background: #151d2e; border: 1px solid #24314b; border-radius: 10px; padding: 14px 16px; margin-bottom: 16px; }
  .frame header { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  .frame-index { color: #7f8da3; }
  code.fn { color: #ffcb6b; } code.loc { color: #7fdbca; }
  .status { font-size: 12px; padding: 2px 8px; border-radius: 999px; border: 1px solid #2c3b58; }
  .status-located { background: #14362a; color: #69e09a; border-color: #1f5c43; }
  .status-located-no-content { background: #3a3318; color: #e3c468; border-color: #6b5d26; }
  .status-unmapped, .status-map-missing, .status-invalid-segment, .status-source-forbidden,
  .status-unknown, .status-map-not-declared, .status-cycle, .status-hop-limit { background: #3a1f25; color: #f0939b; border-color: #6e2c37; }
  .status-node-internal, .status-chain-end { background: #222a3c; color: #93a4c4; }
  .reason { background: #1d2740; border-left: 3px solid #ff9f6b; padding: 8px 10px; font-size: 13px; }
  .excerpt { margin: 10px 0; }
  .excerpt table { border-collapse: collapse; width: 100%; background: #0e1525; border-radius: 8px; overflow: hidden; }
  .excerpt td { padding: 2px 10px; white-space: pre; }
  td.ln { color: #55637d; text-align: right; user-select: none; width: 48px; }
  tr.target-line { background: #1d2b47; }
  tr.marker-row td.marker { color: #ff6b81; }
  .missing .warn, .warnbox, .warn { color: #f0939b; }
  .warnbox { border: 1px solid #6e2c37; background: #2a1720; padding: 8px 10px; border-radius: 8px; }
  .ok { color: #69e09a; }
  .coverage { padding: 8px 10px; border-radius: 8px; font-size: 13px; margin: 8px 0; background: #12251d; border: 1px solid #1f5c43; }
  .coverage.warnbox { background: #2a1720; border-color: #6e2c37; }
  .hop { background: #101828; border: 1px solid #233049; border-radius: 8px; padding: 8px 12px; margin: 8px 0; }
  .hop summary { cursor: pointer; color: #82aaff; }
  table.segments { border-collapse: collapse; margin: 6px 0; font-size: 12px; }
  table.segments td, table.segments th { border: 1px solid #2a3852; padding: 3px 8px; }
  table.segments tr.selected { background: #1d2b47; }
  .tag { padding: 1px 6px; border-radius: 6px; font-size: 11px; }
  .unmapped-tag { background: #3a1f25; color: #f0939b; }
  pre { white-space: pre-wrap; word-break: break-all; margin: 4px 0; }
  .hop-output { background: #12233a; border-radius: 8px; padding: 8px 10px; margin-top: 8px; }
</style>
</head>
<body>
<main>
  <div class="top">
    <h1>离线堆栈溯源报告</h1>
    <div class="meta">生成时间：${esc(report.generatedAt)} · 工具：${esc(report.tool)} · Node ${esc(report.node)}</div>
    <div class="meta">manifest：${esc(report.manifest)} · 栈文件：${esc(report.stackFile)} · 坐标：栈 1 基 → map 0 基，列单位 UTF-16 code unit</div>
    <h2>帧统计（共 ${report.summary.totalFrames} 帧，保持原顺序）</h2>
    <ul>${summaryItems}</ul>
  </div>
  ${report.frames.map(frameCard).join('')}
</main>
</body>
</html>`;
}

module.exports = { render };
