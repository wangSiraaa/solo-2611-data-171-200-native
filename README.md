# 题 171 输入材料

先运行 `python fixtures/mock_api.py --port 8171`。规范的servers仅为默认地址，客户端应可覆盖base URL。mock响应包含真实收到的raw target及服务端解释的参数，便于核对请求；它不读取OpenAPI也不生成客户端请求。

所有输入均为本地合成材料；此目录不提供题目实现、参考产物或预期答案。路径相对题目仓库根。使用 Python 3.11；无需 Docker。
