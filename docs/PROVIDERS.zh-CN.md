# 外部服务 Provider

[EN](./PROVIDERS.md) | 中文

外部 API 统一配置在 `providers.yaml`。主配置 `config.json` 只写
`"providers": "providers.yaml"`，路径相对于主配置所在目录。Dashboard 设置页根据 adapter
字段定义生成能力表单，保存前统一校验并原子写入。启用 dashboard 时，配置变化会自动重建业务实例；
dashboard 本身保持可用，服务配置仍持久化在 YAML 中。

从[完整示例](../config.example/providers.yaml)开始，启用所需能力并填写凭据。

## 连接服务

Service 定义适配器、端点、凭据和共享默认参数；Binding 为某项能力选择服务并配置
任务参数。同一个服务可以绑定多个能力，前提是适配器实现了这些能力。

```yaml
version: 1
credentials:
  deepseek:
    api_key: {env: DEEPSEEK_API_KEY}
services:
  deepseek:
    adapter: openai
    base_url: https://api.deepseek.com
    credentials: deepseek
  deepseek_balance:
    adapter: deepseek
    credentials: deepseek
bindings:
  llm:
    service: deepseek
    options:
      model: deepseek-v4-flash
      accounting: deepseek
      max_tokens: 16384
      thinking:
        effort: high
        stages:
          reply_followup: low
  balance:
    service: deepseek_balance
    options:
      timeout_seconds: 10
```

在 Momoi 进程环境中设置 `DEEPSEEK_API_KEY`。Docker 部署需要通过容器的
`environment` 或 `env_file` 传入。凭据也可以直接写字符串。
只有明确的 `{env: NAME}` 会读取环境变量，不会自动展开字符串中的 `${NAME}`。

| 字段 | 含义 |
| --- | --- |
| `version` | 必填整数 `1` |
| `plugins` | 可选的 Python 模块名列表，启动校验前导入，模块需安装在运行环境 |
| `credentials` | 命名凭据组，例如 `api_key` 或腾讯的 `secret_id` / `secret_key` |
| `services` | 命名服务：`adapter`，以及可选的 `credentials`、`base_url`、`timeout_seconds`、`settings` |
| `bindings` | 能力映射：`service`，可选布尔值 `enabled`（默认 `true`）和 `options` |

参数按服务 `settings` → 服务端点/超时 → binding `options` → 凭据字段合并。
凭据字段不能在 options 中重复定义。启动前会校验未知字段、重复 YAML 键、引用关系
和适配器支持的能力。启用的 binding 必须能读取其引用的环境凭据；禁用的 binding
仍需引用有效服务，但不要求环境密钥，也不会创建客户端。
首次配置允许暂缺 LLM 和渠道。LLM 一旦绑定就不能设置 `enabled: false`，配置接口也不允许删除已配置模型或移除最后一个渠道。语音、embedding 和余额可以禁用；禁用 embedding 仅停止语义检索，关键词召回与记忆写入保留。

## 内置适配器

| 适配器 | 能力 | 用途 |
| --- | --- | --- |
| `anthropic` | `llm` | Anthropic Messages 协议 |
| `openai` | `llm` | OpenAI Chat Completions 协议 |
| `openai` | `embedding` | OpenAI 兼容的向量接口 |
| `tencent` | `asr` | 腾讯 SentenceRecognition |
| `fish` | `tts` | Fish Audio 语音合成 |
| `deepseek` | `balance` | 查询账户余额，与本地 token 统计独立 |

### LLM

模型协议只有 `openai` 和 `anthropic`。`model`、`base_url` 必填；连接 DeepSeek 时
选择 `openai` 并填写 `https://api.deepseek.com`。DeepSeek 不是单独的模型协议。
无需鉴权的本地接口可以省略 `api_key`。默认参数：`max_tokens: 16384`、
`temperature: 0.6`、`timeout_seconds: 300`、`max_retries: 3`、`tool_choice: true`。
`thinking.effort` 和 `thinking.stages` 的值支持 `low`、`high`、`max`，阶段设置优先。
接口不支持强制工具调用时设置 `tool_choice: false`。协议由服务的 adapter 决定。

### Embedding

只配置 `endpoint`，填写完整请求 URL，例如 `https://api.example.com/v1/embeddings`，
也支持网关自定义路径。程序不自动追加路径。默认值为
`http://embedding:8002/v1/embeddings`。该字段放在服务 `settings` 或 binding `options` 中。
Embedding 不再接受 `base_url`；已有配置需改为完整的 `endpoint`。

| 参数 | 默认值 |
| --- | --- |
| `model` | `BAAI/bge-small-zh-v1.5` |
| `dimensions` | `512` |
| `calibration_profile` | `bge-small-zh-v1.5-momoi-v1` |
| `query_timeout_seconds` | `5` |
| `document_timeout_seconds` | `30` |

