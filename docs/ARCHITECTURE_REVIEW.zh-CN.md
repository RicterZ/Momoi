# Momoi 架构审阅与重构方案

## 目标与不变量

本方案只重构结构，不改变 Momoi 的外部效果：消息接收、上下文选择、模型工具调用、记忆/议程写入、回复投递、重试和日志语义必须保持一致。任何阶段都应以现有测试、录制的 provider 响应和端到端样例作为行为基线。

## 当前架构概览

运行时由 `runtime.daemon.MomoiDaemon` 驱动多个异步 worker；`runtime.turns.TurnRunner` 同时负责批量消息整理、上下文规划、提示词组装、provider 循环、工具授权、`TurnDraft` 累积和提交。`context_assembler`、`context_candidates` 和 `context_planner` 是相对清晰的上下文子系统。`storage.Store` 聚合 SQLite、记忆和投递状态；`provider.py` 通过两个协议实现模型适配；channel/webhook/dashboard 作为外围适配器接入。

## 保留的设计优点

- `TurnDraft` 将模型副作用延迟到 turn 结束提交，便于失败恢复和测试。
- 上下文候选、规划、组装已经拆成独立模块，且有专门测试；应保留“候选→计划→组装”的边界。
- `Store` 对事件、outbox、记忆和 usage 提供持久化边界，daemon 不直接操作 SQL。
- provider 统一返回 `ProviderResponse`/`ToolCall`，日志带 trace/turn 上下文，适合继续抽象。
- 配置使用不可变 dataclass 并集中校验，适合作为策略注入入口。

## 主要问题（按风险排序）

### 1. TurnRunner 是高耦合上帝对象

`runtime/turns.py` 同时包含提示词文件加载与 XML section 渲染（约 1-220 行）、上下文预算计算（约 560-930 行）、工具循环与失败熔断（约 1400-1900 行）、副作用草稿提交和回复构造。新增一个 turn 阶段需要同时理解协议、存储和渠道约束，重复的 `estimate_tokens`、`max(...)` 和状态分支容易产生不一致。

**重构方向：**引入 `TurnOrchestrator`（流程）、`ContextService`（候选/预算/组装）、`ToolExecutionService`（授权/调用/结果裁剪）和 `TurnCommitter`（草稿持久化/出站消息）四个协作对象；保留 `TurnRunner` 作为兼容门面，方法签名和调用顺序不变。heartbeat、reflection、owner、webhook 的重复上下文/提交路径应在此层收敛。

### 2. Provider 协议实现重复

`provider.py` 的 OpenAI 和 Anthropic 实现各自复制请求 dump、usage 记录、重试退避、HTTP 错误和工具响应校验逻辑（OpenAI 约 350-520 行，Anthropic 约 610-830 行）。两套实现的差异主要是 URL、请求/响应映射和协议特有错误字段。

**重构方向：**第一阶段只抽取共享 retry/log/usage/dump 基础函数；OpenAI/Anthropic 保留各自请求映射、tool_choice、无效 JSON 和错误重试判断。待逐响应回放证明等价后，再考虑薄的 transport 层，避免统一接口改变协议语义。

### 3. 启发式策略散落且不可配置

`daemon.py` 将消息合并间隔固定为 4~7 秒、4~60 字符（34-51 行）；`turns.py` 将连续工具失败固定为 3 次、目标/提醒上下文固定截取 8 条（约 629-652 行）；`memory.py` 用 1 小时/7 天 TTL、字符近似 token 和 0.1 词元重叠阈值；`context_planner.py` 大量 12/20/200 等校验上限。它们是有效的产品策略，但目前无法区分“领域不变量”和“可调策略”。

**重构方向：**新增按域拆分的不可变策略（`DaemonPolicy`、`ContextPolicy`、`MemoryPolicy`、`ProviderPolicy`）。可调的产品策略（间隔、候选数、TTL）与不可调的 `DomainInvariant` 分离；context planner 的 12/20/200 等 schema 安全上限、工具失败熔断和协议字段约束默认保持代码级不变量。配置解析负责默认值、优先级和校验，业务代码只依赖策略对象。

