# 题目输入材料

fixtures/performance.mid 是三轨真实SMF格式1文件；plain.mid是恒速小文件。rules.json明确同tick顺序、同音配对、踏板及截断口径。

安装 `python -m pip install -r requirements.txt`。MIDI只描述控制事件，本题实际发声指遵守声明的踏板生命周期，不模拟乐器包络自然衰减。

所有数据均为合成材料。实现代码、结果与独立核验由作答者完成。输入中的数字和规则是业务定义，不是参考答案。