查询与文档编码分别设置超时，不再接受总超时 `timeout_seconds`。
索引批次固定使用内部默认值 8；已有配置需移除 `document_batch_size` 和 `timeout_seconds`。
`calibration_profile` 是高级配置项，用来选择已实现的评分校准规则，必须与模型匹配。
当前内置规则为 `bge-small-zh-v1.5-momoi-v1`；填写新名称不会自动生成阈值。
校准逻辑和向量空间一致性检查仍保留；切换模型并不表示自动完成了针对新模型的评分校准。
模型、维度必须与编码器匹配。使用 `momoi embedding` 命令前需要启用 binding。
查询失败仍会退回关键词召回，并保留查询熔断机制。

### ASR

腾讯凭据要求 `secret_id` 和 `secret_key`。默认参数：`region: ""`、`engine: 16k_zh`、
`timeout_seconds: 30`、`max_audio_bytes: 3145728`。音频大小限制由入站渠道执行，
不会作为请求参数发给腾讯。微信渠道自带的转写独立于这项可选的 NapCat ASR 能力。

### TTS

Fish 需要 `api_key` 和 `reference_id`。默认参数：`model: s2.1-pro-free`、
`base_url: https://api.fish.audio`、`format: mp3`、`latency: normal`、
`timeout_seconds: 60`、`max_audio_bytes: 20971520`。
模型支持 `s1`、`s2-pro`、`s2.1-pro`、`s2.1-pro-free`；格式支持 `mp3`、`wav`、`opus`；
latency 支持 `normal`、`balanced`、`low`。

