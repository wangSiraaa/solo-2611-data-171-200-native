"""文字换行与几何工具。

字符宽度采用 display-plan 声明的确定性估算（ASCII=7，非 ASCII=14），
不依赖浏览器字体度量。几何函数同时供布局与独立 verify 使用。
"""
from __future__ import annotations

from .model import Box, r

CHAR_W_ASCII = 7.0
CHAR_W_NONASCII = 14.0
LINE_HEIGHT = 17.0          # 节点标签行高（SVG px）
FONT_SIZE = 13.0


def char_width(ch: str) -> float:
    return CHAR_W_ASCII if ord(ch) < 128 else CHAR_W_NONASCII


def text_width(text: str) -> float:
    return sum(char_width(c) for c in text)


def edge_label_width(text: str) -> float:
    """display-plan: max(36, 7*ASCII + 14*nonASCII)。"""
    return max(36.0, text_width(text))


def _wrap_long_token(token: str, max_w: float) -> list[str]:
    """单个词超长时按字符硬切。"""
    lines: list[str] = []
    cur = ""
    for ch in token:
        if text_width(cur + ch) <= max_w or not cur:
            cur += ch
        else:
            lines.append(cur)
            cur = ch
    if cur:
        lines.append(cur)
    return lines


def wrap_label(label: str, box_width: float, padding: float) -> list[str]:
    """确定性换行：先按空格分词，超长词按字符切。

    对没有空格的连续 CJK 词，不再只做贪心填充，而是在可行断点中选择
    行宽最均衡的方案，避免出现“历史归/属”这类一字孤儿行。
    """
    max_w = box_width - 2 * padding
    if max_w <= 0:
        return [label]

    def split_token(token: str) -> list[str]:
        if text_width(token) <= max_w:
            return [token]
        if " " in token:
            out: list[str] = []
            for sub in token.split(" "):
                out.extend(split_token(sub))
            return out
        # 收集所有能放下的断点（前缀宽度 <= max_w），选最均衡的两断点
        n = len(token)
        best: tuple[float, list[str]] | None = None
        for k in range(1, n):
            a, b = token[:k], token[k:]
            if text_width(a) <= max_w and text_width(b) <= max_w:
                balance = abs(text_width(a) - text_width(b))
                if best is None or balance < best[0]:
                    best = (balance, [a, b])
        if best is not None:
            return best[1]
        return _wrap_long_token(token, max_w)

    lines: list[str] = []
    for raw_token in label.split(" "):
        for piece in split_token(raw_token):
            if lines and text_width(lines[-1] + " " + piece) <= max_w:
                lines[-1] = lines[-1] + " " + piece
            elif lines and text_width(lines[-1] + piece) <= max_w:
                lines[-1] = lines[-1] + piece          # CJK 直接拼接
            else:
                lines.append(piece)
    return lines or [label]


def label_block_height(lines: list[str]) -> float:
    return len(lines) * LINE_HEIGHT


def needed_box_size(label: str, given_w: float, given_h: float,
                    padding: float, v_padding: float = 4.0
                    ) -> tuple[float, float, list[str], bool]:
    """返回最终盒尺寸、换行结果，以及是否放大了给定盒（只能放大不能缩小）。

    水平留白采用 display-plan 的 node_padding；垂直留白固定 4px——给定盒高
    已含业务定义的标签内边距，这里只补偿行高估算误差，避免无意义的整体放大。
    """
    lines = wrap_label(label, given_w, padding)
    text_w = max((text_width(ln) for ln in lines), default=0.0)
    text_h = label_block_height(lines)
    w = max(given_w, text_w + 2 * padding)
    h = max(given_h, text_h + 2 * v_padding)
    enlarged = (w > given_w + 0.01) or (h > given_h + 0.01)
    return w, h, lines, enlarged


# ---------------------------------------------------------------------------
# 几何
# ---------------------------------------------------------------------------
def point_in_box(px: float, py: float, b: Box, tol: float = 0.0) -> bool:
    return (b.x - tol <= px <= b.x2 + tol and
            b.y - tol <= py <= b.y2 + tol)


