# 题目输入材料

fixtures/repo.bundle 为真实Git仓库包，可用 `git clone fixtures/repo.bundle work-repo` 创建分析副本；release-plan.json给出两个标签及有限范围。

仓库使用合成身份，不连接任何远程地址。Git是运行环境依赖。不要改bundle或用作者时间先后代替父子可达关系。允许读取Git对象，但审计判定及净差量核验应由自己的工具组织。

所有数据均为合成材料。实现代码、结果与独立核验由作答者完成。输入中的数字和规则是业务定义，不是参考答案。
