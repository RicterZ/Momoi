# Momoi Episode 记忆架构设计

## 1. 目标

Momoi 的目标不是把尽可能多的历史对话放进每次模型请求，而是：

1. 平时只提供当前回复真正需要的上下文。
2. 需要回忆时，能够可靠找到很久以前的 Episode。
3. 需要核对细节时，能够继续读取原始消息。
4. 长期事实和偏好继续由 `memories` 承担，不给已确认记忆增加默认时间窗。
5. 最近几轮原文继续由 `recent_conversation` 承担，不改变其预算和行为。
6. Episode 只表示一次活动或一条话题线，不代替 Goal、Memory 或 Reflection。
7. 删除 harness 中代替 LLM 判断对话语义的规则，保留协议校验、权限校验和资源上限。

这里所说的“过期”不是删除历史。Episode 原始消息仍然永久保存在数据库中并可搜索。
过期只表示它不再自动出现在普通 Owner Turn 的上下文候选中。

## 2. 非目标

本设计不做以下事情：

- 不创建第二套记忆数据库。
- 不把 Episode 转换成长时间有效的事实记忆。
- 不把 Reflection 当作 Episode 摘要。
- 不给 `memories` 增加 30 天或其他默认搜索窗口。
- 不改变 `recent_conversation` 的 `recent_turns` 和 `recent_raw_tokens` 语义。
- 不依靠大量针对具体话题、词语或 speech act 的 Python 条件分支。
- 不在每轮自动读取旧 Episode 的完整原文。
- 不因为 Episode 被标记为 `open`，就允许它永久自动进入上下文。

## 3. 当前对象的职责

### 3.1 memories

主人确认过的长期事实、偏好、关系和习惯。

- 默认永久可搜索。
- `always`、`recent`、`recall` 继续表示激活方式。
- Episode 归档不能自动升级为 Memory。
- 只有主人原话和现有 Memory 工具流程可以建立或修改确认记忆。

### 3.2 reflections

Momoi 的复盘结果和低权限学习。

- 可以帮助改善行为和检索。
- 不是对话话题目录。
- 不能用来替代 Episode 的总结或原始证据。

### 3.3 episodes

一次活动或一条话题线。

- 保存 title、status、summary、working summary、open loops 和关联 turns。
- 支持按话题搜索、时间搜索和原文读取。
- 默认自动检索有时间范围。
- 超出默认时间范围后仍可显式搜索。

### 3.4 recent_conversation

最近几轮已完成对话的原文。

- 独立注入。
- 保留当前约 `recent_turns=6`、`recent_raw_tokens=64000` 的生产行为。
- 不与旧 Episode 原文共享预算。
- 不因 Episode 设计调整而缩短。

### 3.5 goals 与 reminders

跨 Turn 继续执行的任务和未来提醒。

- Episode 的 `open_loops` 只是归档信息，不应承担可靠任务调度。
- 一个话题很旧但仍有有效 Goal 时，Goal 继续通过自己的上下文通道出现。
- 不需要为了保留 Goal 而让整个 Episode 永久自动出现。

## 4. 核心原则

### 4.1 目录与原文分离

自动检索 Episode 时，默认只向主模型提供：

- Episode ID
- 标题
- 状态
- 创建时间
- 最后活动时间
- 简短总结
- topics、entities
- open loops
- 与当前 intent unit 的对应关系

不自动提供：

- 完整 working summary 证据列表
- 搜索命中的原始消息
- `summarized_through_ordinal` 之后的原始消息
- Episode 的全部 turns

主模型看到目录后，再决定是否使用工具读取更多内容。

### 4.2 归档关系与读取权限分离

`episode_bindings` 只表示当前 turn 应该归档到哪些 Episode。

它不再表示：

- 这些 Episode 必须注入主模型上下文；
- 需要读取这些 Episode 的原始消息；
- `related` Episode 应当继续展开历史；
- 当前 turn 需要复述这些 Episode。

历史检索只由独立的 recall request 触发。

### 4.3 默认范围有限，显式范围无限

Episode 自动搜索默认只搜索最近 30 天。

30 天按 Episode 的最后实际活动时间计算，而不是按 summary 更新时间计算。

仅使用“最后活动时间”还不够。一个不断收到新 turn 的 Episode 可能永远处于最近 30 天，
例如所有家庭开关门事件都被持续挂到同一个 Episode。为避免这种 Episode 永久增长，
Episode 还必须有与最近活动时间无关的强制滚动上限，见“7.4 永久活跃 Episode 与强制滚动”。

以下操作可以突破默认范围：

- 主人明确提到更早的日期或时间范围；
- 主人说“以前”“很久前”“去年”“最早一次”等，且当前回复确实需要历史；
- 当前问题要求核对历史事实、原话、决策过程或时间线；
- 主模型在默认搜索结果中没有找到答案，并判断继续搜索是必要的；
- 工具调用显式指定 `from`、`to` 或 `lookback_days`；
- 工具调用显式指定全量历史。