首次请求失败后重试三次，间隔 1、2、4 秒，每次失败记录有长度限制且脱敏的错误详情。
`send_voice` 等待合成完成，失败返回 tool error 并建议 `send_bubbles` 降级文字；
成功结果与 `send_bubbles` 一致。投递细节见[语音合成](./CONFIG.zh-CN.md#fish-audio-语音合成)。

### 账户余额与 token 统计

DeepSeek 余额需要 `api_key`，默认 `base_url: https://api.deepseek.com`、
`timeout_seconds: 10`。后台通过 balance 能力查询余额；API 失败时显示不可用，
不影响概览中的其他数据。

Token 数量独立记录。模型高级参数 `accounting` 默认 `none`，只记录通用用量；
选择 `deepseek` 才启用 DeepSeek 用量解析和官方价格估算，与接口协议分开配置。
仅配置 DeepSeek 的余额服务不会给其他 LLM 套用 DeepSeek 价格。
旧的 `adapter: deepseek` 模型绑定需改为 `adapter: openai` 并显式填写地址；需要保留
专属统计时增加 `accounting: deepseek`。如果原服务同时用于余额，拆成两个 service，凭据仍可共享。
禁用 balance 不影响 token 记录或 LLM 费用估算。

## 扩展代码架构

`integrations/contracts` 定义 LLM、ASR、TTS、embedding、balance 能力接口，
`integrations/adapters` 实现厂商协议。`ServiceRegistry` 负责组装服务，业务代码只依赖
能力接口，不导入具体 API 客户端。HTTP 连接池、错误分类和重试基础设施与注册表同层；
LLM 协议特有的消息回放、工具编码、遥测和重试保留在 `llm`。

工厂收到参数字典和 `AdapterContext`（HTTP transport、dump 目录、语义策略），不接收
Momoi 主配置。构造时不应打开网络资源。在进入注册表的 async scope 前获取所需能力；
注册表按需创建实例，进入其异步上下文或注册 `close()`，退出时关闭所拥有的资源。
外部注入的测试服务由调用方管理。LLM 协议 session 和 embedding 的 HTTPX pool 也由
注册表统一管理生命周期。

安装 Python 模块并将模块名加入 `plugins`。以下 `my_balance.py` 是可用于本地测试的
固定余额适配器：

```python
from momoi.integrations.registry import Adapter, register_adapter

class FixedBalance:
    def __init__(self, options, context):
        self.amount = options["amount"]

    async def balance(self):
        return {"source": "live", "currency": "CNY",
                "is_available": True, "total_balance": self.amount}

def validate(options):
    if set(options) != {"amount"} or not isinstance(options["amount"], str):
        raise ValueError("amount must be a string")

register_adapter(Adapter(
    name="fixed", capability="balance", factory=FixedBalance, validate=validate,
    schema={"amount": {"type": "string", "default": "12.34"}},
))
```

添加 `plugins: [my_balance]`，定义 `adapter: fixed`、`settings: {amount: "12.34"}`
的 service，并将 balance binding 指向它。每项能力必须注册工厂、离线校验函数和字段 schema；
凭据字段使用 `secret: True`，provider 配置 API 返回已保存的密钥原值。旧位置参数注册签名已移除。
同一厂商新增其他 API 时，继续注册对应能力，无需修改主配置字段、业务消费者或后台。

LLM 实现需提供契约中的 `accounting`、`usage_sink`、`thinking_sink`、
`usage_parser` 和 `complete()`；不要求暴露厂商的 `config`。应用层统一通过
`require_tool` / `required_tool` 表达工具调用要求，由 adapter 转换为厂商协议。
需要响应的 Owner 轮次也采用这一契约，包括 Anthropic。

Embedding 的 `encode()` 返回归一化向量，并实现 `health()`、`close()`。
实例必须提供 `space: EmbeddingSpaceConfig`，描述启用状态、模型标识、维度、
校准配置和文档批次大小。adapter 自行将自定义选项转换为这些标准信息；例如将
`deployment` 转成 `space.model`，将嵌套 `connection.address` 用作自己的连接地址。
注册表和语义检索不会假设配置一定含有 `model`、`dimensions` 或 HTTP 地址。
读取 `ServiceRegistry.embedding_config` 会按需获取 encoder 的 `space`；禁用时
返回禁用的语义空间且不创建 encoder。

ASR 实例提供正整数 `max_audio_bytes`，供入站渠道执行限制；继承 `ASRProvider`
时默认 3 MiB。厂商自定义参数如何转换为这一限制由 adapter 决定。
TTS 失败抛出 `TTSError`，余额适配器抛出带脱敏详情及分类的 `IntegrationError`；
取消操作必须向上传播。

## 字段声明与前端接入契约

后端 `GET /api/settings/configuration` 的 `adapters[]` 返回
`{adapter, capability, fields}`。`fields` 是按声明顺序排列的字段映射，插件自行定义，
不需要编辑内置 schema。后端已支持以下完整契约；当前页面尚未支持全部嵌套表单和
布局元数据，递归字段渲染待接入。

| 元数据 | 后端语义 / 前端用途 |
| --- | --- |
| `type` | `string`、`integer`、`number`、`boolean`、`object`、`array` |
| `label`、`description` | 字段名称和帮助说明 |
| `required` | 启用时必须提供；字符串不能仅含空白，布尔值 `false` 合法 |
| `default` | 字段缺省时补入运行配置；显式值优先，`null` 不表示缺省 |
| `secret` | 仅字符串；标记凭据及环境引用，支持嵌套及数组元素；配置 API 返回原值 |
| `enum` | 标量可选值；前端必须保留原始类型，不能一律提交字符串 |
| `minimum`、`maximum` | 数字闭区间；整数拒绝布尔值和小数，数字拒绝 NaN / Infinity |
| `properties` | 对象的子字段映射，递归应用同一契约；省略时为自由 JSON 对象 |
| `items` | 数组元素的字段声明，可继续嵌套对象或数组 |
| `advanced` | 可折叠的高级配置标记，缺省为普通字段 |

注册时校验字段声明及默认值，拒绝未知元数据。秘密值不得出现在公开的 default 或 enum 中。
读取配置、Dashboard 保存和工厂实例化使用同一套字段规则，未知配置字段会报错。
校验顺序为：合并服务与 binding 参数、解析凭据、补默认值并校验字段，最后调用
adapter 的 `validate(options)` 做跨字段或业务约束校验。工厂收到最终解析后的字典。
禁用 binding 可缺必填项及环境密钥，但已经填写的值仍需符合字段类型和范围。

例如，一个厂商可以声明完全不同的认证方式：

```python
schema = {
    "tenant": {"type": "string", "label": "租户", "required": True},
    "region": {"type": "string", "enum": ["east", "west"], "default": "east"},
    "auth": {"type": "object", "required": True, "properties": {
        "client": {"type": "string", "label": "客户端密钥", "secret": True,
                   "required": True},
        "scope": {"type": "string", "default": "read"},
    }},
    "retries": {"type": "integer", "default": 2, "minimum": 0,
                "maximum": 5, "advanced": True},
}
```

用 `Adapter(..., schema=schema)` 注册即可暴露这份声明。
命名凭据表保存顶层字符串密钥；嵌套密钥保留在 settings/options 的原路径，
配置 API 均返回原值。`auth.client: {env: CUSTOM_PROVIDER_AUTH}` 同样支持环境引用。
缺省值只在解析后的运行配置中补齐，不会将环境值或默认值自动写回 YAML。
前端应根据 schema 显示缺省值，并保留未填写与显式填写的区别。
已保存密钥直接返回字符串，前端回传原值即可保留，填写新值即可替换。
Provider 配置不接受 `{"$secret":"keep"}` 占位符。环境变量字段返回 `{env: NAME}`
引用本身，运行时才解析环境值。

新增同类 provider 只需注册 adapter、实现标准能力接口和厂商选项转换。
配置热加载仍由 supervisor 串行关闭旧实例、创建新实例；业务消费者不参与转换。
