# RDF 图交换材料
文件均为本地合成 N-Quads，URL 只作 RDF IRI，不应访问网络。每文件是一份独立 dataset，空白节点标签只在该文件有效。默认图与命名图不同，命名图自身可为 blank node。

本题采用 RDF 词项身份：IRI 原字符串，literal 的解码后词法值、datatype、语言标签（不区分大小写）。plain string 等同 xsd:string；不能把 xsd:integer 的词法 `01` 自动变成 `1`。允许用 rdflib 解析；其 NORMALIZE_LITERALS 默认行为须自行注意。重复四元组折叠为集合。

plan.json 规定上限。base/renamed/changed 用于常规交换；symmetric 系列用于无固定锚点的对称图。文件名不提供规范标号或答案。实现自行定义规范序列化，包括 escaping、排序、blank label 规则并在报告公开，不强制采用某标准版本。规范结果须是完整四元组信息，而不只是邻域哈希。对两份等价输入输出的节点映射必须逐四元组可核验；非等价不要求最小编辑差异。