这不是永久遗忘。超过 30 天的 Episode 只是退出默认自动候选。

### 4.4 先总结，后原文

历史读取采用逐级展开：

1. 最近对话原文。
2. 自动检索到的 Episode 目录。
3. 主模型调用 Episode 搜索，默认返回总结。
4. 主模型指定某个 Episode，读取相关消息或最近一页原文。
5. 只有确有需要时继续翻页、扩大时间范围或读取完整记录。

### 4.5 语义判断交给 LLM

LLM 判断：

- 当前是否需要历史；
- 当前是普通分享还是继续旧话题；
- 是否需要超过 30 天的搜索；
- 是否需要完整原文；
- 是否应建立新 Episode；
- 是否应继续已有 Episode；
- 哪些内容属于 open loop；
- 一个旧 Episode 是否混入了多个话题，需要拆分。

Harness 只负责：

- JSON/schema 校验；
- ID 和引用存在性校验；
- 权限和工具范围；
- 时间范围合法性；
- 分页游标；
- token、结果数和执行时间上限；
- 引用原文真实性；
- 数据库一致性。

## 5. Owner Turn 的目标流程

### 5.1 Context Planner 输入

Planner 获得：

- 当前主人消息；
- `recent_conversation`；
- 最近 30 天的分层 Episode 目录候选；
- 当前 Goals 和 Reminders 的简短候选；
- 必要的时间信息。

Planner 不获得旧 Episode 的完整原文。

候选 Episode 每条应包含：

```json
{
  "id": "episode-id",
  "status": "closed",
  "title": "回复拆分与连续消息风格反馈",
  "created_at": "2026-08-06T01:47:13+08:00",
  "last_activity_at": "2026-08-06T03:20:00+08:00",
  "summary": "主人指出回复拆分、过度描述和 www 使用方面的问题。",
  "topics": ["回复风格", "消息拆分"],
  "entities": ["Momoi"],
  "open_loops": [],
  "summary_quality": "grounded"
}
```

`summary_quality` 可以先由现有字段推导，不一定立即增加数据库列：

- `grounded`：有 `working_summary_claims`，语义总结由这些 claims 生成；
- `extractive`：只有已验证 claims，但还没有语义总结；
- `legacy`：summary 或 working summary 没有 claims；
- `empty`：没有可用总结。

Planner 必须知道质量状态，但主模型默认目录中不需要展示内部维护细节。

### 5.2 Context Planner 输出

建议将 Planner 协议升级为 v2，把“归档”和“召回”明确分开：

```json
{
  "version": 2,
  "intent_units": [
    {
      "id": "u1",
      "event_ids": ["event-id"],
      "text": "刚才给 Momoi 修改了提示词和代码，升级了",
      "intent": "分享刚完成的 Momoi 升级",
      "speech_act": "casual_share",
      "references": []
    }
  ],
  "recall_requests": [],
  "episode_bindings": [
    {
      "episode_ref": "new:momoi-upgrade-20260815",
      "title": "Momoi 提示词和代码升级",
      "relation": "primary",
      "unit_ids": ["u1"],
      "topics": ["Momoi", "提示词", "代码升级"],
      "entities": ["Momoi"],
      "open_loops": [],
      "salience": 0.5
    }
  ],
  "episode_links": [],
  "uncertainty": []
}
```

需要历史时，使用结构化 recall request：

```json
{
  "query": "Momoi 回复拆分 www 使用反馈",
  "unit_ids": ["u1"],
  "sources": ["episodes", "memories"],
  "time_range": {
    "mode": "recent",
    "lookback_days": 30
  }
}
```

显式长时间搜索：

```json
{
  "query": "最早一次讨论 Momoi 回复拆分",
  "unit_ids": ["u1"],
  "sources": ["episodes"],
  "time_range": {
    "mode": "all"
  }
}
```

Planner 不指定 `detail=full`。Planner 的职责是准备目录级上下文，不是提前决定向主模型灌入原文。

### 5.3 Runtime 检索

Runtime 根据 `recall_requests`：

- 搜 memories；
- 搜 reflections；
- 搜 Episode；
- 搜 goals、reminders、conflicts。

Episode 检索只产生目录项。

一个 Episode 同时被 binding 和 recall 命中时：

- 合并 unit IDs；
- 保留归档 relation；
- 不因此展开原文；
- 不把 relation 转换成原文读取模式。

### 5.4 主模型默认注入

Owner Turn 默认包含：

1. `current_owner_messages`
2. `context_resolution`
3. `runtime_directives`
4. `runtime_state`
5. `recent_conversation`
6. `episode_directory`
7. `owner_preferences`
8. `recent_memories`
9. `confirmed_owner_memory`
10. `reflection_memory`
11. `pending_memory_conflicts`
12. `active_goals`
13. `pending_reminders`
14. `cooled_reply_expectation`
15. `open_reconciliations`

