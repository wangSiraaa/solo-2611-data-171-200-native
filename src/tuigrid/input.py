"""终端字节流输入解析器（不依赖 curses getch，直接读 os.read 的原始字节）。

为什么自己解析：
- bracketed paste 的起止标记（``ESC[200~`` / ``ESC[201~``）以及多字节 UTF-8 字符
  可能被拆到多次 read() 中，curses 的按键抽象无法保证把整个粘贴作为一个事务交付；
- 粘贴内容里的 ``\\n`` 必须作为数据而不是 Enter 按键，只有字节级状态机才能区分
  “粘贴批次内的换行”与“真实按键”。

解析器是纯逻辑、可单测的：:meth:`feed` 吃任意大小的字节块，:meth:`idle` 在读取
超时时调用以处理悬空 ESC。终端侧需要先开启 bracketed paste 模式（见 app.py）。
"""

from __future__ import annotations

from dataclasses import dataclass

# bracketed paste 模式标记
PASTE_START = b"\x1b[200~"
PASTE_END = b"\x1b[201~"

# 方向键与常用按键（curses keypad 开启后常见 CSI 形式；同时兼容光标模式 SS3）
_KEY_MAP = {
    b"\x1b[A": "up",
    b"\x1b[B": "down",
    b"\x1b[C": "right",
    b"\x1b[D": "left",
    b"\x1b[H": "home",
    b"\x1b[F": "end",
    b"\x1bOA": "up",
    b"\x1bOB": "down",
    b"\x1bOC": "right",
    b"\x1bOD": "left",
    b"\x1bOH": "home",
    b"\x1bOF": "end",
    b"\x1b[12~": "f2",
    b"\x1b[Z": "shift_tab",
}


@dataclass
class Event:
    kind: str  # key | paste | paste_error
    name: str = ""        # key 名称：up/down/.../enter/esc/ctrl_z/f2/ctrl_c/tab/ctrl_l
    text: str = ""        # 普通可打印字符，或 paste 的 payload 文本
    reason: str = ""      # paste_error 原因


