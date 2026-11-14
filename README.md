# 冻结站点材料

fixtures/site.warc 为真实未压缩WARC/1.1，21条记录；capture-inventory.json只列原始捕获身份，不是导出结果。plan.json声明两个cutoff、URL身份与revisit引用政策。原站example.invalid地址是合成身份，禁止联网获取。

包含普通响应、较新revisit、显式丢引用与循环、仅digest复用、摘要不符、未来捕获、HTML base、CSS相对url、带query的同路径页面、fragment与redirect。5份逻辑HTML内容、2个CSS及微型PNG保持规模很小。

安装requirements中的普通Python库。无需Docker或浏览器；用本地HTTP请求及解析导出文件也可验证导航和资源闭合。提供的是输入，不含重建实现或答案路径。