原来的 `<recalled_episodes>` 改成 `<episode_directory>`，其中不包含 raw messages。

示例：

```text
<episode_directory>
- id=eb0b... status=closed
  last_activity=2026-08-06T03:20:00+08:00
  title=回复拆分与连续消息风格反馈
  summary=主人指出回复拆分、过度描述和 www 使用方面的问题。
  supports=u1
</episode_directory>
```

默认注入不使用“最多 3 个 Episode”这种低固定数量。Episode 数量由相关性覆盖和独立
token 预算共同决定，采用两层目录：

#### focused summaries

与当前 intent units 直接相关的 Episode：

- 提供 ID、时间、标题、状态、topics 和简短语义总结；
- 每个 intent unit 至少有机会获得一个结果，再为高相关 intent 增加更多结果；
- 初始建议允许 8 至 16 个详细目录项；
- 单条 summary 建议不超过 200 至 400 tokens。

#### compact index

在 focused summaries 之外继续提供更宽的检索视野：

- 只提供 ID、日期、标题、topics 和一句极短说明；
- 初始建议允许再提供 16 至 64 个目录项；
- 单条通常控制在 30 至 80 tokens；
- 让主模型看到可能相关的其他历史，但不把它们当成当前回复重点。

整体规则：

- 以独立的 `episode_directory_tokens` 为主要限制，而不是以 3 条为限制；
- 初始总预算建议 6000 至 10000 tokens，根据实际模型上下文窗口评估；
- 目录结果按 intent unit 轮询分配，避免一个查询占满全部预算；
- focused 和 compact 结果去重；
- 不从 `recent_raw_tokens` 中扣除；
- 不包含任何 `raw_tail`；
- 64 或更高的条目上限只作为请求体和数据库查询的安全上限，不是正常召回目标。

“超凡的记忆”需要较高的候选召回率，而不是默认读取较多原文。宽目录负责让模型看到
足够多的可能历史，按需读取工具负责取得精确细节。

### 5.5 主模型按需读取

如果目录已经足够回答，主模型不调用工具。

如果需要历史详情，主模型调用 Episode 搜索或读取工具。工具结果进入当前 tool loop，
而不是永久加入每轮上下文。

## 6. Episode 总结与归档

### 6.1 总结字段的目标语义

继续使用现有字段，不建立新的总结系统：

#### working_summary_claims

已验证的原文证据引用。

- 每条 claim 指向 message ID、turn ID、ordinal 和连续原文 quote。
- 是语义总结的证据基础。
- 可以被搜索索引使用。
- 不应默认完整注入主模型。

#### working_summary

由 claims 确定性渲染的证据清单。

- 便于调试、审计和迁移。
- 不是面向普通 Owner Turn 的最终总结。
- 可以保留当前 extractive 格式。

#### summary

面向检索和目录展示的简短语义总结。

- 只能基于 verified claims 生成。
- 应描述这个 Episode 发生了什么、有哪些决定、结果和未完成事项。
- 不保留寒暄、重复表达和过时过程细节。
- 不应复制大量原文。
- 应有严格长度限制。

### 6.2 两阶段后台维护

每次 Episode 维护分成两步：

#### 阶段 A：证据选择

沿用当前 episode annealing：

- 从尚未处理的 turns 中选择重要 claims；
- 复用仍有效的旧 claims；
- 所有 quote 必须在原始消息中逐字存在；
- Runtime 验证引用。

#### 阶段 B：语义总结

把以下内容交给总结模型：

- title
- status
- verified claims
- 当前 topics、entities、open loops

模型输出：

```json
{
  "version": 1,
  "summary": "主人调整了 Momoi 的回复拆分方式，并指出过度描述和 www 滥用问题；Momoi 已确认新的使用原则。",
  "topics": ["回复拆分", "表达风格", "www 使用"],
  "entities": ["Momoi"],
  "open_loops": []
}
```

Runtime 只允许总结引用 claims 已支持的内容。第一阶段已经保证证据存在，第二阶段不接触未验证 raw history。

### 6.3 维护触发位置

继续使用现有后台 episode annealing worker，避免在 Owner Turn 同步总结。

触发条件：

- Owner Turn commit 后发出维护请求；
- 主人空闲达到 `episode_annealing.idle_seconds`；
- 一次只处理一个 Episode；
- 主人新消息到达时取消；
- 失败进入退避重试。

不另设新的定时 daemon。

需要扩展当前候选范围：

- open
- closing
- closed 但 summary quality 为 `legacy` 或 `empty`
- closed 且仍有未总结 turns

closed 只表示话题结束，不表示不再维护总结。

### 6.4 何时归档

Episode 状态继续使用：

- `open`：当前主要话题；
- `closing`：近期可能继续，但已经切换到其他主要话题；
- `closed`：话题已结束或长期没有继续。

归档是状态变化和总结完成，不是移动或删除消息。

建议流程：

