# Momoi 重构实施文档

## 实施原则

本实施不改变默认配置、提示词内容、模型请求、上下文顺序、重试判定、数据库 schema 和回复效果。每个阶段独立可合并、可回滚；先建立等价测试，再移动代码，最后删除旧路径。

## 交付顺序

### P0：行为基线

新增测试而不改生产逻辑：

- `tests/test_runtime_policy.py`：固化 daemon 4~7 秒/4~60 字符、工具失败 3 次、goal/reminder 8 条等当前默认值；区分产品策略与领域不变量。
- `tests/test_provider_replay.py`：以 fixture 覆盖 OpenAI/Anthropic 成功、429/5xx、不可重试 4xx、无效 JSON、空 choices/content、tool_choice 和 usage。
- `tests/test_budget_compatibility.py`：覆盖空文本、预算 0/1、刚好命中边界、Unicode、marker、整数舍入和工具结果 JSON 保留字段。
- `tests/test_autonomous_queue.py`：覆盖 heartbeat/reflection/goal 优先级、公平轮转、stop、取消、重试及旧字符串 goal id。
- 记录相同输入下的 outbox、memory/agenda mutation、usage、关键日志事件名称与 stage，作为 golden snapshot。

完成门：只增加测试时全量测试通过，snapshot 经人工确认是现有行为而非期望行为。

### P1：集中策略定义

新增 `src/momoi/policies.py`：

```python
@dataclass(frozen=True)
class DaemonPolicy:
    message_gap_min_seconds: float = 4.0
    message_gap_max_seconds: float = 7.0
    message_gap_min_chars: int = 4
    message_gap_saturation_chars: int = 60

@dataclass(frozen=True)
class ContextPolicy:
    max_visible_goals: int = 8
    max_visible_reminders: int = 8

@dataclass(frozen=True)
class MemoryPolicy:
    recent_min_ttl_hours: float = 1
    recent_max_ttl_hours: float = 168
    lexical_overlap_floor: float = 0.1
```

planner schema 长度、允许枚举、工具失败熔断等进入 `DomainInvariant` 或保留为模块常量，不暴露为用户配置。第一步仅依赖注入默认对象；是否开放 JSON 配置另开变更，避免默认序列化和旧配置兼容风险。

迁移文件：`runtime/daemon.py`、`runtime/turns.py`、`storage/memory.py`。完成门：P0 策略 snapshot 完全相同。

### P2：预算与裁剪适配器

新增 `src/momoi/runtime/budget.py`：

- `TextSizer.estimate(text)` 封装当前字符近似算法。
- `MemoryTextFitter` 原样承接 `truncate_tokens`/`excerpt_tokens`。
- `ToolResultFitter` 原样承接 `_truncate_tool_result_json`，保留 `ok/error/message/provenance`。
- `SectionBudgetAllocator` 只封装现有分配顺序，不改变任何比例或 `max/min`。

旧函数先变成转发门面，所有调用迁移后再删除。`context_assembler._selected_by_unit`、`_search_or` 和行级 token 分配只做依赖替换，不在此阶段调整算法。

完成门：P0 budget 参数化测试逐字节相等；上下文 section 顺序及内容 snapshot 相等。

### P3：Provider 去重

在 `provider.py` 或新 `provider_support.py` 抽取纯基础设施：

- 通用 retry 循环骨架接收协议专属 `should_retry` 回调。
- 共享 `_log_retry`、`_log_failure`、dump 和 usage 记录。
- OpenAI/Anthropic 继续拥有 payload、URL、响应解析、无效响应判定和 tool_choice 映射。

不在本阶段引入统一 codec 中间模型。只有 P0 fixture 能证明两协议的调用次数、delay、异常类型、日志字段和 `ProviderResponse` 一致时，才迁移对应分支。

完成门：录制回放完全一致；网络请求次数与 sleep 序列一致。

### P4：拆分 TurnRunner

按副作用边界迁移，不一次性重写：

1. `PromptRenderer`：承接 `_live_prompt`、`_sections` 和 prompt 读取；保留热重载、fallback、optional 语义及 `prompt_reload_failed` 日志。
2. `ContextService`：承接候选收集、planner 调用、预算和组装；保留 trace stage、候选排序与降级计划。
3. `ToolExecutionService`：承接工具授权、调用、连续失败计数和结果适配；`TurnDraft` 仍由调用方持有。
4. `TurnCommitter`：承接 draft 的 memory/agenda/outbox 提交；事务边界与提交顺序不变。
5. `TurnOrchestrator`：统一 owner、webhook、heartbeat、reflection 的公共模板；各模式差异通过显式 `TurnMode`/策略表达。

`TurnRunner` 在迁移期只做门面和依赖装配，daemon 的公开调用不变。每移动一个方法就运行对应单测和 golden snapshot，禁止顺手改变提示词或日志文案。

完成门：`turns.py` 只保留编排/兼容代码；四类 turn 的输入、provider 请求和提交结果均等价。

### P5：类型化自主任务与跨模块契约

新增 `AutonomousJob(kind, id, priority)`，在入队边界把旧字符串转换为对象；`_prioritize_autonomous` 只比较显式字段。先允许 `Queue[str | AutonomousJob]`，所有生产者迁移后再收紧为 `Queue[AutonomousJob]`。

随后只为频繁跨模块的裸 dict 建模：`ContextPlan`、`EpisodeBinding`、`GoalMutation`、`ToolResult`。provider 原始 JSON 和 workflow 任意 payload 保持 dict，避免过度抽象。

完成门：队列公平性、stop/取消/重试测试全绿；类型检查不再需要跨层字段猜测；旧持久化数据仍可读取。

## 提交与回滚策略

建议每个 P 阶段至少一个独立提交，P4 按四个服务再拆提交。旧门面必须保留到下一阶段稳定；新旧实现不同时运行写副作用。任何 snapshot 差异都先视为回归，只有产品明确批准后才能更新基线。

## 验证命令

```bash
uv sync --extra dev
uv run pytest -q
uv run pytest tests/test_provider_replay.py tests/test_budget_compatibility.py -q
uv run pytest tests/test_daemon.py tests/test_messaging.py tests/test_context_assembler.py -q
```

当前仓库 `.venv` 未安装 pytest，系统 Python 也无 pytest；执行实施前应通过项目依赖工具安装测试环境。验证还应包含同一 fixture 的 provider 请求 dump diff、SQLite 数据库升级前后读取，以及至少一次多 channel 并发 soak test。

## 完成定义

- 所有现有与新增测试通过，无 golden snapshot 未解释差异。
- 默认配置与旧配置均可启动，数据库无需破坏性迁移。
- 四类 turn、两个 provider、三类自主任务均有等价证据。
- 重复的 retry/usage/dump、预算入口、turn 提交路径已实质收敛。
- 启发式参数被归类为产品策略或领域不变量，不再散落；默认 Momoi 行为和效果保持不变。