### 4. 上下文预算职责重复

`storage.memory` 提供 `estimate_tokens`/`truncate_tokens`/`excerpt_tokens`，而 `turns.py`、`context_assembler.py` 又分别进行行级 token 累加、剩余预算分配和工具结果 JSON 裁剪。不同裁剪器的 marker、最小预算和优先级不统一。

**重构方向：**建立 `TextSizer` 与带兼容模式的 `BudgetAllocator`。记忆 `truncate/excerpt` 和工具结果 JSON 裁剪保留各自 marker、最小预算、舍入及 JSON 保留字段；先用等价适配器统一入口，再逐步减少重复，禁止一次性改成同一个 `fit` 语义。

### 5. 队列控制协议使用字符串哨兵

daemon 用 `__momoi_heartbeat__`、`__momoi_reflection__:` 等字符串区分自主任务，并在多个地方 `startswith`/分支解析。这使合法 goal id 与控制消息的边界依赖字符串约定。

**重构方向：**引入 `AutonomousJob` 联合类型（heartbeat/reflection/goal），显式表达 id/kind/优先级；迁移覆盖现有 `asyncio.Queue[str]`、`_prioritize_autonomous`、stop/取消路径和旧字符串 goal id。兼容层只在入队边界接受旧字符串，并用回放测试验证公平性。

### 6. 领域状态仍以裸 dict 为主

`TurnDraft.goals/reminders`、planner action、workflow variables 和 provider payload 大量使用 `dict[str, Any]`。这放大了字段名重复和运行时校验成本。

**重构方向：**优先为跨模块契约建立小型 frozen dataclass（`ContextPlan`、`EpisodeBinding`、`GoalMutation`、`ToolResult`），边界处统一转换；不为纯 JSON provider payload 过度建模。

## 分阶段方案

1. **冻结行为基线：**补齐默认策略快照、provider fixture、消息间隔/工具失败/预算裁剪测试；记录关键日志事件和 outbox 状态序列。
2. **提取无副作用组件：**先抽 `TextSizer`/`BudgetAllocator`、provider transport、prompt renderer。通过旧门面调用，提交保持不变。
3. **拆分 turn 编排：**把上下文、工具执行、提交迁出 `TurnRunner`，保留兼容门面和原有 trace/stage 命名。
4. **策略注入：**把 daemon、memory、planner 的常量映射到 `RuntimePolicy`，配置缺省值完全等价；逐项开放配置应另行评估，不在本次改变默认行为。
5. **类型化协议与哨兵迁移：**新增对象协议、兼容旧输入，完成后删除字符串分支和重复 dict 校验。
6. **删除兼容层前复盘：**以全量测试、录制回放和人工行为对照确认无差异后再清理旧实现。

## 风险与验收

- 最大风险是上下文排序、预算边界和 provider 重试次数改变。每次提取都必须有等价性测试，尤其是空文本、刚好命中预算、工具错误和取消任务。
- 验收要求：现有测试全绿；新增策略快照和 provider 回放全绿；覆盖并发队列公平性、取消/重试、非 200 与无效 JSON、配置缺失和数据库迁移兼容；相同输入/响应产生相同 outbox 文本、记忆/议程 mutation、usage 记录和关键日志字段；性能不低于当前实现。
- 本文只定义重构边界和顺序；具体文件拆分、接口字段、迁移开关和测试矩阵见实施文档。

## 独立复盘记录

本方案已由一个 subagent 结合当前代码独立复盘一次。复盘确认主要事实，并促成以下修订：把 schema 安全上限与可调策略分离；provider 改为先抽基础设施而非强制 codec 统一；预算组件保留 memory/JSON 的不同裁剪语义；自主任务迁移补齐优先级、取消和旧字符串兼容；验收增加并发、无效响应、配置缺失及数据库兼容场景。以上意见均已纳入本文及实施文档。