1. 新 primary Episode 出现时，旧 primary 从 open 进入 closing。
2. 后续 Turn 没有继续旧 Episode 时，旧 Episode 从 closing 进入 closed。
3. Reflection 可以关闭遗漏的话题，但不是唯一关闭机制。
4. closed 后仍执行必要的最终总结。
5. 完成总结后进入稳定归档状态。

### 6.5 混合 Episode 的修复

旧数据中可能存在一个 Episode 包含多个不相关话题，例如：

- 修改提示词；
- 掉头发；
- 台风航班；
- 鼠标垫；
- 门锁告警；
- 次日 heartbeat。

这种数据不能仅靠总结压缩，因为它本身的归档边界已经错误。

后台维护需要支持 Episode 重分组：

1. 读取 Episode 的 turn 目录和已有 context plan bindings。
2. 有 context plan 的 turns 优先按 unit binding 重建关系。
3. legacy turns 交给 LLM 按活动或话题线分组。
4. LLM 只输出 turn ID 到目标 Episode 的映射，不复制消息。
5. Runtime 校验所有 turn ID 都存在，且不会丢失。
6. 新建必要的 Episode，并更新 `episode_turns`。
7. 原始 `messages` 表不改、不复制。
8. 为新 Episode 分别生成 claims 和 summary。
9. 原 Episode 无 turns 后删除；仍有 turns 时保留。

自动触发只使用客观维护信号，例如：

- legacy import 标记；
- turn 数明显超过普通 Episode；
- summary 仍为 unverified；
- 多个 context plan 的 primary bindings 被历史迁移合并；
- title 与大部分已验证 claims 长期不一致。

是否拆分、如何拆分由 LLM 决定，Python 不根据“门锁”“天气”等关键词硬编码。

## 7. Episode 过期

### 7.1 两种时间概念

必须区分：

#### 数据保留时间

原始消息和 Episode 保存多久。

本设计默认永久保存，除非主人明确删除。

#### 自动检索有效期

Episode 是否进入普通 Owner Turn 的自动候选。

默认 30 天。

### 7.2 last_activity_at

过期判断应使用关联 turn 的最大实际活动时间：

```sql
MAX(turns.updated_at)
```

不能使用 `conversation_episodes.updated_at`，因为以下维护操作会更新它：

- 重新总结；
- 重建索引；
- 关闭 Episode；
- 修改 title/topics；
- 错误的 related binding。

维护时间不应让旧话题重新变成“最近话题”。

### 7.3 默认候选规则

普通 Planner 候选和自动 Episode 搜索默认：

```text
last_activity_at >= now - 30 days
```

不因以下条件自动突破：

- status=open；
- status=closing；
- salience 较高；
- summary 最近更新；
- Episode 被 Reflection 最近处理。

如果一个长期任务必须持续出现，应由 Goal 或 Reminder 表达，而不是依赖旧 Episode。

### 7.4 永久活跃 Episode 与强制滚动

**需要强制滚动。**

仅依赖 inactivity 或最近 30 天搜索窗口，会出现永不过期的 Episode：

- 周期性家庭事件持续写入同一 Episode；
- heartbeat 长期写入固定 Episode；
- 同一长期项目每周都有少量新消息；
- Planner 持续把只共享实体、但不是同一次活动的 turn 绑定到旧 Episode；
- 错误的 related/primary 绑定不断刷新 Episode 的最后活动时间。

这会造成三个问题：

1. Episode 越来越难以总结，旧事实、已结束事件和当前状态混在一起。
2. 搜索只能返回一个过大的 Episode ID，无法准确定位某次事件。
3. 即使默认只注入目录，summary 也会不断失焦，原文分页会越来越深。

因此 Episode 实例必须是有限的。达到以下任一客观上限时，Runtime 关闭当前 Episode，
为后续内容建立 successor Episode，并使用 `episode_links(kind="continues")` 连接：

- 从 Episode `created_at` 起达到最大持续天数；
- turn 数达到上限；
- 关联原始消息 token 数达到上限；
- 单个 Episode 的 summary/claims 达到维护上限；
- legacy 迁移或维护发现该 Episode 已经由多个不相关活动组成。

前四项是资源和结构约束，不是语义启发式。最后一项是否拆分以及如何拆分由 LLM 决定。

建议的初始保护值：

- `episode_max_span_days`: 30
- `episode_max_turns`: 64
- `episode_max_raw_tokens`: 100000

这些值需要通过生产数据评估，但“上限必须有限”是架构要求。不能允许 `open_loops`、
高 salience、open 状态或 Goal 存在绕过强制滚动。

长期连续性不依赖一个永久 Episode：

- Goal 保存长期任务状态；
- Memory 保存已确认的长期事实和偏好；
- successor Episode 保存下一阶段活动；
- `continues` links 保留跨 Episode 的话题连续性；
- 搜索可以一次返回整个 successor 链的目录。

以家庭开关门事件为例：

