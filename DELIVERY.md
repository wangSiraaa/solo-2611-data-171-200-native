# 规格驱动本地 API 客户端

Python 3.11，OpenAPI 3.0 子集。先编译调用计划，再按计划发送；非法输入在产生任何网络请求之前被拒绝。

## 运行

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 终端 1：题目提供的 mock（必要时改端口）
.venv/bin/python fixtures/mock_api.py --port 8171

# 终端 2
PYTHONPATH=src .venv/bin/python -m api_contract compile --spec fixtures/openapi.json
PYTHONPATH=src .venv/bin/python -m api_contract plan    --spec fixtures/openapi.json --requests fixtures/requests.json --out .runtime/plan.json
PYTHONPATH=src .venv/bin/python -m api_contract call    --spec fixtures/openapi.json --requests fixtures/requests.json --out .runtime/call.json
PYTHONPATH=src .venv/bin/python -m api_contract call    --spec fixtures/openapi.json --requests fixtures/requests.json --base-url http://127.0.0.1:9000
```

`plan` 只编译与校验，**零网络请求**；`call` 先记录同样的计划，再逐个发送。`--base-url` 只替换服务器地址，规范里的路径模板与操作语义不变。

## 库入口

```python
from api_contract import Client, compile_operation, InputError, CompileError

plan = compile_operation(spec, "search")           # 编译期错误 -> CompileError
client = Client(spec, base_url=override_or_none)
request = client.plan("search", parameters={...})   # 校验 + 生成 target；不联网
request.target   # 原始请求 target，例如 /search?tags=a%2Cb
result = client.call("search", parameters={...})    # 发送并返回 CallResult
result.status_code, result.content_type, result.json, result.target
```

- `compile_operation(spec, operationId, base_url=None)` / `Client.compile(...)`
- `Client.plan(...)`：返回 `RequestPlan`，任何非法输入抛 `InputError`
- `Client.call(...)`：返回 `CallResult`，不把错误响应包装成成功对象；真实状态码与 `Content-Type` 原样记录
- 主体用 `_UNSET` 哨兵区分“未提供”与“显式 JSON null”

## 支持范围（超出即拒绝，不猜测）

| 维度 | 支持 |
| --- | --- |
| 文档 | OpenAPI 3.0.x，`operationId` 唯一定位；重复则拒绝 |
| `$ref` | 仅文档内 JSON Pointer（`#/...`）；缺失目标、外部引用、循环、带兄弟键一律报错并给规格位置 |
| 参数位置 | `path`、`query` |
| 样式 | path `simple`；query `form`；query 平面 `deepObject`（`explode=true`、仅原始类型属性、`additionalProperties: false`） |
| 参数 schema | `object`/`array`/`string`/`integer`/`number`/`boolean`；数组元素为原始类型 |
| 请求体 | `application/json`，`$ref` 到本地 schema，支持 `nullable` |
| 响应 | 普通 JSON；其他响应类型不解析，仍记录真实状态码 |
| 明确不支持 | `matrix`/`label`/`spaceDelimited`/`pipeDelimited`、表单平面对象、`allOf` 等组合、鉴权、重试、分页、全 OpenAPI 兼容、SDK 生成 |

编译期发现的冲突/未知项（样式、参数重名、`operationId` 歧义、路径占位符与参数不符等）在**发送前**抛 `CompileError`。

## 序列化规则

1. 先对**数据原子**做 UTF-8 百分号编码（`quote(..., safe="")`），再拼装结构分隔符。
   - `north,south` -> `north%2Csouth`，数组内逗号不会变成分隔符；
   - 路径中的 `/`、空格、`#` -> `%2F` `%20` `%23`，不改变 URL 结构；
   - `+` 编码为 `%2B`，`&` 编码为 `%26`。
2. 结构字符保持字面：form 非 explode 数组用 `,` 连接；deepObject 键为 `name[prop]`。
3. 重复 query 键按数组逐项输出，不用字典合并。
4. `false` 与 `0` 按原值输出，不因真假判断被省略。
5. 空数组省略整键；空字符串输出 `name=`；`null` 在 query/path 一律拒绝，只有 body 中声明 `nullable: true` 才允许。
6. 请求体 JSON 保留类型（布尔、整数、数组、`null`），键排序后编码，因此输入对象键顺序不影响请求。

## 校验与定位

错误同时给出调用输入位置与规格位置：

```
InputError: 'false' is not of type 'boolean'
  input=parameters/active
  spec=#/paths/~1search/get/parameters/1/$ref -> #/components/parameters/Active/schema/type
```

`additionalProperties` 指向违规键与 `schema/additionalProperties`，`required` 指向缺失键。被拒绝的用例 `network_requests` 恒为 0。

## 报告格式

`plan` / `call` 输出：

```json
{
  "mode": "call",
  "plans": { "<operationId>": { "status": "compiled", "plan": { ... } } },
  "cases": [
    {
      "case_id": "...", "operationId": "...", "status": "called",
      "network_requests": 1,
      "request": {
        "operation": {"operationId": "...", "method": "GET", "path_template": "/..."},
        "base_url": "http://...", "target": "/search?...", "url": "http://.../search?...",
        "path": "/search", "query": "tags=...",
        "parameters": [{"name": "...", "in": "...", "source": "...", "provided": true,
                        "omitted": null, "style": "...", "explode": false,
                        "required": false, "spec_location": "#/paths/..."}],
        "query_pairs": [{"key": "tags", "value": "...", "parameter": "tags",
                         "logical_key": "tags", "atom": null}],
        "body": null,
        "headers": {}
      },
      "result": {"status_code": 200, "content_type": "application/json",
                 "target": "...", "json": {...}, "response_text": "...",
                 "request_headers": {}, "response_headers": {}}
    },
    {
      "case_id": "...", "status": "rejected", "network_requests": 0,
      "rejection": {"error_type": "InputError", "message": "...",
                    "input_location": "...", "spec_location": "..."}
    }
  ],
  "summary": {"cases": 7, "called": 5, "rejected": 2,
              "transport_errors": 0, "network_requests": 5}
}
```

状态：`planned`（仅 plan 模式）/ `called` / `rejected`（编译或输入校验失败）/ `transport_error`（已尝试但无响应）。

## 独立核对

测试不仅做序列化往返，还用**手工写死的 URL** 与生成结果比对，再让服务端解析：

- 手工 `GET /assets/a%2Fb%20c%23d` -> mock 看到单段路径、`asset_id == "a/b c#d"`；
- 手工 `/search?tags=x%2Cy,z&...&filter[min_age]=0` -> mock 解释出 `["x,y","z"]`、`false`、`0`；
- 直接调用 mock 的 `/labels?...` 确认重复键逐对存在、空字符串保留；
- POST body 确认 `columns` 保持数组、`note` 为 `null`。

```sh
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

## 边界

- 不处理磁盘/URL 外部 `$ref`，不实现鉴权、重试、重定向、分页/续采。
- 不根据 `oneOf` 等组合推断类型；不把字符串强制转换为数字/布尔。
- 不支持 `multipart`、非 JSON 请求体；响应只在 `Content-Type` 含 `application/json` 时解析。
- 服务器 URL 变量（`{var}`）不展开；带 query/fragment 的 base URL 被拒绝。
- 空字符串路径参数允许（`/assets/`），是否业务有效由服务端判断。
