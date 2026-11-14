"""真实 PTY 交互测试：启动 curses 程序，分块发送 bracketed paste 与按键。

覆盖（题目要求的端到端场景，均通过屏幕画面与独立导出 JSON 双重核对）：
1. 引用内含换行/制表符的合法粘贴：画面出现预演、当前位置可见，Enter 提交、Ctrl+Z 撤销；
2. 类型错误粘贴：预演逐条显示逻辑源行列/目标坐标/原因，Enter 被拒、网格零改动；
3. Esc 取消预演：无任何半批写入；
4. ragged 缺列与 unclosed 未闭合引号：预演定位错误且不落表；
5. 标记/UTF-8 跨 chunk 拆分发送：仍作为单个粘贴事务；
6. F2 导出：独立读取 JSON 核对值与类型；
7. 小终端缩放：显示尺寸提示且状态不丢；
8. row_id 不可被粘贴改写（code 列锚点粘贴也不影响 row_id 字段）。
"""

from __future__ import annotations

import fcntl
import json
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import time
import unicodedata
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tuigrid.input import PASTE_END, PASTE_START  # noqa: E402

FIX = ROOT / "fixtures"
ART = ROOT / "artifacts"
ART.mkdir(exist_ok=True)

ENV = {
    "PATH": os.environ.get("PATH", ""),
    "TERM": "xterm-256color",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PYTHONPATH": str(ROOT / "src"),
    "PYTHONIOENCODING": "utf-8",
}

NORMAL_W, NORMAL_H = 140, 40
TINY_W, TINY_H = 40, 8


def _is_wide(ch: str) -> bool:
    """终端中占两列的宽字符（东亚全角、全角标点、CJK 等）。"""
    if not ch or ch == " ":
        return False
    return unicodedata.east_asian_width(ch) in ("W", "F")