- 正常设计应把一次具体告警、一次授权开门或一段连续异常作为独立 Episode；
- 多次日常事件可以按有限时间段形成多个 Episode；
- 它们共享 topics/entities，并通过 `continues` 或 `references` 关联；
- 即使 Planner 错误地持续复用同一 Episode，硬上限也会强制创建下一段；
- 旧段超过默认时间范围后退出自动目录，但仍可按“门锁”、日期或时间段搜索。

这里的强制滚动不是删除，也不是忘记。它是为了保持每个 Episode 有清晰时间范围和可读
总结。

### 7.5 显式长时间搜索

搜索工具支持：

- 默认最近 30 天；
- `lookback_days`；
- `from`；
- `to`；
- `all_time=true`。

`from`、`to` 使用带时区的 ISO 8601 时间。

当参数冲突时拒绝调用，不猜测。

## 8. Episode 搜索与读取工具

优先扩展现有 `conversation_search` 和 `conversation_read`，不增加重复工具。

### 8.1 conversation_search

建议 schema：

```json
{
  "name": "conversation_search",
  "arguments": {
    "query": "回复拆分 www 使用",
    "detail": "summary",
    "lookback_days": 30,
    "from": null,
    "to": null,
    "limit": 5
  }
}
```

字段：

- `query`：可为空；为空时按时间列出 Episode。
- `detail`：
  - `summary`：默认，只返回目录和总结；
  - `messages`：同时返回匹配 Episode 的一页原文。
- `lookback_days`：默认 30；显式传入时覆盖默认。
- `from`、`to`：精确时间段。
- `all_time`：显式全历史。
- `limit`：1 至 10。

时间参数只允许一种模式：

- `lookback_days`
- `from`/`to`
- `all_time`

默认结果：

```json
{
  "ok": true,
  "count": 2,
  "range": {
    "from": "2026-07-16T10:00:00+08:00",
    "to": "2026-08-15T10:00:00+08:00"
  },
  "results": [
    {
      "id": "episode-id",
      "title": "回复拆分与连续消息风格反馈",
      "status": "closed",
      "created_at": "2026-08-06T01:47:13+08:00",
      "last_activity_at": "2026-08-06T03:20:00+08:00",
      "summary": "主人指出回复拆分、过度描述和 www 使用方面的问题。",
      "topics": ["回复拆分", "表达风格"],
      "entities": ["Momoi"],
      "open_loops": [],
      "summary_quality": "grounded",
      "match": {
        "score": 0.82,
        "message_count": 3,
        "ordinals": [3, 8]
      }
    }
  ]
}
```

默认 summary 模式不返回消息正文。

### 8.2 detail=messages

显式要求原文时：

```json
{
  "query": "门锁一次性密码",
  "detail": "messages",
  "from": "2026-08-03T14:00:00+08:00",
  "to": "2026-08-03T17:00:00+08:00",
  "limit": 3
}
```

返回：

- Episode 目录；
- 与 query 直接匹配的消息；
- 每个 Episode 最近或相关的一页消息；
- `next_before_ordinal`；
- 超长消息的 `next_content_offset`。

不能因为 `detail=messages` 就无上限返回整个数据库。完整记录仍通过分页读取。

### 8.3 conversation_read

保留现有分页工具，并增加可选过滤：

```json
{
  "episode_id": "episode-id",
  "before_ordinal": 20,
  "from": "2026-08-03T14:00:00+08:00",
  "to": "2026-08-03T17:00:00+08:00",
  "roles": ["user", "assistant"],
  "delivery_states": ["delivered", "uncertain", "internal"]
}
```

默认读取最新一页。

`conversation_read` 是审计和细节读取工具，不参与每轮自动注入。

### 8.4 MCP 暴露

如果要让外部 MCP 客户端访问，暴露与内部工具相同的 schema：

- `episode_search`
- `episode_read`

内部可以继续调用同一 Store 方法，避免维护两套实现。

MCP 层只负责协议适配、身份鉴权和结果序列化。

## 9. 搜索实现

### 9.1 搜索数据源

Episode 搜索索引继续来自：

- title
- grounded summary
- topics
- entities
- open loops
- 已验证 claims
- 与 Episode 关联的消息索引
- context plan 中绑定到该 Episode 的 unit text、intent、references 和 recall request

legacy summary 可以参与低权重召回，但必须标记质量，不能当成可靠事实返回。

### 9.2 排序

建议排序因素：

1. intent unit 覆盖；
2. 查询词覆盖率；
3. title 和 summary 命中；
4. message 证据命中；
5. topics/entities 命中；
6. 时间接近程度；
7. salience 作为轻微调整。

时间先作为搜索范围过滤，再作为同范围内的排序因素。

不能让一次维护更新后的 `updated_at` 使旧 Episode 排到前面。

检索不能只取全局 Top N。应先为每个 intent unit 分配结果，再在剩余目录预算内按总分填充，
避免一个高频关键词压掉其他 intent 的历史。

