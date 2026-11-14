"""v1/v2 静态对照 HTML：内联两份实际 SVG，可直接双击打开，无网络依赖。"""
from __future__ import annotations

import json
from xml.sax.saxutils import escape


def _issue_rows(issues: list[dict]) -> str:
    if not issues:
        return "<tr><td colspan=3>无</td></tr>"
    rows = []
    for i in issues:
        rows.append(
            f"<tr class='sev-{i['severity']}'><td>{i['severity']}</td>"
            f"<td>{escape(i['code'])}</td><td>{escape(i['message'])}</td></tr>")
    return "\n".join(rows)


def build_html(v1_json_path: str, v1_svg_path: str,
               v2_json_path: str, v2_svg_path: str) -> str:
    v1 = json.load(open(v1_json_path, encoding="utf-8"))
    v2 = json.load(open(v2_json_path, encoding="utf-8"))
    svg1 = open(v1_svg_path, encoding="utf-8").read()
    svg2 = open(v2_svg_path, encoding="utf-8").read()

    upd = v2.get("update") or {}
    moved = upd.get("moved_nodes", [])
    if moved:
        move_rows = "".join(
            f"<tr><td>{escape(d['node_id'])}</td>"
            f"<td>({d['from']['x']}, {d['from']['y']})</td>"
            f"<td>({d['to']['x']}, {d['to']['y']})</td>"
            f"<td>({d['dx']}, {d['dy']})</td>"
            f"<td>{escape(d['reason'])}</td></tr>" for d in moved)
    else:
        move_rows = "<tr><td colspan=5>无旧节点移动：全部旧坐标冻结</td></tr>"
    causes = "<br>".join(escape(c) for c in upd.get("relocation_causes", [])) or "无"

    legend = """
    <div class="legend">
      <span><i class="box normal"></i>普通节点</span>
      <span><i class="box new"></i>新增节点/边</span>
      <span><i class="dash"></i>SCC 循环簇（回边保留并在簇外绕行）</span>
      <span><i class="band"></i>独立组件边界</span>
      <span><i class="arrow"></i>箭头端点落在节点盒边界</span>
    </div>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>依赖图布局 v1 / v2 对照</title>
<style>
  body {{ font-family: sans-serif; margin: 24px; color: #1f2d3d; background: #f5f7fa; }}
  h1 {{ font-size: 20px; }} h2 {{ font-size: 16px; margin-top: 28px; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
  .card {{ background: #fff; border: 1px solid #d8dee8; border-radius: 10px;
           padding: 14px; overflow: auto; max-height: 78vh; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 13px; background:#fff; }}
  th, td {{ border: 1px solid #d8dee8; padding: 6px 8px; text-align: left; }}
  th {{ background: #eef2f7; }}
  .sev-error {{ color:#b3261e; }} .sev-warning {{ color:#9a6700; }}
  .sev-info {{ color:#5a6b7b; }}
  .legend {{ display:flex; gap:18px; flex-wrap:wrap; font-size:13px; margin:12px 0; }}
  .legend i {{ display:inline-block; vertical-align:middle; margin-right:5px; }}
  .legend .box {{ width:16px; height:12px; border:2px solid #2f6db5; border-radius:3px; background:#fff; }}
  .legend .box.new {{ border-color:#c0392b; }}
  .legend .dash {{ width:18px; height:12px; border:2px dashed #c47ed4; }}
  .legend .band {{ width:18px; height:12px; background:#f7f9fc; border:1px solid #d8dee8; }}
  .legend .arrow {{ width:18px; height:2px; background:#444; }}
  .meta {{ font-size:12px; color:#667; }}
  svg {{ max-width: 100%; height: auto; }}
</style>
</head>
<body>
<h1>依赖关系图静态布局对照（同一份坐标 JSON 渲染）</h1>
<p class="meta">v1：{len(v1['nodes'])} 节点 / {len(v1['edges'])} 边；
v2：{len(v2['nodes'])} 节点 / {len(v2['edges'])} 边；
新增节点 {escape(', '.join(upd.get('added_nodes', [])))}，
新增边 {escape(', '.join(upd.get('added_edges', [])))}。</p>
{legend}
<div class="grid">
  <div class="card"><h2>v1 初始布局</h2>{svg1}</div>
  <div class="card"><h2>v2 增量布局（红色为新增）</h2>{svg2}</div>
</div>

<h2>增量稳定性报告</h2>
<table>
<tr><th>节点</th><th>旧坐标</th><th>新坐标</th><th>位移</th><th>原因</th></tr>
{move_rows}
</table>
<p><b>迁移原因：</b>{causes}<br>
<b>旧边复用：</b>{escape(', '.join(upd.get('old_edges_reused', [])))}（折线逐点保留）</p>

<h2>v1 布局问题清单（含边交叉定位，交叉允许且不渲染为节点）</h2>
<table><tr><th>级别</th><th>代码</th><th>信息</th></tr>{_issue_rows(v1['issues'])}</table>
<h2>v2 布局问题清单</h2>
<table><tr><th>级别</th><th>代码</th><th>信息</th></tr>{_issue_rows(v2['issues'])}</table>
</body></html>"""
