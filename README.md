# 题 180 输入材料

输入是20×8小表，候选自行实现curses界面；不提供表格组件。clips/各文件为粘贴内容原始UTF-8字节。终端测试通过bracketed paste包裹发送内容，可拆成多次写入。F2导出当前表到指定路径供独立核对。

所有输入均为本地合成材料；此目录不提供题目实现、参考产物或预期答案。路径相对题目仓库根。使用 Python 3.11；无需 Docker。

valid.tsv与invalid.tsv均从name列开始预演，覆盖name/qty/price/day/active/note六列；请选择至少还剩两行的位置。