### 9.3 空查询

`conversation_search(query="")` 可以用于：

- 列出某个时间范围内的 Episode；
- 回答“上个月聊了什么”；
- 按日期浏览。

空查询时只按时间排序，不运行关键词重叠阈值。

### 9.4 搜索结果的证据位置

summary 模式可以返回：

- 命中 message IDs；
- ordinals；
- timestamps；
- 匹配数量。

不返回正文。

这样主模型知道有证据可读，但不会自动获得无关原文。

## 10. 提示词设计

### 10.1 Context Planner 提示词

Planner 应明确：

- `episode_bindings` 只用于归档当前 turn。
- `recall_requests` 只在当前回复需要历史证据时填写。
- 当前话题与旧 Episode 只是词语相似时，建立新 Episode。
- 普通分享不因为出现“Momoi”“提示词”“代码”等实体就自动继续旧话题。
- 默认搜索最近 30 天。
- 主人明确指向更早时间，或问题要求历史比较时，才请求更长范围。
- Planner 只请求目录级 recall，不请求完整原文。
- `references` 记录指代解析，不会自动触发 recall。
- 不需要 recall 时使用空数组。

建议加入正反例，但例子只解释职责，不绑定具体中文关键词。

### 10.2 主模型系统提示词

增加以下原则：

```text
<episode_directory> contains compact search results, not a complete account of
the archived conversation. Use it only when it helps the current owner intent.
Do not mention or continue an old episode merely because it appears in the
directory.

Use conversation_search when the owner asks about older shared history and the
supplied directory is missing or insufficient. Search defaults to the last
30 days. Expand the time range only when the owner points to an older period,
asks for a historical comparison, or a narrower search failed and older
evidence is necessary.

Use summary results first. Read archived messages only when exact wording,
chronology, corrections, disputed facts, commitments, or omitted details matter.
Do not read full conversation records for ordinary social continuity.
```

同时删除依赖 harness 工具裁剪的假设。所有 Owner Turn 可以获得统一工具集合，由提示词和工具描述约束使用时机。

### 10.3 搜索工具描述

`conversation_search` 描述应告诉主模型：

- 默认最近 30 天；
- 默认只返回 summary；
- 什么时候扩大范围；
- 什么时候用 `detail=messages`；
- summary 不足时再用 `conversation_read`。

`conversation_read` 描述应告诉主模型：

- 只在需要原文时使用；
- 先读相关 Episode；
- 使用分页，不一次请求整个历史；
- `delivery=uncertain` 不是主人已收到的证明；
- `visibility=internal` 不是对主人说过的话。

## 11. 删除 harness 语义启发式

### 11.1 应删除

#### is_light_social_plan

当前用途：

- 根据 speech act 和 recall query 决定是否过滤 bound Episode；
- 决定是否移除搜索、Agenda、文件和 MCP 工具。

问题：

- 把 Planner 的语义输出再次解释为执行策略；
- `references`、`recall_queries` 的细微变化会切换整套工具；
- 不能正确处理“闲聊但需要历史”或“请求但不需要历史”；
- 与系统提示词重复；
- 是本次旧 Episode 注入链路的一部分。

处理：

- 删除函数；
- Owner Turn 始终提供统一工具集合；
- Episode 自动注入只由 `recall_requests` 决定；
- 工具是否调用交给主模型。

#### NON_OPEN_LOOP_SPEECH_ACTS 对 open_loops 的强制清空

当前 Parser 根据 speech act 修改 Planner 输出。

问题：

- emotional share 或 casual share 中也可能包含明确的跨 Turn 承诺；
- Parser 不应二次做语义判断；
- Planner 提示词已经定义 open loop。

处理：

- 删除强制清空；
- 保留数组长度和字符串长度校验；
- 通过 Planner 测试验证普通社交消息不会产生 open loops。

#### 根据绑定关系自动读取 Episode

处理：

- 删除 binding 到自动注入内容的隐式转换；
- binding 只在 commit 时用于 `episode_turns`；
- recall request 单独生成目录。

#### degraded_context_plan 自动用全文生成 recall query

当前 Planner 失败后，Runtime 将消息片段直接作为 recall query。

问题：

- Planner 失败会触发最宽泛、最不可控的检索；
- 普通闲聊在故障时反而更容易召回旧内容；
- fallback 在做语义决策。

处理：

- degraded plan 只保存当前消息和 uncertainty；
- 不自动 recall；
- 为每个消息批次建立中性新 Episode；
- recent conversation 和 always memory 仍然可用；
- 主模型仍可以主动使用搜索工具。

### 11.2 应保留

以下不是语义启发式：

