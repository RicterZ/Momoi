# Provider 连接测试接口

测试使用当前表单的未保存配置，发出真实请求。不会保存配置、修改 revision、
触发运行时重载、写入对话记录或本地用量统计。测试实例与运行中的服务实例独立，
结束后关闭连接。模型测试可能产生少量供应商侧 Token 费用。

## 按钮显示规则

`GET /api/settings/configuration` 的 `adapters[]` 增加布尔字段 `test_supported`：

```json
{
  "adapter": "openai",
  "capability": "llm",
  "fields": {},
  "test_supported": true
}
```

上例省略了实际 `fields`。前端按当前 capability 和 adapter 查找元数据，
仅当 `test_supported === true` 时显示“测试连接”。不按厂商名写判断。

| capability | adapter | 支持测试 |
| --- | --- | --- |
| `llm` | `openai` | 是 |
| `llm` | `anthropic` | 是 |
| `embedding` | `openai` | 是 |
| `asr` | `tencent` | 否 |
| `tts` | `fish` | 否 |
| `balance` | `deepseek` | 否 |

未修改的配置、尚未保存的配置，以及已停用的 embedding 都可测试。
测试不改变启用状态。修改字段或切换 adapter 后，应清除旧结果；请求过程中
禁用重复测试，并忽略与当前表单不匹配的迟到结果。

## 发起测试

`POST /api/settings/providers/{capability}/test`

使用现有 dashboard JWT：`Authorization: Bearer <token>`。
请求体直接传配置，不包 `document`，不需要 revision：

```json
{
  "adapter": "openai",
  "options": {
    "base_url": "https://api.example.com/v1",
    "api_key": "当前输入的密钥",
    "model": "当前输入的模型名称"
  }
}
```

Embedding 示例：

```json
{
  "adapter": "openai",
  "options": {
    "endpoint": "http://embedding:8002/v1/embeddings",
    "model": "BAAI/bge-small-zh-v1.5",
    "dimensions": 512,
    "calibration_profile": "bge-small-zh-v1.5-momoi-v1"
  }
}
```

`options` 采用和保存接口相同的类型规则：仅提交当前 adapter schema 中的字段；
数字为 JSON 数字，布尔为 JSON 布尔，JSON 参数为对象。后端应用 schema 默认值并校验，
不会从已保存配置补齐密钥或其他字段。支持已有 `{ "env": "变量名" }` 密钥引用。
可选的 `enabled` 必须为布尔值，但不影响测试执行。

模型测试发一条“Reply OK”请求，使用当前协议、地址、密钥、模型和生成参数；
仅在测试实例中将重试次数设为 0。必须返回有效文本才判定通过。
Embedding 分别执行一次查询编码和文档编码，检查响应数量、向量维度及有效数值；
不调用供应商未必支持的 `/healthz`。不构建索引，也不测试召回质量或校准阈值。

网络请求从 Momoi 后端发出。总超时为 30 秒，连接资源关闭后返回；前端建议设置
至少 40 秒的请求超时。服务端同时只执行一个连接测试，避免连续点击重复调用。

## 响应与错误处理

测试成功，HTTP 200：

```json
{
  "ok": true,
  "capability": "embedding",
  "adapter": "openai",
  "elapsed_ms": 126,
  "details": {
    "model": "BAAI/bge-small-zh-v1.5",
    "dimensions": 512
  }
}
```

模型成功时 `details` 仅包含 `model`。`elapsed_ms` 为整数毫秒。

远程调用失败仍返回 HTTP 200，通过 `ok: false` 区分：

```json
{
  "ok": false,
  "capability": "llm",
  "adapter": "openai",
  "elapsed_ms": 85,
  "error": {
    "code": "authentication",
    "message": "认证失败，请检查密钥及权限。",
    "http_status": 401
  }
}
```

`http_status` 仅在可获得上游 HTTP 状态码时提供。前端可直接展示 `error.message`，
并附加上游状态码。不要仅根据 HTTP 200 显示“测试成功”。

| HTTP 状态 | error.code | 含义 |
| --- | --- | --- |
| 200 | `authentication` | 上游认证失败 |
| 200 | `connection` | DNS、网络、TLS 等连接错误 |
| 200 | `timeout` | 单次请求或整个测试超时 |
| 200 | `rate_limit` | 上游返回 429 |
| 200 | `server` | 上游服务异常 |
| 200 | `invalid_response` | 响应格式、向量维度或生成内容无效 |
| 200 | `request` | 地址、模型、参数等请求错误 |
| 400 | `validation` | 本地字段校验失败、未知 adapter/capability、请求 JSON 无效 |
| 400 | `unsupported` | adapter 未注册测试能力 |
| 429 | `busy` | 另一个测试正在执行 |

400/429 使用同样的 `ok: false` 响应结构。未通过 dashboard 鉴权时返回现有的
401 响应，不使用上述错误结构。所有响应沿用 dashboard 的 `Cache-Control: no-store`。

## 后端 adapter 扩展

`Adapter` 新增可选的 `test` 异步回调，签名为：

```python
async def test_provider(instance) -> dict:
    # 使用已创建、已进入资源上下文的临时 provider 实例执行真实验证。
    await instance.check_connection()
    return {"checked": True}
```

注册时传 `test=test_provider`；不传则 `test_supported` 为 false。
回调成功返回可 JSON 序列化的详情，失败抛异常。优先使用携带 category 的
`IntegrationError`。测试回调只操作测试实例，不获取应用 Store 或运行时实例。
接口负责 schema 校验、隔离实例、生命周期、总超时与统一结果转换。
新增 adapter 无需修改公共 HTTP 路由或前端按钮逻辑。
