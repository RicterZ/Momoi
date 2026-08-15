# 选择性 Episode 记忆实施文档

## 1. 本轮范围

本轮只实现：

1. Context Planner 允许每个 intent unit 选择 `none`、`continue`、`new`。
2. `none` Turn 只保存原始消息，不强制创建 fallback Episode。
3. 移除新数据中的 `related` binding；Episode 关系只使用 links。
4. 后台 Consolidator 处理尚未归档的 Owner Turns：
   - 忽略无长期意义的片段；
   - 把连续、有意义的片段组成新 Episode；
   - 或继续最近候选 Episode。
5. Episode annealing 输出 verified claims、narrative summary、emotional context 和 outcomes。
6. closed empty Episode 也可以进入后台维护。
7. Episode 达到 64 Turns 或约 64000 raw tokens 时滚动 successor，并用 `continues` 连接。

本轮不实现：

- 向量检索；
- 搜索停止机制；
- 自动拆分现有大型混合 Episode；
- 新的 Memory/Reflection 对象；
- 按存活天数切分 Episode。

## 2. Planner 协议

顶层字段：

```json
{
  "version": 2,
  "intent_units": [],
  "episode_actions": [],
  "episode_links": [],
  "uncertainty": []
}
```

每个 intent unit 必须恰好出现一次归档动作：

### none

```json
{
  "action": "none",
  "unit_ids": ["u1"]
}
```

### continue

```json
{
  "action": "continue",
  "episode_ref": "existing-id",
  "unit_ids": ["u1"],
  "topics": [],
  "entities": [],
  "open_loops": [],
  "salience": 0.5
}
```

### new

```json
{
  "action": "new",
  "episode_ref": "new:ascii-slug",
  "title": "具体事件或讨论",
  "unit_ids": ["u1"],
  "topics": [],
  "entities": [],
  "open_loops": [],
  "salience": 0.5
}
```

`episode_links` 只连接 Episode，不承担当前 Turn 归档。

## 3. 数据库

### conversation_episodes 新字段

- `narrative_summary TEXT NOT NULL DEFAULT ''`
- `emotional_context_json TEXT NOT NULL DEFAULT '{}'`
- `outcomes_json TEXT NOT NULL DEFAULT '[]'`

保留：

- `working_summary`
- `working_summary_claims_json`
- `summarized_through_ordinal`

### episode_consolidation_decisions

```sql
turn_id TEXT PRIMARY KEY
action TEXT CHECK(action IN ('ignored', 'linked'))
episode_id TEXT
reason TEXT NOT NULL DEFAULT ''
processed_at REAL NOT NULL
```

它只记录后台是否已经处理未归档 Turn，避免重复整理。

## 4. Owner Turn 提交

1. 始终保存 Turn 和 messages。
2. `none` action 不写 `episode_turns`。
3. `continue/new` action 写入一个 owning Episode。
4. 已有 Episode：
   - title 不被 Planner 覆盖；
   - topics/entities 去重合并；
   - open_loops 使用当前 owning action 的最新值；
   - salience 只升不降。
5. `closing` 且未继续的 Episode 自动关闭。
6. `open` Episode 不因切换话题自动关闭。

## 5. 后台 Consolidator

在现有 episode maintenance worker 中，优先处理未归档 Owner Turns，再做 Episode
annealing。

候选：

- completed Owner Turn；
- 至少包含一条 message；
- 没有 `episode_turns`；
- 没有 consolidation decision；
- 不含 queued assistant message；
- 按时间取最近一小批连续 Turn。

`continue` 只能从默认近 30 天的候选目录中选择；超出该范围的历史 Episode 仍可由
显式搜索找到，但后台整理不会仅因类别相似而继续写入。

LLM 输出每个 Turn 的处理：

- `defer`
- `ignore`
- `continue`
- `new`

Runtime 校验：

- 每个候选 Turn 恰好覆盖一次；
- 最新 Turn 不能 `ignore`，信息不足时必须 `defer`；
- `defer` 不写 decision，下次出现新的 Owner Turn 后会连同新上下文再次判断；
- continue 只能使用候选 Episode；
- new ref 必须为 ASCII slug；
- ignored Turn 不进入 Episode；
- 不修改原始 messages。

当 Consolidator 把较早 Turn 补入一个已经存在的 Episode 时，Runtime 按消息实际
时间重新生成连续 ordinal。若已有 ordinal 发生变化，则清空该 Episode 的 claims、
narrative、emotional context、outcomes 和总结进度，交给 annealing 重新生成，避免
旧引用中的 ordinal 与原文顺序不一致。

## 6. Episode 维护

Evidence-selection 输出升级为：

```json
{
  "version": 2,
  "claims": [],
  "narrative_summary": "",
  "emotional_context": {
    "owner": "",
    "momoi": "",
    "tone": ""
  },
  "outcomes": []
}
```

Runtime：

- 继续逐条验证 claims；
- narrative/emotional/outcomes 视为基于 claims 的低权限情景解释；
- 默认 Episode 目录优先使用 narrative summary；
- narrative 为空时使用 compact extractive claims。

维护候选包括：

- open/closing 且存在未总结 turns；
- closed empty；
- 有 claims 但 narrative 为空。

## 7. 尺寸滚动

写入已有 Episode 前计算：

- `COUNT(episode_turns)`
- 关联 messages 的估算 token 数

达到任一条件：

- 64 Turns
- 64000 raw tokens

则：

1. 创建 successor；
2. 继承 title/topics/entities/open_loops/salience；
3. 旧 Episode 关闭；
4. successor 写入当前 Turn；
5. 建立 `successor -> old, continues`。

## 8. 迁移

启动时自动增加字段和 consolidation 表。

现有 context plan v1 保持可读；新 Planner 使用 v2。数据库中已有 Episode 和 Turns
不重写。

## 9. 验收

1. 普通问候可以 `none`，不会创建 Episode。
2. “揉揉”处于明确上下文时可和当前 Episode 一起整理。
3. 简短回应根据最近上下文 `continue`。
4. 新讨论可 `new`。
5. 回忆问题不强制创建“回忆……”Episode。
6. Planner 失败仍保存消息但不复用旧 Episode。
7. 后台可把连续未归档 Turn 整理成 Episode。
8. 最新低信息 Turn 会 defer；有后续上下文后才允许最终 ignored。
9. closed empty Episode 可获得 claims 和 narrative。
10. Episode 超限后自动 successor。
11. 原始 messages 不因整理被修改。
12. 默认目录使用 narrative summary，原文仍需显式读取。

## 10. 实施结果

- 数据库迁移在 Store 启动时自动执行，不需要手工迁移。
- Planner v1 仅保留读取兼容；新请求固定使用 v2。
- 原始 messages 表及投递状态不被 Consolidator 改写。
- 完整测试：215 项通过。
- 2026-08-15 生产观察通过：真实 Owner Turns 正确归档，空 Turn 不再进入
  Consolidator，默认 Episode 目录没有原文，后台 narrative summary 正常生成。
- 第二次生产观察确认自动清理持续推进：narrative Episode 从 3 增加到 6，empty
  Episode 从 203 减少到 200；主人消息仍能取消后台任务。