- JSON schema 校验；
- speech act enum 校验；
- event 覆盖校验；
- episode_ref 存在性；
- primary binding 必须存在；
- relation enum；
- 字段长度和数量上限；
- tool 权限；
- evidence quote 必须来自当前主人输入；
- summary claim 必须逐字存在于归档消息；
- delivery state 规则；
- token budget；
- 搜索结果数；
- 默认 30 天时间范围；
- lexical/BM25/FTS 排序；
- 分页；
- 重试和超时；
- `/stop` 等明确协议命令。

### 11.3 需要单独评估

#### fallback Episode 标题提取

从当前消息第一行生成标题只是故障时的展示 fallback，可以保留。
它不能用于判断话题关系。

#### 搜索 overlap 阈值

这是检索排序策略，不是对话语义策略，可以保留，但需要离线评估。
如果召回率不足，优先替换为 SQLite FTS/BM25，而不是增加话题关键词规则。

#### Episode 自动关闭

open → closing → closed 是状态机，不是语义启发式。
但不能再用“最新 primary 之外全部关闭”替代 LLM 对多条并行话题的判断。
Planner 应明确哪些 Episode 仍是 primary/related，Reflection 可以做后续整理。

## 12. 防止 Episode 被持续污染

当前 related binding 会：

- 把当前 turn 挂到旧 Episode；
- 更新旧 Episode 的 title/topics/entities/open loops；
- 更新 `updated_at`；
- 让旧 Episode 更容易再次成为候选。

建议：

1. `primary` binding 表示当前 turn 主要属于该 Episode。
2. `related` binding 只建立 `episode_links` 或轻量关联，不默认把整个 turn 挂入旧 Episode。
3. 如果一个 intent unit 的内容确实同时属于两条话题线，Planner 可以显式输出多个归档 bindings。
4. 更新 Episode 元数据时：
   - 新 Episode 可以直接写入 Planner 元数据；
   - 已有 Episode 不应被一次 Planner 输出整体覆盖；
   - topics/entities 采用去重合并；
   - title 默认保持；
   - open loops 通过独立维护动作更新；
   - salience 不因一次弱关联被覆盖。

这样可以防止旧 Episode 被一句相关闲聊重新改名、更新时间和扩充内容。

## 13. 迁移现有 legacy Episode

### 13.1 legacy 识别

现有数据可通过以下条件识别：

- `working_summary_claims_json = []`
- `working_summary` 非空
- `summary` 非空但没有证据来源
- 包含 imported legacy/extractive 标记
- `summarized_through_ordinal = 0`

### 13.2 迁移顺序

1. 先停止 legacy summary 和 raw tail 的自动注入。
2. 让 closed legacy Episode 进入后台维护。
3. 为 legacy Episode 选择 verified claims。
4. 生成 grounded semantic summary。
5. 对明显混合的 Episode 执行 turn 重分组。
6. 重建 Episode 搜索索引。
7. 保留原始 messages，不做破坏性删除。

### 13.3 安全要求

- 每个原始 turn 在迁移前后至少属于一个 Episode。
- 不修改 message content。
- 不改变 delivery state。
- 不把 assistant `uncertain` 当作已送达。
- 所有拆分操作记录审计日志。
- 迁移可以重复执行，结果必须幂等。

## 14. 配置建议

扩展现有配置，不增加独立记忆服务：

```json
{
  "context": {
    "recent_raw_tokens": 64000,
    "recent_turns": 6,
    "memory_results": 6,
    "memory_tokens": 8000,
    "episode_directory_tokens": 8000,
    "episode_focused_max_entries": 16,
    "episode_directory_max_entries": 64,
    "episode_default_lookback_days": 30
  },
  "episode_annealing": {
    "enabled": true,
    "idle_seconds": 60,
    "max_seconds": 650,
    "maintain_closed_legacy": true,
    "episode_max_span_days": 30,
    "episode_max_turns": 64,
    "episode_max_raw_tokens": 100000
  }
}
```

现有 `summary_results` 和 `summary_tokens` 应迁移为目录预算配置。正常召回不再受 3 条
Episode 限制。

旧 Episode 原文只能通过工具结果预算控制。

## 15. 可观测性

每轮记录：

- Planner 产生的 recall requests；
- 实际搜索时间范围；
- 命中的 Episode IDs；
- 注入目录 token 数；
- 是否调用了 conversation search/read；
- 是否扩大时间范围；
- 读取了多少原始消息；
- 是否使用 all-time；
- 最终绑定到哪些 Episode；
- Episode 是否被创建、继续、关闭、拆分；
- summary quality。

建议指标：

- `episode_directory_tokens`
- `episode_directory_results`
- `episode_search_default_range_count`
- `episode_search_expanded_range_count`
- `episode_read_calls`
- `episode_raw_messages_returned`
- `episode_legacy_count`
- `episode_unsummarized_turns`
- `episode_split_count`
- `episode_auto_injection_raw_messages`

最后一项应始终为 0。

## 16. 测试与评估

### 16.1 必须通过的回归场景

#### 普通分享

输入：

```text
刚才给 Momoi 修改了提示词和代码，升级了
```

