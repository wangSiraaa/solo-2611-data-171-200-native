"""curses 终端界面：20×8 小表的粘贴预览编辑器。

界面分三区：
1. 顶部状态栏：逻辑坐标、row_id、列 id、当前值及其 JSON 类型；
2. 中部固定 20×8 网格（不做虚拟滚动），预演中的待改格/错误格高亮；
3. 底部预演面板：粘贴锚点、矩形范围、合法性、每条错误的逻辑源行列/目标坐标/原因。

终端过小或缩放时只显示尺寸提示，所有模型状态与光标位置原样保留，绝不索引越界。
"""

from __future__ import annotations

import curses
import fcntl
import os
import select
import signal
import struct
import sys
import termios

from .input import Event, InputParser
from .model import COLS, ROWS, GridModel, Preview

# 布局尺寸
ROWID_W = 6
COL_W = 12
TABLE_W = ROWID_W + 1 + COL_W * COLS  # 6+1+96 = 103
MIN_H = 26
MIN_W = 110

PASTE_ON = b"\x1b[?2004h"
PASTE_OFF = b"\x1b[?2004l"

TYPE_NAMES = {
    str: "text",
    int: "integer",
    bool: "boolean",
    type(None): "null",
}


class TuiApp:
    def __init__(self, model: GridModel, export_path: str | None = None) -> None:
        self.model = model
        self.export_path = export_path
        self.cur_r = 0
        self.cur_c = 0
        self.preview: Preview | None = None
        self.parser = InputParser()
        self._message = "就绪：方向键移动当前格，在终端中直接粘贴（需 bracketed paste 支持）"
        self._message_error = False
        self._resized = False
        self._last_dim: tuple[int, int] | None = None
        self._running = True

    # ---------- 生命周期 ----------

    def run(self) -> None:
        curses.wrapper(self._main)

    def _main(self, stdscr) -> None:
        self._stdscr = stdscr
        curses.curs_set(0)  # 隐藏物理光标，用反色表示当前格
        stdscr.keypad(True)
        self._init_colors()
        signal.signal(signal.SIGWINCH, self._on_winch)

        fd = sys.stdin.fileno()
        os.write(sys.stdout.fileno(), PASTE_ON)
        try:
            while self._running:
                # 双保险：即使 SIGWINCH 被库拦截或时序错过，每帧也直接向内核查询真实
                # 终端尺寸（ncurses 的 update_lines_cols 在部分配置下不刷新窗口缓存），
                # 尺寸一变就同步 curses 内部行列并整屏重绘，杜绝索引错位
                real_h, real_w = self._terminal_size(stdscr)
                if self._resized or (real_h, real_w) != self._last_dim:
                    self._resized = False
                    self._last_dim = (real_h, real_w)
                    try:
                        curses.resizeterm(real_h, real_w)
                    except curses.error:
                        pass
                    stdscr.clear()
                self._draw()
                stdscr.refresh()

                r, _, _ = select.select([fd], [], [], 0.1)
                if not r:
                    for ev in self.parser.idle():
                        self._handle(ev)
                    continue
                try:
                    chunk = os.read(fd, 1 << 16)
                except OSError:
                    break
                if not chunk:  # EOF
                    break
                for ev in self.parser.feed(chunk):
                    self._handle(ev)
        finally:
            os.write(sys.stdout.fileno(), PASTE_OFF)

    def _on_winch(self, signum, frame) -> None:
        self._resized = True

    @staticmethod
    def _terminal_size(stdscr) -> tuple[int, int]:
        """直接向内核查询 tty 真实行列；查询失败时回退到 curses 缓存。"""
        try:
            packed = fcntl.ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, b"\x00" * 8)
            rows, cols, _, _ = struct.unpack("HHHH", packed)
            if rows > 0 and cols > 0:
                return rows, cols
        except OSError:
            pass
        return stdscr.getmaxyx()

    # ---------- 颜色 ----------

    def _init_colors(self) -> None:
        self.attr_current = curses.A_REVERSE
        self.attr_change = curses.A_BOLD
        self.attr_error = curses.A_BOLD
        self.attr_header = curses.A_BOLD | curses.A_UNDERLINE
        self.attr_ok = curses.A_BOLD
        self.attr_bad = curses.A_BOLD
        if curses.has_colors():
            try:
                curses.start_color()
                curses.use_default_colors()
                curses.init_pair(1, curses.COLOR_GREEN, -1)
                curses.init_pair(2, curses.COLOR_RED, -1)
                curses.init_pair(3, curses.COLOR_CYAN, -1)
                curses.init_pair(4, curses.COLOR_YELLOW, -1)
                self.attr_change = curses.color_pair(1) | curses.A_BOLD
                self.attr_error = curses.color_pair(2) | curses.A_BOLD
                self.attr_header = curses.color_pair(3) | curses.A_BOLD
                self.attr_ok = curses.color_pair(1) | curses.A_BOLD
                self.attr_bad = curses.color_pair(2) | curses.A_BOLD
            except curses.error:
                pass

    # ---------- 事件处理 ----------

    def _handle(self, ev: Event) -> None:
        if ev.kind == "paste_error":
            self._set_message(f"粘贴被拒绝：{ev.reason}", error=True)
            return
        if ev.kind == "paste":
            self._start_preview(ev.text)
            return

        name = ev.name
        if name == "ctrl_c":
            self._running = False
            return

        if self.preview is not None:
            self._handle_preview_keys(name, ev)
        else:
            self._handle_normal_keys(name, ev)

    def _handle_normal_keys(self, name: str, ev: Event) -> None:
        if name in ("up", "down", "left", "right", "home", "end"):
            self._move(name)
        elif name == "ctrl_z":
            if self.model.undo():
                self._set_message("已撤销最近一次粘贴，相关单元格原值与类型已整块恢复")
            else:
                self._set_message("没有可撤销的粘贴记录", error=True)
        elif name == "f2":
            self._do_export()
        elif name == "char" and ev.text == "q":
            self._running = False
        elif name == "enter":
            self._set_message("当前没有待提交的粘贴预演；请先粘贴内容")
        elif name == "char":
            self._set_message("本程序不做格内键入编辑：请移动到锚点后直接粘贴 TSV 内容")
        else:
            # esc/tab 等在普通模式下无动作
            pass

    def _handle_preview_keys(self, name: str, ev: Event) -> None:
        if name in ("up", "down", "left", "right", "home", "end"):
            self._move(name)  # 预演中仍可移动查看，预演本身不改变
        elif name == "enter":
            assert self.preview is not None
            if self.preview.ok:
                n = self.model.commit(self.preview)
                self._set_message(f"已整批提交 {n} 个单元格；Ctrl+Z 可整块撤销")
                self.preview = None
            else:
                self._set_message("预演存在错误或越界，Enter 提交被拒绝；Esc 可取消", error=True)
        elif name == "esc":
            self.preview = None
            self._set_message("已取消粘贴预演，网格未做任何修改")
        elif name == "f2":
            self._do_export()
        elif name == "ctrl_z":
            self._set_message("预演尚未提交，没有新的撤销记录；Esc 可取消当前预演", error=True)
        elif name == "char" and ev.text == "q":
            self._set_message("预演仍在进行：请先 Enter 提交或 Esc 取消", error=True)
        elif name == "char":
            self._set_message("预演模式下不接受键入；Enter 提交或 Esc 取消", error=True)

    def _move(self, name: str) -> None:
        if name == "up":
            self.cur_r = max(0, self.cur_r - 1)
        elif name == "down":
            self.cur_r = min(ROWS - 1, self.cur_r + 1)
        elif name == "left":
            self.cur_c = max(0, self.cur_c - 1)
        elif name == "right":
            self.cur_c = min(COLS - 1, self.cur_c + 1)
        elif name == "home":
            self.cur_r = self.cur_c = 0
        elif name == "end":
            self.cur_r, self.cur_c = ROWS - 1, COLS - 1

    def _start_preview(self, text: str) -> None:
        if self.preview is not None:
            self._set_message("已有一个待处理预演：请先 Enter 提交或 Esc 取消", error=True)
            return
        # 锚点在开始标记到达时捕获：此刻光标位置即稳定 row_id + 列 id
        self.preview = self.model.build_preview(text, self.cur_r, self.cur_c)
        p = self.preview
        if p.height == 0:
            self.preview = None
            self._set_message("粘贴内容为空，未生成预演", error=True)
            return
        if p.ok:
            self._set_message(
                f"预演就绪：锚点 {p.anchor_row_id}/{p.anchor_col_id}，"
                f"矩形 {p.height} 行 × {p.width} 列，全部合法；Enter 提交 / Esc 取消"
            )
        else:
            self._set_message(
                f"预演发现 {len(p.errors)} 个问题：网格不会被修改，修正粘贴后重试；Esc 取消",
                error=True,
            )

    def _do_export(self) -> None:
        if not self.export_path:
            self._set_message("未提供导出路径：请用 --export 参数启动后再按 F2", error=True)
            return
        try:
            path = self.model.export_json(self.export_path)
        except OSError as exc:
            self._set_message(f"导出失败：{exc}", error=True)
            return
        self._set_message(f"已导出当前表到 {path}（row_id 与值/类型原样保留）")

    def _set_message(self, text: str, error: bool = False) -> None:
        self._message = text
        self._message_error = error

    # ---------- 绘制 ----------

    def _draw(self) -> None:
        # 使用主循环同步过的真实尺寸（_last_dim），避免 curses 缓存的旧行列导致错位
        h, w = self._last_dim or self._stdscr.getmaxyx()
        self._stdscr.erase()
        if h < MIN_H or w < MIN_W:
            self._draw_undersized(h, w)
            return
        self._draw_status(w)
        self._draw_grid()
        self._draw_preview(h, w)
        self._draw_help(h, w)

    def _safe_addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
        h, w = self._last_dim or self._stdscr.getmaxyx()
        if y < 0 or y >= h or x >= w - 1:
            return
        text = text[: max(0, w - 1 - x)]
        try:
            self._stdscr.addstr(y, x, text, attr)
        except curses.error:
            pass  # 右下角写入等极端情况，忽略以免崩溃

    @staticmethod
    def _display(value: object) -> str:
        if value is None:
            return "\\N"
        if isinstance(value, bool):
            return "true" if value else "false"
        text = str(value)
        return text.replace("\n", "\\n").replace("\t", "\\t")

    def _draw_status(self, w: int) -> None:
        col = self.model.schema.columns[self.cur_c]
        value = self.model.cells[self.cur_r][self.cur_c]
        title = " TUI 小表粘贴编辑器  20×8  row_id 只读 "
        self._safe_addstr(0, 0, title.ljust(w - 1), self.attr_header)
        coord = (
            f"当前格: 逻辑({self.cur_r + 1},{self.cur_c + 1})  "
            f"row_id={self.model.row_ids[self.cur_r]}  列id={col.id}({col.type})"
        )
        self._safe_addstr(1, 0, coord[: w - 1])
        shown = self._display(value)
        typ = TYPE_NAMES.get(type(value), type(value).__name__)
        line = f"当前值: [{typ}] {shown}"
        attr = self.attr_error if self._message_error else 0
        self._safe_addstr(2, 0, line[: w - 1])
        msg_attr = self.attr_bad if self._message_error else self.attr_ok
        self._safe_addstr(3, 0, (" " + self._message)[: w - 1], msg_attr)

    def _draw_grid(self) -> None:
        top = 5
        # 列头
        self._safe_addstr(top, 0, "row_id".ljust(ROWID_W + 1), self.attr_header)
        for c in range(COLS):
            cid = self.model.schema.columns[c].id
            self._safe_addstr(top, ROWID_W + 1 + c * COL_W, cid.ljust(COL_W - 1), self.attr_header)

        change_cells = {}
        error_cells = set()
        if self.preview is not None:
            for chg in self.preview.changes:
                change_cells[(chg.row, chg.col)] = chg
            for err in self.preview.errors:
                if err.target_row is not None and err.target_col is not None:
                    error_cells.add((err.target_row, err.target_col))

        for r in range(ROWS):
            y = top + 1 + r
            self._safe_addstr(y, 0, self.model.row_ids[r].ljust(ROWID_W + 1))
            for c in range(COLS):
                x = ROWID_W + 1 + c * COL_W
                text = self._display(self.model.cells[r][c])
                attr = 0
                if (r, c) in error_cells:
                    attr = self.attr_error
                elif (r, c) in change_cells:
                    attr = self.attr_change
                cell = " " + text[: COL_W - 2].ljust(COL_W - 2) + " "
                if r == self.cur_r and c == self.cur_c:
                    attr = self.attr_current
                self._safe_addstr(y, x, cell, attr)

    def _draw_preview(self, h: int, w: int) -> None:
        p = self.preview
        top = 5 + 1 + ROWS + 1  # 网格下空一行
        if p is None:
            self._safe_addstr(top, 0, "（无待处理预演：粘贴后此处显示矩形范围与逐格校验结果）")
            return

        r1 = p.anchor_row + 1
        r2 = p.anchor_row + p.height
        c1 = p.anchor_col + 1
        c2 = p.anchor_col + p.width
        head = (
            f"预演批次: 锚点 row_id={p.anchor_row_id} 列id={p.anchor_col_id} | "
            f"逻辑矩形 {p.height}行×{p.width}列 | 目标 行{r1}-{r2} 列{c1}-{c2} | "
            f"{'可提交' if p.ok else '拒绝提交'}"
        )
        self._safe_addstr(top, 0, head[: w - 1], self.attr_ok if p.ok else self.attr_bad)

        bottom = top + 2
        if p.parse.unterminated:
            self._safe_addstr(
                top + 1, 0,
                "注意: TSV 存在未闭合引号，已按可恢复内容尽量还原矩形，请对照源数据检查",
                self.attr_bad,
            )

        if not p.errors:
            self._safe_addstr(top + 1, 0, f"全部 {len(p.changes)} 个目标格类型校验通过，未触碰原表")
            return

        # 错误列表：每条占一行，空间不足时提示剩余条数
        avail = h - 1 - bottom  # 最后一行留给帮助
        self._safe_addstr(top + 1, 0, f"错误 {len(p.errors)} 条（源[逻辑行,列] -> 目标: 原因）:", self.attr_bad)
        shown = 0
        for idx, e in enumerate(p.errors):
            y = top + 2 + idx
            if y >= h - 2:
                break
            target = self._format_target(e)
            line = (
                f"  源[{e.source_row},{e.source_col}] -> {target}: {e.reason}"
            )
            self._safe_addstr(y, 0, line[: w - 1], self.attr_bad)
            shown += 1
        hidden = len(p.errors) - shown
        if hidden > 0:
            self._safe_addstr(h - 2, 0, f"  …另有 {hidden} 条错误未显示（可放大终端或修正粘贴内容）", self.attr_bad)

    @staticmethod
    def _format_target(e) -> str:
        if e.target_row is None or e.target_col is None:
            tr = "越界" if e.target_row is None else str(e.target_row + 1)
            tc = "越界" if e.target_col is None else str(e.target_col + 1)
            return f"({tr},{tc})"
        return f"{e.target_row_id}/{e.target_col_id}({e.target_row + 1},{e.target_col + 1})"

    def _draw_help(self, h: int, w: int) -> None:
        undo_state = "可撤销" if self.model.can_undo() else "无撤销记录"
        help_line = (
            f" 方向键移动 | 粘贴=bracketed paste 整批预演 | Enter 提交 | Esc 取消 | "
            f"Ctrl+Z 撤销({undo_state}) | F2 导出 | q 退出 "
        )
        self._safe_addstr(h - 1, 0, help_line.ljust(w - 1)[: w - 1], self.attr_header)

    def _draw_undersized(self, h: int, w: int) -> None:
        # 小屏/缩放：只显示提示与关键状态，模型与光标完全不动
        lines = [
            "终端尺寸不足",
            f"当前 {w} 列 × {h} 行；至少需要 {MIN_W} 列 × {MIN_H} 行",
            "请放大终端窗口（状态与数据已完整保留，不会丢失或错位）",
            "",
            f"当前格 逻辑({self.cur_r + 1},{self.cur_c + 1}) "
            f"row_id={self.model.row_ids[self.cur_r]} "
            f"列={self.model.schema.columns[self.cur_c].id}",
            f"当前值: {self._display(self.model.cells[self.cur_r][self.cur_c])}",
            f"预演: {'存在（Enter/Esc 处理）' if self.preview is not None else '无'}",
            "q 退出（预演中请先 Esc 取消）",
        ]
        for i, line in enumerate(lines):
            if i < h:
                self._safe_addstr(i, 0, line[: max(0, w - 1)], self.attr_bad if i < 3 else 0)