class InputParser:
    def __init__(self) -> None:
        self._buf = bytearray()       # 普通模式下待解析字节
        self._in_paste = False
        self._paste = bytearray()     # 粘贴累积字节

    # ---------- 对外接口 ----------

    def feed(self, chunk: bytes) -> list[Event]:
        events: list[Event] = []
        self._buf.extend(chunk)
        if self._in_paste:
            self._drain_paste(events)
        else:
            self._drain_normal(events)
        return events

    def idle(self) -> list[Event]:
        """read 超时（无新字节）时调用：把悬空 ESC 判定为单独 Esc 按键。"""
        events: list[Event] = []
        if self._in_paste:
            return events  # 粘贴未结束前不产生任何按键事件
        if self._buf and self._buf[0] == 0x1B:
            events.append(Event(kind="key", name="esc"))
            # 只消费 ESC 本身，后续字节（可能是下一按键）留给下一轮解析
            del self._buf[:1]
        return events

    # ---------- 粘贴模式 ----------

    def _drain_paste(self, events: list[Event]) -> None:
        data = self._buf
        while True:
            idx = data.find(PASTE_END)
            if idx >= 0:
                self._paste.extend(data[:idx])
                del data[: idx + len(PASTE_END)]
                self._in_paste = False
                self._finish_paste(events)
                # 结束标记之后若还有字节，按普通输入继续解析
                if data:
                    self._drain_normal(events)
                return
            # 没找到完整结束标记：保留可能是标记前缀的尾部，其余进 payload
            keep = _prefix_len(data, PASTE_END)
            if keep:
                self._paste.extend(data[: -keep])
                tail = bytes(data[-keep:])
                data.clear()
                data.extend(tail)
            else:
                self._paste.extend(data)
                data.clear()
            return

    def _finish_paste(self, events: list[Event]) -> None:
        raw = bytes(self._paste)
        self._paste = bytearray()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            events.append(
                Event(
                    kind="paste_error",
                    reason=f"粘贴内容不是合法 UTF-8（字节 {exc.start}..{exc.end} 无法解码）",
                )
            )
            return
        events.append(Event(kind="paste", text=text))

    # ---------- 普通模式 ----------

    def _drain_normal(self, events: list[Event]) -> None:
        data = self._buf
        while data:
            # 粘贴开始标记（可能跨 chunk，用前缀匹配保护尾部）
            if data[0] == 0x1B:
                start_idx = data.find(PASTE_START)
                if start_idx == 0:
                    del data[: len(PASTE_START)]
                    self._in_paste = True
                    self._drain_paste(events)
                    return
                event, consumed = self._try_escape(data)
                if event is not None:
                    events.append(event)
                    del data[:consumed]
                    continue
                if consumed == 0:
                    # 悬空 ESC / 不完整转义序列：等待更多字节或 idle() 裁决
                    return
                # 完整但无需上报的转义序列（其他功能键）：整体消费掉
                del data[:consumed]
                continue

            b = data[0]
            if b == 0x0D or b == 0x0A:  # CR / LF => Enter
                events.append(Event(kind="key", name="enter"))
                del data[:1]
            elif b == 0x1A:  # Ctrl+Z
                events.append(Event(kind="key", name="ctrl_z"))
                del data[:1]
            elif b == 0x03:  # Ctrl+C
                events.append(Event(kind="key", name="ctrl_c"))
                del data[:1]
            elif b == 0x09:  # Tab
                events.append(Event(kind="key", name="tab"))
                del data[:1]
            elif b == 0x0C:  # Ctrl+L
                events.append(Event(kind="key", name="ctrl_l"))
                del data[:1]
            elif b < 0x20 or b == 0x7F:
                # 其他控制字符/退格：本应用不做单元格内编辑，忽略
                del data[:1]
            else:
                # 可打印字节：按 UTF-8 多字节序列整体取走（支持跨 read 的拆分字符）
                n = _utf8_seq_len(b)
                if n == 0:
                    # 非法首字节：立即报错并跳过，保证状态机不崩
                    events.append(
                        Event(kind="paste_error", reason=f"非法 UTF-8 首字节：0x{b:02X}")
                    )
                    del data[:1]
                    continue
                if len(data) < n:
                    return  # 多字节字符被拆到下次 read，等待
                seq = bytes(data[:n])
                try:
                    text = seq.decode("utf-8")
                except UnicodeDecodeError:
                    events.append(
                        Event(
                            kind="paste_error",
                            reason=f"非法 UTF-8 序列：{seq.hex(' ')}",
                        )
                    )
                    del data[:n]
                    continue
                events.append(Event(kind="key", name="char", text=text))
                del data[:n]

    def _try_escape(self, data: bytearray) -> tuple[Event | None, int]:
        """返回 (事件, 消耗字节数)。消耗 0 表示序列不完整需等待；正整数为整序列消费。"""
        # 已知完整序列优先精确匹配
        for seq, name in _KEY_MAP.items():
            if data.startswith(seq):
                return Event(kind="key", name=name), len(seq)

        # 通用 CSI/SS3 序列解析：ESC [ params intermediates final
        if len(data) >= 2 and data[1] in (0x5B, 0x4F):  # [ 或 O
            kind = data[1]
            i = 2
            if kind == 0x5B:
                while i < len(data) and 0x30 <= data[i] <= 0x3F:
                    i += 1  # 参数字节
                while i < len(data) and 0x20 <= data[i] <= 0x2F:
                    i += 1  # 中间字节
            else:
                # SS3（ESC O x）：x 直接是终止字节，没有参数段
                if len(data) < 3:
                    return None, 0
            if i >= len(data):
                return None, 0  # 还没收到终止字节
            if 0x40 <= data[i] <= 0x7E:
                # 完整但未在键位表中（如其他功能键）：忽略整个序列
                return None, i + 1
            # 终止字节非法：同样吞掉已接收部分，避免状态机卡死
            return None, i + 1

        # 粘贴开始标记的不完整前缀（ESC 后是 [、[2、[20 ...）：等待更多字节
        if len(data) < len(PASTE_START) and PASTE_START.startswith(bytes(data)):
            return None, 0

        # ESC 后紧跟普通字节：ESC 自身就是 Esc 键，余下字节下次再解析
        if len(data) >= 2:
            return Event(kind="key", name="esc"), 1
        # 只有一个 ESC：等待 idle() 或后续字节
        return None, 0


def _utf8_seq_len(first: int) -> int:
    if first < 0x80:
        return 1
    if 0xC2 <= first <= 0xDF:
        return 2
    if 0xE0 <= first <= 0xEF:
        return 3
    if 0xF0 <= first <= 0xF4:
        return 4
    return 0  # 非法首字节（含 continuation byte 0x80..0xBF、0xC0/0xC1、0xF5+）


def _prefix_len(data: bytearray, marker: bytes) -> int:
    """data 末尾与 marker 前缀的最长匹配长度（用于跨 chunk 的标记拼接）。"""
    max_k = min(len(data), len(marker) - 1)
    for k in range(max_k, 0, -1):
        if marker.startswith(bytes(data[-k:])):
            return k
    return 0
