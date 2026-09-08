# 从空工作区配置 Momoi

运行 `momoi run` 即可启动 dashboard。Provider 注册接口已改为完整的 `Adapter` 定义，旧位置参数签名不再支持。纯后台部署使用 `momoi run --no-dashboard`。

## 首次使用

```bash
momoi --workspace ~/.momoi run
```

工作区没有 `config.json` 时，自动生成最小主配置、空 `providers.yaml`、人格提示词和心跳提示词。已有文件不会被覆盖，配置文件默认权限为 `0600`。语言模型、渠道及自动任务默认关闭。

打开 `http://服务器地址:8788`，输入首次启动输出的访问口令。已有工作区使用 `dashboard.token` 或环境变量 `MOMOI_DASHBOARD_TOKEN`。部署平台可预先设置该环境变量，后续配置无需登录服务器。

1. 在“CONFIG // MODEL”选择语言模型协议，填写端点、模型和密钥并保存。模型没有停用开关。
2. 在“CONFIG // CHANNEL”选择微信或 QQ 作为主渠道并保存，也可以添加其他渠道。主渠道不可停用，QQ 需要提供已运行的 NapCat WebSocket 地址和主人 QQ。
3. 微信点击“微信扫码登录”，在页面扫码；需要验证码时直接在页面填写。登录状态保存到工作区，并自动重新应用配置。
4. 在“CONFIG // VOICE”“CONFIG // MEMORY”“CONFIG // BALANCE”分别配置语音、记忆和余额。每个区块独立保存。

语音合成开关控制 TTS；停用后不创建合成 provider，也不再提供 `send_voice` 工具。收到的语音由 NapCat 或微信转写，不受合成开关影响。记忆开关仅控制 embedding 向量编码和语义检索，关键词召回与记忆写入保持可用。余额开关停止远程余额查询，不影响本地用量统计。关闭开关后点击“保存并应用”生效，原有选项和凭据保留。

提示词编辑位于模型区块。五个区块分别使用独立的 `CONFIG // CHANNEL / MODEL / VOICE / MEMORY / BALANCE` 副标题，输入框、下拉框和复选框共用粗描边、圆角、粉蓝阴影与键盘焦点样式。

页面显示缺失条件、配置错误、已保存与已应用版本。“业务已启动”表示运行实例的初始化完成，并不保证第三方 API 密钥有效或远程渠道已经连接。外部请求仍可能报认证、连接或配额错误。

## 模块职责

| 模块 | 职责 |
| --- | --- |
| `integrations/contracts` | LLM、TTS、embedding、balance 的业务能力接口 |
| `integrations/registry.py` | 注册完整 `Adapter`；创建服务，管理实例和资源生命周期 |
| `integrations/builtins.py`、`schema.py` | 内置工厂、校验入口和表单字段元数据 |
| `integrations/fields.py` | 递归字段契约、默认值、类型及范围校验 |
| `integrations/configuration.py` | 解析服务、凭据、能力绑定，解析显式环境引用并校验选项 |
| `config/loading.py` | 将主配置文档转换为运行配置，不要求功能已启用 |
| `config/workspace.py` | 初始文件生成与原子文件替换 |
| `config/manager.py` | 配置快照、版本冲突检测、校验及持久化；provider 字段返回原值 |
| `runtime/supervisor.py` | 独立管理业务运行实例，判断启动条件，应用配置与恢复 |
| `dashboard/configuration.py` | 配置 HTTP 接口与微信登录会话 |
| `web/src/ConfigurationSettings.jsx` | schema 驱动的 provider 表单、功能开关和运行状态 |

Dashboard 由 CLI 直接启动，持有独立的存储连接。Daemon 不再导入或启动 dashboard。新旧业务实例串行切换，旧实例的服务关闭后才创建新实例，避免同一任务混用两个 provider 的配置。

## 保存、覆盖和生效规则

每次读取返回 `revision`，保存必须携带该版本。多个浏览器或外部编辑器修改文件后，旧版本保存返回 HTTP 409，要求重新加载。保存入口先校验完整候选配置，再通过同目录临时文件、`fsync`、`os.replace` 写入。一次请求只替换一份文档。

Provider 选项优先级为：服务 `settings` → 服务端点和超时 → binding `options`；凭据另行合入，禁止与选项重复。主配置的显式环境覆盖优先于文件；页面显示当前存在的 `MOMOI_*` 环境变量名称，不返回它们的值。修改进程环境变量需要重新启动进程。