class PtySession:
    """一个真实 PTY 中的子进程会话。"""

    def __init__(self, export_path: str | None = None, size: tuple[int, int] = (NORMAL_W, NORMAL_H)):
        self.master, self.slave = pty.openpty()
        w, h = size
        self._set_size(w, h)
        # 让 PTY 行规程原样透传控制字节：真实终端里 curses 会自己进入 raw 模式，
        # 但从主端写入时 ISIG/ICANON 仍可能吞掉 \x1a(Ctrl+Z) 等，这里预先关闭。
        for fd in (self.slave, self.master):
            attrs = termios.tcgetattr(fd)
            attrs[0] = 0  # iflag：关闭输入处理
            attrs[1] = 0  # oflag：关闭输出后处理
            attrs[3] = 0  # lflag：关闭 ISIG/ICANON/ECHO 等
            # 清空所有特殊控制字符（VSUSP/VINTR/VQUIT ...），避免被行规程拦截
            attrs[6] = [bytes([0]) if isinstance(c, bytes) else 0 for c in attrs[6]]
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
        cmd = [
            sys.executable,
            "-m",
            "tuigrid",
            "--grid",
            str(FIX / "grid.json"),
            "--schema",
            str(FIX / "schema.json"),
        ]
        if export_path:
            cmd += ["--export", str(export_path)]
        self.proc = subprocess.Popen(
            cmd,
            stdin=self.slave,
            stdout=self.slave,
            stderr=self.slave,
            env=ENV,
            close_fds=True,
        )
        os.close(self.slave)
        # 行缓冲屏幕：{行号: 字符列表}，列缺失按空格补，避免坐标错位
        self.lines: dict[int, dict[int, str]] = {}
        self.dim = (w, h)
        self._cursor = [0, 0]

    def _set_size(self, w: int, h: int) -> None:
        fcntl_pack = struct.pack("HHHH", h, w, 0, 0)
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, fcntl_pack)

    def resize(self, w: int, h: int) -> None:
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", h, w, 0, 0))
        self.dim = (w, h)
        # 部分环境下主端 ioctl 不保证递送 SIGWINCH；真实终端缩放必然发信号，这里补发
        self.proc.send_signal(signal.SIGWINCH)
        # 模拟器丢弃旧帧，避免小屏文字残留干扰断言（真实终端会被 clear() 整屏覆盖）
        self.lines.clear()
        # 给前台进程处理 SIGWINCH 的时间
        self.pump(0.4)

    def send(self, data: bytes, pause: float = 0.05) -> None:
        os.write(self.master, data)
        if pause:
            self.pump(pause)

    def send_chunked(self, data: bytes, chunk: int = 3, pause: float = 0.02) -> None:
        """按小块逐次写入，模拟 read() 拆分（含从 UTF-8 字符和转义标记中间切开）。"""
        for i in range(0, len(data), chunk):
            os.write(self.master, data[i : i + chunk])
            self.pump(pause)
        self.pump(0.15)

    def pump(self, timeout: float) -> bytes:
        """读取并应用这期间的所有终端输出，返回原始字节。"""
        deadline = time.monotonic() + timeout
        raw = bytearray()
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            r, _, _ = select.select([self.master], [], [], min(left, 0.1))
            if not r:
                continue
            try:
                chunk = os.read(self.master, 1 << 16)
            except OSError:
                break
            if not chunk:
                break
            raw.extend(chunk)
            self._apply(chunk)
        return bytes(raw)

    def _apply(self, data: bytes) -> None:
        """极简终端模拟器：处理光标定位/擦除与可打印文本，足够断言屏幕内容。"""
        text = data.decode("utf-8", errors="replace")
        i = 0
        row, col = self._cursor
        saved = (0, 0)
        n = len(text)
        while i < n:
            ch = text[i]
            if ch == "\x1b":
                if i + 1 < n and text[i + 1] == "[":
                    j = i + 2
                    params = ""
                    while j < n and not ("@" <= text[j] <= "~"):
                        params += text[j]
                        j += 1
                    final = text[j] if j < n else ""
                    row, col = self._csi(final, params, row, col)
                    i = j + 1
                    continue
                if i + 1 < n and text[i + 1] in "78":
                    if text[i + 1] == "7":
                        saved = (row, col)
                    else:
                        row, col = saved
                    i += 2
                    continue
                # ESC 后跟中间字节 '(' ')' '*' '+' 再跟一个终止字节：字符集指定，忽略两字节
                if i + 2 < n and text[i + 1] in "()*+":
                    i += 3
                    continue
                # ESC ] OSC 标题序列：吞到 BEL 或 ST(ESC \)
                if i + 1 < n and text[i + 1] == "]":
                    j = i + 2
                    while j < n and text[j] != "\x07" and not (text[j] == "\x1b" and j + 1 < n and text[j + 1] == "\\"):
                        j += 1
                    i = j + (2 if text[j:j+2] == "\x1b\\" else 1)
                    continue
                i += 1
                continue
            if ch == "\r":
                col = 0
                i += 1
                continue
            if ch == "\n":
                row += 1
                col = 0
                i += 1
                continue
            if ch == "\b":
                col = max(0, col - 1)
                i += 1
                continue
            if ch >= " ":
                # 空格也写入缓冲：curses 重绘用空格覆盖旧内容，模拟器必须同样清除
                self.lines.setdefault(row, {})[col] = ch
                # 东亚全角/宽字符在真实终端占两列，必须按宽度推进，否则后续列全错位
                col += 2 if _is_wide(ch) else 1
            i += 1
        self._cursor = [row, col]

    def _csi(self, final: str, params: str, row: int, col: int) -> tuple[int, int]:
        # 私有模式序列与 SGR 颜色：本模拟器无需动作
        if params[:1] in ("?", ">", "=") or final == "m":
            return row, col
        nums = []
        for x in params.split(";"):
            try:
                nums.append(int(x) if x else 0)
            except ValueError:
                nums.append(0)
        if not nums:
            nums = [0]
        step = nums[0] or 1
        if final in ("H", "f"):
            r = (nums[0] or 1) - 1
            c = ((nums[1] if len(nums) > 1 else 0) or 1) - 1
            return r, c
        if final == "A":
            return max(0, row - step), col
        if final == "B":
            return row + step, col
        if final == "C":
            return row, col + step
        if final == "D":
            return row, max(0, col - step)
        if final == "d":
            return step - 1, col
        if final == "G":
            return row, step - 1
        if final == "J":
            if nums[0] in (2, 3):
                self.lines.clear()
            elif nums[0] == 0:
                self.lines = {
                    r: {c: v for c, v in cells.items() if r < row or (r == row and c < col)}
                    for r, cells in self.lines.items()
                }
            return row, col
        if final == "K":
            if nums[0] in (0, 2):
                self.lines[row] = {c: v for c, v in self.lines.get(row, {}).items() if c < col}
            elif nums[0] == 1:
                self.lines[row] = {c: v for c, v in self.lines.get(row, {}).items() if c > col}
            return row, col
        return row, col

    def line(self, r: int) -> str:
        cells = self.lines.get(r)
        if not cells:
            return ""
        max_c = max(cells)
        out = []
        c = 0
        while c <= max_c:
            ch = cells.get(c, " ")
            out.append(ch)
            # 宽字符的第二列不存数据，跳过以免把后一个字符提前
            if ch != " " and _is_wide(ch):
                c += 2
            else:
                c += 1
        return "".join(out)

    def all_text(self) -> str:
        if not self.lines:
            return ""
        max_r = max(self.lines)
        return "\n".join(self.line(r) for r in range(max_r + 1))

    def normalized(self) -> str:
        """折叠连续空格后的全文，规避极简模拟器在重复重绘时的残余交叠。"""
        import re
        return "\n".join(re.sub(r"\s+", " ", ln).strip() for ln in self.all_text().splitlines())

    def sees(self, *needles: str) -> bool:
        blob = self.normalized()
        return all(x in blob for x in needles)

    def wait_for(self, needle: str, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if needle in self.normalized():
                return True
            self.pump(0.1)
        return False

    def close(self) -> None:
        try:
            self.proc.send_signal(signal.SIGTERM)
            self.proc.wait(timeout=2)
        except Exception:
            self.proc.kill()
            self.proc.wait(timeout=2)
        try:
            os.close(self.master)
        except OSError:
            pass


# 常用按键字节
KEY_UP = b"\x1b[A"
KEY_DOWN = b"\x1b[B"
KEY_RIGHT = b"\x1b[C"
KEY_LEFT = b"\x1b[D"
KEY_ENTER = b"\r"
KEY_ESC = b"\x1b"
KEY_CTRL_Z = b"\x1a"
KEY_F2 = b"\x1b[12~"
KEY_Q = b"q"


def load_clip(name: str) -> bytes:
    return (FIX / "clips" / name).read_bytes()


class PtyFlowTests(unittest.TestCase):
    def setUp(self):
        self.export = ART / "export_live.json"
        if self.export.exists():
            self.export.unlink()
        self.s = PtySession(export_path=str(self.export))
        self.assertTrue(self.s.wait_for("TUI 小表粘贴编辑器"), self.s.all_text()[:400])

    def tearDown(self):
        self.s.close()

    def _move_to_name_r18(self):
        # 起点 (1,1)=R01/code；目标 R18(行18,下标17)、name(列2,下标1)
        for _ in range(17):
            self.s.send(KEY_DOWN, pause=0.01)
        self.s.send(KEY_RIGHT, pause=0.1)
        self.assertTrue(self.s.sees("R18", "name"))

    def test_01_valid_paste_commit_undo_on_real_pty(self):
        self._move_to_name_r18()
        clip = load_clip("valid.tsv")
        # bracketed paste 分块发送：开始标记、载荷（3 字节一块，切开 UTF-8 与换行）、结束标记
        self.s.send_chunked(PASTE_START, chunk=2)
        self.s.send_chunked(clip, chunk=3)
        self.s.send_chunked(PASTE_END, chunk=2)

        # 画面必须能看到预演、锚点、矩形与当前位置
        self.assertTrue(
            self.s.sees("预演批次", "R18", "name", "2行", "6列", "可提交", "当前格"),
            self.s.normalized()[:600],
        )
        # 合法粘贴没有错误行
        self.assertNotIn("错误 1", self.s.normalized())

        # Enter 整批提交
        self.s.send(KEY_ENTER)
        self.assertTrue(self.s.wait_for("已整批提交"))
        self.assertIn("12 个单元格", self.s.normalized())

        # F2 导出，独立读取核对
        self.s.send(KEY_F2)
        self.assertTrue(self.s.wait_for("已导出当前表"))
        exported = json.loads(self.export.read_text(encoding="utf-8"))
        self.assertEqual(len(exported), 20)
        r18, r19 = exported[17], exported[18]
        self.assertEqual(r18["row_id"], "R18")  # row_id 未被改写
        self.assertEqual(r18["cells"][1], "Name 名称\nnewline")  # 引用内换行
        self.assertIsInstance(r18["cells"][1], str)
        self.assertEqual(r18["cells"][2], 0)  # 整数 0 不被吞
        self.assertIsInstance(r18["cells"][2], int)
        self.assertEqual(r18["cells"][3], "0.00")  # decimal 两位小数，仍是字符串
        self.assertIs(r18["cells"][5], False)  # 布尔 false
        self.assertEqual(r18["cells"][6], 'quote "and" tab\there')  # 引用内制表符
        self.assertEqual(r19["cells"][1], "Second")
        self.assertEqual(r19["cells"][2], 12)
        self.assertEqual(r19["cells"][3], "3.40")
        self.assertIsNone(r19["cells"][6])  # \N -> null
        # 未触及的行与原表一致
        original = json.loads((FIX / "grid.json").read_text("utf-8"))
        self.assertEqual(exported[0], original[0])
        self.assertEqual(exported[16], original[16])

        # Ctrl+Z 整块撤销（含多行文本），再导出核对恢复
        self.s.send(KEY_CTRL_Z)
        self.assertTrue(self.s.wait_for("已撤销最近一次粘贴"))
        self.s.send(KEY_F2)
        self.assertTrue(self.s.wait_for("已导出当前表"))
        undone = json.loads(self.export.read_text(encoding="utf-8"))
        self.assertEqual(undone, original)

    def test_02_invalid_paste_blocks_commit_and_no_half_write(self):
        self._move_to_name_r18()
        clip = load_clip("invalid.tsv")
        self.s.send_chunked(PASTE_START + clip + PASTE_END, chunk=4)
        screen = self.s.normalized()
        self.assertIn("预演批次", screen)
        self.assertIn("拒绝提交", screen)
        # 四条错误都要在画面上，且带逻辑源行列与目标坐标
        self.assertEqual(screen.count("源[2,"), 4)
        self.assertTrue(self.s.sees("qty", "price", "day", "active", "R19"))  # 源行 2 -> R19
        # Enter 必须被拒
        self.s.send(KEY_ENTER)
        self.assertTrue(self.s.wait_for("Enter 提交被拒绝"))
        # F2 导出：与原表逐格相等，证明没有半批写入
        self.s.send(KEY_F2)
        self.assertTrue(self.s.wait_for("已导出当前表"))
        exported = json.loads(self.export.read_text("utf-8"))
        original = json.loads((FIX / "grid.json").read_text("utf-8"))
        self.assertEqual(exported, original)
        # 撤销记录不应产生
        self.assertIn("无撤销记录", self.s.normalized())

    def test_03_escape_cancels_preview_without_write(self):
        self._move_to_name_r18()
        self.s.send_chunked(PASTE_START + load_clip("valid.tsv") + PASTE_END, chunk=5)
        self.assertTrue(self.s.wait_for("预演批次"))
        # 悬空 ESC（先发 ESC，不跟其他字节）应被识别为取消键
        self.s.send(KEY_ESC)
        self.s.pump(0.25)
        self.assertTrue(self.s.wait_for("已取消粘贴预演"))
        self.assertNotIn("预演批次", self.s.normalized())
        self.s.send(KEY_F2)
        self.assertTrue(self.s.wait_for("已导出当前表"))
        exported = json.loads(self.export.read_text("utf-8"))
        original = json.loads((FIX / "grid.json").read_text("utf-8"))
        self.assertEqual(exported, original)

    def test_04_ragged_and_unclosed_clips(self):
        # 移动到 R03 / code 附近（起点直接粘贴，锚点 R01/code）
        for _ in range(2):
            self.s.send(KEY_DOWN, pause=0.01)
        self.s.pump(0.15)
        self.s.send_chunked(PASTE_START + load_clip("ragged.tsv") + PASTE_END, chunk=2)
        screen = self.s.normalized()
        self.assertIn("拒绝提交", screen)
        self.assertIn("缺列", screen)
        self.s.send(KEY_ESC)
        self.assertTrue(self.s.wait_for("已取消粘贴预演"))

        self.s.send_chunked(PASTE_START + load_clip("unclosed.tsv") + PASTE_END, chunk=2)
        screen = self.s.normalized()
        self.assertIn("未闭合", screen)
        self.assertIn("拒绝提交", screen)
        self.s.send(KEY_ESC)
        self.assertTrue(self.s.wait_for("已取消粘贴预演"))

        self.s.send(KEY_F2)
        self.assertTrue(self.s.wait_for("已导出当前表"))
        original = json.loads((FIX / "grid.json").read_text("utf-8"))
        self.assertEqual(
            json.loads(self.export.read_text("utf-8")), original
        )

    def test_05_marker_split_at_byte_boundaries(self):
        # 开始标记从中间切开，载荷含多字节 UTF-8，也逐字节拆
        self._move_to_name_r18()
        stream = PASTE_START + load_clip("valid.tsv") + PASTE_END
        self.s.send_chunked(stream, chunk=1, pause=0.005)
        self.assertTrue(self.s.wait_for("可提交"), self.s.normalized()[:600])
        self.s.send(KEY_ESC)
        self.assertTrue(self.s.wait_for("已取消粘贴预演"))

    def test_06_arrow_moves_and_current_value_displayed(self):
        screen = self.s.normalized()
        self.assertIn("逻辑(1,1)", screen)
        self.s.send(KEY_DOWN)
        self.s.pump(0.15)
        self.assertIn("逻辑(2,1)", self.s.normalized())
        self.s.send(KEY_RIGHT)
        self.s.pump(0.15)
        screen = self.s.normalized()
        self.assertIn("逻辑(2,2)", screen)
        self.assertIn("Object 2", screen)  # 当前值可见
        # 方向键在边界不越界崩溃
        for _ in range(30):
            self.s.send(KEY_RIGHT, pause=0.005)
        for _ in range(30):
            self.s.send(KEY_DOWN, pause=0.005)
        self.s.pump(0.2)
        self.assertIn("逻辑(20,8)", self.s.normalized())

    def test_07_quit(self):
        self.s.send(KEY_Q)
        try:
            rc = self.s.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.fail("按 q 后进程未退出")
        self.assertEqual(rc, 0)

    def test_08_failed_and_cancelled_previews_keep_undo_stack_intact(self):
        original = json.loads((FIX / "grid.json").read_text("utf-8"))
        # 1) 先成功提交一版（锚点 R18/name）
        self._move_to_name_r18()
        self.s.send_chunked(PASTE_START + load_clip("valid.tsv") + PASTE_END, chunk=4)
        self.assertTrue(self.s.wait_for("可提交"))
        self.s.send(KEY_ENTER)
        self.assertTrue(self.s.wait_for("已整批提交"))

        # 2) 再来一个类型错误预演，Enter 被拒；它不得覆盖上一次的撤销快照
        self.s.send_chunked(PASTE_START + load_clip("invalid.tsv") + PASTE_END, chunk=4)
        self.assertTrue(self.s.wait_for("拒绝提交"))
        self.s.send(KEY_ENTER)
        self.assertTrue(self.s.wait_for("Enter 提交被拒绝"))

        # 3) 再取消一个合法预演，同样不应触碰撤销栈
        self.s.send(KEY_ESC)
        self.assertTrue(self.s.wait_for("已取消粘贴预演"))
        self.s.send_chunked(PASTE_START + load_clip("valid.tsv") + PASTE_END, chunk=4)
        self.assertTrue(self.s.wait_for("可提交"))
        self.s.send(KEY_ESC)
        self.assertTrue(self.s.wait_for("已取消粘贴预演"))

        # 4) 唯一一次 Ctrl+Z 必须恢复到第一次成功提交之前的原表
        self.s.send(KEY_CTRL_Z)
        self.assertTrue(self.s.wait_for("已撤销最近一次粘贴"))
        self.s.send(KEY_F2)
        self.assertTrue(self.s.wait_for("已导出当前表"))
        self.assertEqual(
            json.loads(self.export.read_text("utf-8")), original
        )
        # 撤销栈只有一层：再按 Ctrl+Z 提示无记录
        self.s.send(KEY_CTRL_Z)
        self.assertTrue(self.s.wait_for("没有可撤销的粘贴记录"))

    def test_09_row_id_not_editable_via_paste(self):
        # 锚点放在 code 列（最左可粘贴列），row_id 在其左侧、不属于任何粘贴列
        original = json.loads((FIX / "grid.json").read_text("utf-8"))
        payload = b"HACKED\tName X\t5\t9.99\t2026-01-01\ttrue\tnote\tZ\n"
        self.s.send_chunked(PASTE_START + payload + PASTE_END, chunk=3)
        self.assertTrue(self.s.wait_for("可提交"), self.s.normalized()[:500])
        self.s.send(KEY_ENTER)
        self.assertTrue(self.s.wait_for("已整批提交"))
        self.s.send(KEY_F2)
        self.assertTrue(self.s.wait_for("已导出当前表"))
        exported = json.loads(self.export.read_text("utf-8"))
        # code 列（cells[0]）被改成 HACKED，但 row_id 字段必须保持 R01
        self.assertEqual(exported[0]["row_id"], "R01")
        self.assertEqual(exported[0]["cells"][0], "HACKED")
        # 其余行 row_id 全部原样
        self.assertEqual([r["row_id"] for r in exported], [r["row_id"] for r in original])


class PtyResizeTests(unittest.TestCase):
    def test_small_then_grow_preserves_state(self):
        export = ART / "export_resize.json"
        s = PtySession(export_path=str(export), size=(TINY_W, TINY_H))
        self.assertTrue(s.wait_for("终端尺寸不足"))
        # 小屏下按键也不应崩溃或错位
        s.send(KEY_DOWN)
        s.send(KEY_RIGHT)
        s.pump(0.2)
        tiny = s.normalized()
        self.assertIn("终端尺寸不足", tiny)
        # 放大后界面恢复，光标移动被保留（R02/name = 逻辑(2,2)）
        s.resize(NORMAL_W, NORMAL_H)
        self.assertTrue(s.wait_for("TUI 小表粘贴编辑器"))
        screen = s.normalized()
        self.assertIn("逻辑(2,2)", screen)
        s.send(KEY_F2)
        self.assertTrue(s.wait_for("已导出当前表"))
        original = json.loads((FIX / "grid.json").read_text("utf-8"))
        self.assertEqual(json.loads(export.read_text("utf-8")), original)
        s.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
