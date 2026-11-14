# 题目输入材料

fixtures/schema_and_data.sql 可用 Python sqlite3.executescript 建库；model.json 是运行时声明；queries/ 中有合法及无法规划的请求。bad_cardinality_patch.sql 只供在关闭外键的独立副本上制造模型基数违例。

金额单位为分。所有输出规则写在模型semantics中，基础指标不能因增加另一事实行而放大。无需第三方库。

所有数据均为合成材料。实现代码、结果与独立核验由作答者完成。输入中的数字和规则是业务定义，不是参考答案。
