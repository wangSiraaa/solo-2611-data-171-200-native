"""SVG 渲染：直接使用 Layout 中的几何对象，保证画的就是 JSON 里的坐标。"""
from __future__ import annotations

from xml.sax.saxutils import escape

from .model import Layout, r
from .text import FONT_SIZE, LINE_HEIGHT

COMP_FILLS = ["#f7f9fc", "#fbf8f3", "#f6fbf8", "#faf6fb"]


def _px(v: float) -> str:
    return f"{r(v):.2f}"


def render_svg(layout: Layout) -> str:
    L = layout
    w, h = r(L.canvas.width), r(L.canvas.height)
    out: list[str] = []
    out.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_px(w)}" '
        f'height="{_px(h)}" viewBox="0 0 {_px(w)} {_px(h)}" '
        f'font-family="sans-serif">')
    out.append(
        '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="8" markerHeight="8" orient="auto-start-reverse">'
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="#444"/></marker></defs>')

    # -- 独立组件底色带 ----------------------------------------------------
    for c in L.components:
        fill = COMP_FILLS[c.component % len(COMP_FILLS)]
        b = c.box
        out.append(
            f'<g class="component" data-component="{c.component}">'
            f'<rect x="{_px(b.x)}" y="{_px(b.y)}" width="{_px(b.width)}" '
            f'height="{_px(b.height)}" rx="10" fill="{fill}" stroke="#d8dee8"/>'
            f'<text x="{_px(b.x + 8)}" y="{_px(b.y + 16)}" font-size="11" '
            f'fill="#8a94a6">组件 {c.component + 1}（{len(c.node_ids)} 节点）'
            '</text></g>')

    # -- SCC 簇框（循环可见，不删除回边） ----------------------------------
    for s in L.sccs:
        b = s.box
        out.append(
            f'<g class="scc" data-scc="{s.scc_id}">'
            f'<rect x="{_px(b.x)}" y="{_px(b.y)}" width="{_px(b.width)}" '
            f'height="{_px(b.height)}" rx="12" fill="none" stroke="#c47ed4" '
            'stroke-width="1.5" stroke-dasharray="7 5"/>'
            f'<text x="{_px(b.x + 8)}" y="{_px(b.y + 14)}" font-size="11" '
            f'fill="#9b4fb0">SCC {s.scc_id}：循环（{len(s.node_ids)} 节点）'
            f'</text></g>')

    # -- 边（先画边，节点盒盖住折线端） ------------------------------------
    out.append('<g class="edges">')
    for eid, e in sorted(L.edges.items()):
        pts = " ".join(f"{_px(x)},{_px(y)}" for x, y in e.points)
        cls = "edge " + e.kind + (" parallel" if e.is_parallel else "") + \
              (" self-loop" if e.is_self_loop else "") + \
              (" new" if e.is_new else "")
        out.append(
            f'<g class="{cls}" data-edge-id="{escape(eid)}">'
            f'<title>{escape(eid)}: {escape(e.source)} → {escape(e.target)}'
            f'{(" [" + escape(e.label) + "]") if e.label else ""}</title>'
            f'<polyline points="{pts}" fill="none" '
            f'stroke="{"#c0392b" if e.is_new else "#444"}" '
            f'stroke-width="1.4" marker-end="url(#arrow)"/></g>')
    out.append('</g>')

    # -- 边标签（白底 halo，交叉点绝不画成圆点/节点） ----------------------
    out.append('<g class="edge-labels">')
    for eid, e in sorted(L.edges.items()):
        if e.label_box is None:
            continue
        b = e.label_box.box
        out.append(
            f'<g class="edge-label{" new" if e.is_new else ""}" '
            f'data-edge-id="{escape(eid)}">'
            f'<rect x="{_px(b.x)}" y="{_px(b.y)}" width="{_px(b.width)}" '
            f'height="{_px(b.height)}" rx="3" fill="#ffffff" '
            f'stroke="{"#c0392b" if e.is_new else "#c9ced8"}" stroke-width="0.8"/>'
            f'<text x="{_px(b.cx)}" y="{_px(b.cy + 4)}" font-size="11" '
            f'text-anchor="middle" fill="#333">{escape(e.label_box.text)}</text></g>')
    out.append('</g>')

    # -- 节点 --------------------------------------------------------------
    out.append('<g class="nodes">')
    for nid, n in sorted(L.nodes.items()):
        b = n.box
        stroke = "#c0392b" if n.is_new else "#2f6db5"
        cls = "node" + (" enlarged" if n.box_enlarged else "") + \
              (" new" if n.is_new else "") + (" cyclic" if n.in_cycle else "")
        out.append(
            f'<g class="{cls}" data-node-id="{escape(nid)}">'
            f'<title>{escape(nid)}：{escape(n.label)}</title>'
            f'<rect x="{_px(b.x)}" y="{_px(b.y)}" width="{_px(b.width)}" '
            f'height="{_px(b.height)}" rx="7" fill="#ffffff" stroke="{stroke}" '
            f'stroke-width="1.6"/>')
        total_h = len(n.lines) * LINE_HEIGHT
        first_baseline = b.cy - total_h / 2 + FONT_SIZE - 2
        for i, line in enumerate(n.lines):
            out.append(
                f'<text x="{_px(b.cx)}" y="{_px(first_baseline + i * LINE_HEIGHT)}" '
                f'font-size="{FONT_SIZE:.0f}" text-anchor="middle" fill="#1f2d3d">'
                f'{escape(line)}</text>')
        # 稳定身份角标
        out.append(
            f'<text x="{_px(b.x + 5)}" y="{_px(b.y2 - 4)}" font-size="9" '
            f'fill="#9aa5b1">{escape(nid)}</text>')
        out.append('</g>')
    out.append('</g>')
    out.append('</svg>')
    return "\n".join(out)
