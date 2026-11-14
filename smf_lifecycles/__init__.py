"""SMF 格式 1 音符生命周期离线解析工具。

只解析 MIDI 控制事件，不合成音频；“实际发声结束”严格按 CC64/CC66
踏板生命周期推导（见 fixtures/rules.json），不模拟乐器包络自然衰减。
"""

from .analyze import analyze
from .reader import parse_file

__all__ = ["analyze", "parse_file"]