期望：

- Planner 可建立新 Episode；
- 可不发 recall request；
- 主模型不收到 8 月 3 日旧原文；
- 不出现门锁、台风、掉头发内容；
- 工具仍然可用，但模型不应无故调用。

#### 明确继续旧话题

输入：

```text
继续看看上次说的回复拆分问题
```

期望：

- 自动搜索最近 30 天；
- 注入相关 Episode 的 ID 和 summary；
- 不自动注入完整原文；
- summary 足够时直接回复。

#### 核对原话

输入：

```text
我上次关于 www 是怎么说的，给我原话
```

期望：

- 搜索 summary；
- 调用 read/messages；
- 返回直接匹配的主人原话；
- 不读取同 Episode 中无关时间段。

#### 长时间搜索

输入：

```text
去年第一次聊这个时我怎么说的？
```

期望：

- Planner 或主模型显式扩大搜索范围；
- 默认 30 天不被误认为“没有记忆”；
- 读取原文后再回答。

#### 时间段浏览

输入：

```text
帮我找 8 月 3 日下午关于门锁的对话
```

期望：

- 使用明确 from/to；
- 返回对应 Episode summary；
- 指定 messages 后只返回时间段内原文。

#### 旧 Memory

输入引用一年前确认的长期偏好。

期望：

- `memory_search` 仍能找到；
- 不受 Episode 30 天默认范围影响。

#### Planner 失败

期望：

- degraded plan 不自动检索当前全文；
- recent conversation 保留；
- 不注入旧 Episode 原文；
- 建立中性新 Episode。

### 16.2 离线评估指标

- Episode 目录相关率；
- Episode 目录召回率；
- 每个 intent unit 获得候选的覆盖率；
- 旧 Episode 召回率；
- 精确原话查找成功率；
- 无关原文注入率；
- 每轮 Episode 上下文 token 中位数和 P95；
- 全量历史搜索调用率；
- summary 支持率，即 summary 中每个事实是否可由 claims 支持；
- Episode 话题纯度；
- 同一 turn 丢失率，必须为 0。

### 16.3 成功标准

以下条件同时满足，才接近“超凡的记忆系统”：

1. 普通聊天几乎不出现无关旧话题。
2. 默认请求中的旧 Episode 内容稳定保持在独立目录预算内。
3. 主人要求回忆数月或数年前内容时仍能找到。
4. 主人要求原话时能定位到具体消息。
5. 长期 Memory 不受 Episode 时间范围影响。
6. 不依靠具体话题关键词规则。
7. Planner 失败不会导致更激进的历史注入。
8. legacy 混合 Episode 可以逐步修复。
9. 主模型能够从 summary 逐级读取到完整证据。
10. 默认模型请求不包含自动展开的旧 Episode 原文。
11. 没有 Episode 可以因持续收到新 turn 而无限增长。
12. 多 intent 输入不会因为固定 Top 3 而丢失其他 intent 的相关历史。

## 17. 实施阶段

### 阶段一：先切断无关原文自动注入

- `<recalled_episodes>` 改为目录渲染。
- 删除 `_episode_context()` 自动 raw tail。
- recent conversation 保持不变。
- binding 不再自动转成历史内容。
- 删除 `is_light_social_plan` 工具和注入分支。
- degraded plan 不自动 recall。

这是最重要的边界调整。

### 阶段二：搜索工具和时间范围

- 扩展 `conversation_search` 时间参数和 detail 参数。
- 默认最近 30 天。
- 支持空 query 的时间浏览。
- 保留 `conversation_read` 分页。
- 内部工具和 MCP 复用同一 Store API。

### 阶段三：完整总结

- closed Episode 也可进入维护。
- claims 之后生成 semantic summary。
- Planner 候选显示 summary quality。
- legacy summary 退出默认事实上下文。

### 阶段四：legacy Episode 重分组

- 实现 turn mapping 维护协议。
- 修复混合 Episode。
- 重建索引。
- 增加迁移审计。

### 阶段五：评估和调参

- 用生产 dump 和人工构造场景跑离线评估。
- 调整目录数量、summary 长度和搜索排序。
- 不通过增加话题关键词条件修复个例。

## 18. 最终行为

最终 Momoi 在普通对话中看到的是：

- 当前主人消息；
- 最近几轮原文；
- 已确认长期记忆；
- 少量相关 Episode 的 ID 和总结；
- 当前 Goal、Reminder 和必要状态。

当主人只是分享一次升级时，Momoi 不会看到旧门锁原文。

当主人问起很久以前的事情时，Momoi 可以主动扩大时间范围，先找到 Episode 总结，
再根据需要读取原始消息。

这使记忆能力和默认上下文大小解耦：

- 历史可以很深；
- 默认注入必须很小；
- 搜索可以很广；
- 原文读取必须有明确理由；
- 可靠长期事实继续由 memories 提供；
- Episode 负责可追溯的经历和话题历史。
