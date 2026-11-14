# 题目输入材料

regions.geojson包含6个有效相邻Polygon，protected.geojson给固定外域与孔洞，plan.json给误差及拓扑口径。两侧的共线分段数可不同。

安装 `python -m pip install -r requirements.txt`。无需坐标转换。保护孔洞附近的边界候选必须结合区域关系检查，不可把孤立Polygon.is_valid作为全域验收。

所有数据均为合成材料。实现代码、结果与独立核验由作答者完成。输入中的数字和规则是业务定义，不是参考答案。