def boxes_overlap(a: Box, b: Box, tol: float = 0.0) -> bool:
    """严格重叠（共边/共点不算）。"""
    return (a.x < b.x2 - tol and b.x < a.x2 - tol and
            a.y < b.y2 - tol and b.y < a.y2 - tol)


def boxes_touch(a: Box, b: Box) -> bool:
    """共边或共点但不重叠。"""
    x_touch = (abs(a.x - b.x2) < 0.5 or abs(a.x2 - b.x) < 0.5)
    y_overlap = (a.y < b.y2 - 0.5 and b.y < a.y2 - 0.5)
    y_touch = (abs(a.y - b.y2) < 0.5 or abs(a.y2 - b.y) < 0.5)
    x_overlap = (a.x < b.x2 - 0.5 and b.x < a.x2 - 0.5)
    return (x_touch and y_overlap) or (y_touch and x_overlap)


def segments_intersect(p1: tuple[float, float], p2: tuple[float, float],
                       p3: tuple[float, float], p4: tuple[float, float]
                       ) -> tuple[bool, tuple[float, float] | None]:
    """线段相交，返回 (是否相交, 交点)。共线重叠也视为相交。"""
    x1, y1 = p1
    x2, y2 = p2
    x3, y3 = p3
    x4, y4 = p4
    d = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(d) > 1e-9:
        t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / d
        u = -((x1 - x2) * (y1 - y3) - (y1 - y2) * (x1 - x3)) / d
        if 0 - 1e-9 <= t <= 1 + 1e-9 and 0 - 1e-9 <= u <= 1 + 1e-9:
            return True, (r(x1 + t * (x2 - x1)), r(y1 + t * (y2 - y1)))
        return False, None
    # 平行：检查共线且投影重叠
    if abs((x2 - x1) * (y3 - y1) - (y2 - y1) * (x3 - x1)) > 1e-9:
        return False, None

    def on_segment(px: float, py: float,
                   ax: float, ay: float, bx: float, by: float) -> bool:
        return (min(ax, bx) - 1e-9 <= px <= max(ax, bx) + 1e-9 and
                min(ay, by) - 1e-9 <= py <= max(ay, by) + 1e-9)

    overlap_pts = [p for p in (p3, p4) if on_segment(p[0], p[1], x1, y1, x2, y2)]
    if overlap_pts:
        return True, overlap_pts[0]
    overlap_pts = [p for p in (p1, p2) if on_segment(p[0], p[1], x3, y3, x4, y4)]
    if overlap_pts:
        return True, overlap_pts[0]
    return False, None


def segment_intersects_box(p1: tuple[float, float], p2: tuple[float, float],
                           b: Box) -> list[tuple[float, float]]:
    """线段与矩形内部/边界的全部交点（端点在边界上也算，由调用方区分）。"""
    corners = [(b.x, b.y), (b.x2, b.y), (b.x2, b.y2), (b.x, b.y2)]
    hits: list[tuple[float, float]] = []
    for i in range(4):
        ok, pt = segments_intersect(p1, p2, corners[i], corners[(i + 1) % 4])
        if ok and pt is not None:
            if not any(abs(pt[0] - q[0]) < 0.5 and abs(pt[1] - q[1]) < 0.5
                       for q in hits):
                hits.append(pt)
    # 线段完全落在矩形内
    if point_in_box(*p1, b) and point_in_box(*p2, b) and not hits:
        hits.append(p1)
    return hits


def dist_point_segment(px: float, py: float,
                       p1: tuple[float, float], p2: tuple[float, float]) -> float:
    x1, y1 = p1
    x2, y2 = p2
    dx, dy = x2 - x1, y2 - y1
    if dx == dy == 0:
        return ((px - x1) ** 2 + (py - y1) ** 2) ** 0.5
    t = ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    qx, qy = x1 + t * dx, y1 + t * dy
    return ((px - qx) ** 2 + (py - qy) ** 2) ** 0.5


def box_center(b: Box) -> tuple[float, float]:
    return b.cx, b.cy