Provider 配置 API 直接返回已保存的密钥原值，包括命名 credentials、嵌套对象和数组中的密钥。回传原值表示保留，填写新值表示替换；删除字段或清空值表示清除，但仍需满足启用时的必填约束。Provider 配置不接受 `{"$secret":"keep"}` 占位符。`{env: NAME}` 保持为引用，不将环境中的密钥写入磁盘或替换为 API 返回值。插件继续用 `secret: True` 标记凭据字段。

能力表单采用独立配置写入：首次编辑共享服务中的能力时，创建专属于该能力的 `configured_*` 服务及凭据。其他绑定不变，后续编辑复用该专属服务。原服务保留，便于其他绑定继续引用；不会自动删除用户定义的服务。完整 YAML 接口仍支持显式共享。

| 修改内容 | 生效方式 |
| --- | --- |
| Provider、渠道、自动任务及运行参数 | 保存后自动应用；直接编辑主配置或 provider 文件也会被检测，检测周期约一秒 |
| 提示词内容 | 下一个 Turn 读取新内容 |
| MCP 文件、工作流、执行器文件 | 页面点击“重新应用” |
| 数据库存储、时区、dashboard 认证、提示词文件路径、provider 文件路径 | 重启进程；这些字段不提供页面热修改 |
| 新的 Python provider 插件或插件代码 | 本地安装、配置并重启；页面不提供任意模块导入操作 |

在纯后台模式下，没有配置监视器，修改配置后需重启。

## 应用失败与任务边界

配置校验失败时不修改文件；外部编辑产生无效配置时，现有实例继续运行，dashboard 显示错误。有效配置应用时先停止旧实例，再启动新实例；新实例初始化失败则尝试重新启动上一份可运行配置，并保留错误状态和旧的已应用版本。恢复也失败时，dashboard 继续提供状态与配置入口。

这是受控重启，不是无中断切换：应用配置会取消当前业务任务，并按既有持久化恢复规则处理未完成工作。不要把“保存成功”理解成“远程请求成功”或“正在执行的外部操作已撤回”。首次未配置模型或渠道时处于等待配置状态；配置接口拒绝停用模型、删除已配置的模型或移除最后一个渠道。

微信重新登录期间暂停业务实例，避免登录会话与运行渠道同时写入账号状态；登录完成、失败或取消后重新应用。验证码通过异步队列传给现有微信协议实现，登录会话最长八分钟，dashboard 关闭时取消会话。

## 接口与扩展

以下接口使用现有 dashboard JWT 认证，响应禁止缓存：

| 方法与路径 | 用途 |
| --- | --- |
| `GET /api/settings/configuration` | 返回 provider 原值文档、adapter 字段定义、能力配置和版本；应用配置保持现有脱敏规则 |
| `PUT /api/settings/providers/{capability}` | 保存单项能力，正文为 `{revision, document: {adapter, enabled, options}}` |
| `PUT /api/settings/providers` | 原子保存多项能力，正文为 `{revision, document: {tts: {...}, embedding: {...}}}` |
| `PATCH /api/settings/configuration/app` | 合并指定运行配置区块，保留其他区块，正文为 `{revision, document}` |
| `PUT /api/settings/configuration/{app\|providers}` | 替换可编辑运行配置或 provider 文档，正文为 `{revision, document}` |
| `GET /api/settings/runtime` | 返回运行状态、缺失条件、保存/应用版本和微信登录状态 |
| `POST /api/settings/apply` | 异步请求重新应用，返回 202 |
| `POST /api/settings/channels/weixin/login` | 创建登录会话，返回 202 |
| `POST /api/settings/channels/weixin/verify` | 提交 `{code}` |
| `DELETE /api/settings/channels/weixin/login` | 取消登录 |

新增 adapter 时注册 `Adapter(name, capability, factory, validate, schema)`。后端 schema 支持标量、递归对象和数组，以及必填、默认值、枚举、范围、密钥、说明和高级参数元数据。统一字段校验之后，再调用 adapter 的离线 validator 做业务约束检查。工厂不应在构造期间打开网络资源；异步资源由 `ServiceRegistry` 统一进入和关闭。当前页面尚未接入完整递归表单和布局元数据。字段定义和能力接口见 [Provider 文档](PROVIDERS.zh-CN.md#字段声明与前端接入契约)。

## 验证

```bash
uv run python -m unittest tests.test_dashboard_configuration -q
uv run python -m unittest discover -q
npm run build
```

专门覆盖空工作区、provider 密钥原值返回与保留、共享服务隔离、环境引用、原子写入失败、版本冲突、未认证访问、运行实例切换、无效配置保留、初始化失败恢复和页面验证码流程。真实第三方服务的网络连通性及账号登录需要部署者提供有效凭据验证。
