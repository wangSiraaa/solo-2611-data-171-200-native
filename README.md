# 题目输入材料

assets/ 是实际可运行的生成代码和map；intermediate/ 是第二层映射；src/ 是原源文件。fixtures/manifest.json 声明文件身份、路径根及查询口径，*.stack.txt包含两份真实采集错误栈与明确标注的合成边界探针。

重新执行 `node assets/basic.cjs` 与 `node assets/indexed.cjs` 会故意抛错并退出1。采集主机版本见capture.json；目标环境Node20可以运行同样源文件。无map的Node内部帧保留原因即可。source map没有提供定位答案清单。

所有数据均为合成材料。实现代码、结果与独立核验由作答者完成。输入中的数字和规则是业务定义，不是参考答案。
