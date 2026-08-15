# Momoi Episode 记忆系统 TODO 与问题分类

更新时间：2026-08-15

本文不重复完整架构，只记录当前状态、剩余问题所属层级和实施顺序。
完整设计见 `EPISODE_MEMORY_ARCHITECTURE.zh-CN.md`。

## 0. 讨论与实施规则

后续工作按“块”推进。每一块必须依次经过：

1. **现状确认**：结合代码、数据库和生产日志确认真实问题。
2. **方案讨论**：明确对象语义、边界、默认行为和失败行为。
3. **决策记录**：把已确认结论写入本文；未确认内容只能标为候选，不能写成既定方案。
4. **实现范围确认**：列出本轮会改和不会改的文件、数据库与提示词。
5. **编码与测试**：只实现本块已经确认的内容。
6. **生产观察**：部署后记录指标和真实行为，再决定下一块。

未经讨论确认，不提前实现后续块，也不为后续需求增加预留抽象或配置。

### 讨论顺序

| 顺序 | 工作块 | 状态 |
| --- | --- | --- |
| 1 | Episode 边界、归档与尺寸滚动 | 已完成并通过生产观察 |
| 2 | Empty Episode 的 claims 与短摘要 | 已完成并通过生产观察 |
| 3 | 时间浏览与精确原文读取 | 部分完成，待讨论剩余部分 |
| 4 | 词法排序与检索评估集 | 待讨论 |
| 5 | 搜索进展反馈与停止机制 | 待讨论 |
| 6 | Context Planner 简化 | 待讨论 |
| 7 | 默认上下文目录策略 | 待讨论 |
| 8 | 向量/混合检索实验 | 暂缓，前置评估未完成 |
| 9 | 外部 MCP Episode adapter | 待讨论 |
| 10 | 质量监测与发布门槛 | 持续补充 |

### 每块的决策模板

每块讨论结束后必须记录：

- **要解决的问题**
- **数据对象和字段语义**
- **默认行为**
- **允许的例外**
- **Harness 必须执行的硬约束**
- **交给 LLM 的语义判断**
- **失败与降级行为**
- **迁移或兼容要求**
- **验收场景与指标**
- **明确不做的内容**

## 1. 当前已完成

- Episode binding 与历史 recall 分离。
- 默认上下文不再自动注入旧 Episode 原文。
- 无 `recall_queries` 时不注入 Episode 目录。
- 删除 `is_light_social_plan` 和按 speech act 修正 Planner 输出的逻辑。
- degraded plan 不再用当前全文自动检索历史。
- `episode_links` 可以指向候选中的未绑定 Episode。
- 32 个 legacy Episode 已一次性迁移，legacy 兼容代码已删除。
- `conversation_search` 支持关键词、空 query 时间浏览、时间范围、全历史和分页。
- 时间范围按 `messages.created_at` 匹配。
- 搜索结果只返回最多 300 tokens 的相关 claims。
- `conversation_read` 负责读取原始消息。
- Memory 工具只使用 tool schema 描述，不再重复注入 `MEMORY_TOOL_POLICY`。

## 2. 问题分类总览

| 现象 | 所属层 | 根因 | 不应由哪层解决 |
| --- | --- | --- | --- |
| 某段时间聊了什么却搜不到 | 时间浏览/工具协议 | 把浏览请求当成关键词搜索 | 不应先上向量检索 |
| 换一种说法后搜不到 | 检索引擎 | 纯词法召回不足 | 不应让 Planner 堆近义 query |
| 搜索十几次不停止 | 搜索编排 | 没有新证据反馈和停止纪律 | 不应靠话题关键词规则 |
| 搜到的 Episode 包含很多无关内容 | Episode 归档 | 边界错误或无限增长 | 不应靠扩大 Top N |
| 默认上下文出现旧无关内容 | 上下文组装 | 自动展开旧 Episode | 不应靠 Planner 猜得更准 |
| Planner 生成多个近义 query | Context Planner | 过度规划 | 不应由 Runtime 针对词语删除 |
| 原话定位困难 | 原文读取工具 | 缺少 query/time 过滤读取 | 不应把原文放回默认目录 |
| 搜索结果太长 | 工具结果 | 返回完整 claims/summary | 不应降低 recent conversation |
| 203 个 Episode 没有 claims | Episode 维护 | closed empty 不参与维护 | 不应在 Owner Turn 临时总结 |

## 3. A 类：Episode 储存与生命周期

### A1. Episode 边界

问题：

- “家庭门锁”“主动陪伴”“Momoi 开发”容易变成永久 Episode。
- 问候、亲昵动作和跨日期事件持续写入同一个 Episode。
- “回忆某事”可能被归档为新的“回忆……”元话题。

TODO：

- [x] Planner 明确 Episode 是一次事件、讨论或项目阶段，不是永久类别。
- [x] 类别使用 topics/entities 表达。
- [x] 跨阶段连续性使用 `continues` link。
- [ ] 建立问候、门锁、长期项目、游戏阶段、纠正、回忆问题的归档评估集。

讨论状态：**工作块 1 已确认并实施。** 新 Turn 允许不进入 Episode；在线 Planner
只做 `none/continue/new` 的初步判断，后台 Consolidator 再按连续上下文整理未归档
Turn。Episode 表示具体经历、事件、讨论、情绪过程或项目阶段，不再表示永久类别。

生产验收：**2026-08-15 完成。**

- HR 面试结束、薪资复盘、等待 offer 被连续归入同一具体事件；
- “揉揉，让我一个人待一会儿”被建立为新的独处/边界 Episode，并只引用 HR 面试事件；
- 默认目录保持 `raw_messages=0`；
- 最新未归档 Turn 的 `defer` 防护已生效；
- 旧空 Turn 已被排除，不再触发 Consolidator；
- ordinal 回填重排、摘要失效重建代码已部署；
- 服务无重启、无 Episode 数据库异常。

问候、门锁等场景继续纳入长期质量评估，但不再阻塞本工作块完成。

### A2. Episode 尺寸滚动

TODO：

- [x] 计算 Episode turn 数和 raw token 数。
- [x] 达到上限时创建 successor Episode。
- [x] Owner 与 autonomous Episode 使用相同保护。
- [x] 不因单纯存活超过 30 天切分。

讨论状态：**与 A1 合并完成。** 达到 64 Turns 或约 64000 raw tokens 时创建
successor，并用 `continues` 连接；不按存活天数强制切分。

### A3. Empty Episode 维护

后台会在主人空闲时自动为 closed empty Episode 生成 verified claims 和
narrative summary；主人发消息时会取消后台任务，之后继续。

TODO：

- [x] closed empty Episode 可进入后台 evidence selection。
- [x] 不在 Owner Turn 同步生成 claims。
- [x] 自动连续处理，失败时沿用现有退避，Owner Turn 始终优先。

生产验收：**2026-08-15 完成。**

- narrative Episode 从 3 增加到 6；
- empty Episode 从 203 减少到 200；
- 连续 6 次 `episode_anneal_complete`，未出现 summary failure；
- 后台任务在主人发消息时正常取消；
- 无需等待全部历史 Episode 清理完毕。

基于搜索频率和体积的优先级暂不实现；当前单主人场景按现有顺序自动清理已经足够。

### A4. 混合 Episode

历史已全部转换为可验证 claims，但 b392 等 Episode 的话题边界仍然混合。

TODO：

- [ ] 用命中跨度、topics 数量和 turn 数识别高污染 Episode。
- [ ] 后续做离线、可审计的拆分计划。
- [ ] 不在普通 daemon 循环中自动移动历史 turns。

## 4. B 类：检索引擎与排序

### B1. 词法检索排序

TODO：

- [ ] 区分 title、topics/entities、claims、message match 的权重。
- [ ] 精确实体和专有词优先。
- [ ] 主人 claims 高于 Momoi claims。
- [ ] 同一 Episode 的多条命中聚合。

### B2. 时间浏览

空 query 时间浏览已经可用。

剩余问题：

- 跨时间 Episode 返回的 claims 可能位于请求窗口外。

TODO：

- [ ] 空 query 时只选择窗口内消息对应的 claims。
- [ ] 返回 Episode 在窗口内的 first/last activity。
- [ ] 按窗口内活动时间排序。

### B3. 语义检索/向量召回（重任务，暂缓）

解决：

- 同义表达；
- 别称；
- 查询与原文没有共同词；
- 模糊语义候选召回。

不解决：

- 时间浏览；
- Episode 边界；
- 搜索停止；
- 事实可靠性；
- 默认上下文污染。

TODO：

- [ ] 建立真实检索评估集。
- [ ] 比较 lexical、vector、hybrid recall@10。
- [ ] 优先评估 SQLite + `sqlite-vec`。
- [ ] embedding 粒度以 verified claim 和 message 为主。
- [ ] 使用 RRF 合并词法和向量排名。
- [ ] 只有离线评估稳定提升后才接入在线路径。

暂不做：

- 独立 Qdrant/Weaviate 服务。
- 没有评估集前全量生成 embedding。
- 用单个向量表示大型多话题 Episode。

## 5. C 类：工具协议

### C1. conversation_search

已支持：

- 关键词搜索；
- 空 query 时间浏览；
- recent/range/all；
- cursor 分页；
- compact claims。

TODO：

- [ ] 返回 archive earliest/latest timestamp。
- [ ] 返回请求时间段是否有任何归档消息。
- [ ] 返回 Episode 在窗口内的 activity range。

### C2. conversation_read

TODO：

- [ ] 支持 query 匹配原文。
- [x] 支持 time_range 的 from/to 时间过滤。
- [x] 宽时间窗口的上下文风险写入 tool schema。
- [x] 保留 delivery state 标记。

本轮不增加 `matches/full` 模式。精确读取继续保持一个工具、一套分页：先由
`conversation_search` 定位 Episode，再用窄时间窗口读取原文；query 原文匹配留待
检索排序板块讨论。

### C3. memory_search 与 conversation_search 边界

- `memory_search`：规范化、确认过的长期事实和偏好。
- `conversation_search`：对话经历、时间线、原话位置和时间段话题。

TODO：

- [ ] 检查两个 tool description 是否仍有重叠。
- [ ] 用真实模型验证时间浏览不再并行调用 memory search。

## 6. D 类：搜索工作流与停止

真实问题：

- 模糊时间问题曾调用 11 次 `conversation_search`、5 次 `memory_search`。
- 多轮结果高度重复。
- 模型没有“是否出现新证据”的反馈。

### D1. 候选：客观进展信息

- `result_episode_ids`
- `new_episode_count`
- `new_message_count`
- `repeated_episode_count`
- `evidence_fingerprint`
- `no_new_evidence`

Runtime 只描述结果是否重复，不判断语义是否值得继续。

### D2. 候选：搜索节奏

- 有主题：一次关键词搜索。
- 无主题的时间问题：一次空 query 时间浏览。
- 目标超出存档范围：根据 archive coverage 停止或调整范围。
- summary 不足且已有候选 Episode：再 read。
- `no_new_evidence=true` 后停止同义词改写。

### D3. 候选：资源上限

是否增加硬上限仍待讨论。

原则：

1. 先实现进展反馈。
2. 观察模型是否能主动停止。
3. 模型仍忽略反馈时，再限制每 Turn 的 search/read 次数。

## 7. E 类：Context Planner

问题：

- 一个问题被拆成重复的 question + banter。
- 一个 intent 生成多个近义 recall query。
- recent conversation 足够时仍检索旧历史。
- 新建“回忆……”元话题 Episode。
- 普通互动复用大型永久 Episode。
- Planner 延迟和输出 token 偏高。

TODO：

- [ ] 一个语义目标只生成一个 intent unit。
- [ ] recall query 通常最多 1～2 条且必须互补。
- [ ] recent conversation 已足够时不再 recall。
- [ ] 回忆问题归入真实话题或建立 link。
- [ ] uncertainty 只保留影响处理的歧义。
- [ ] 监测 Planner latency、output tokens 和 degraded rate。

## 8. F 类：默认上下文注入

已经达到：

- 不自动提供旧原文。
- binding 不触发 recall。
- 无 recall query 时不提供 Episode 目录。

TODO：

- [ ] 按 intent unit 公平分配结果。
- [ ] 使用相关度阈值，不为填满预算加入弱结果。
- [ ] 评估 12 条安全上限。
- [ ] 监测目录 token 和低信息结果比例。

## 9. G 类：总结与证据

当前 `working_summary` 是 LLM 选择、Runtime 原文验证并格式化的 extractive claims，
`narrative_summary` 是基于这些 claims 生成的低权限自然语言总结。

TODO：

- [x] closed empty Episode 可以获得 claims。
- [x] 基于 verified claims 生成短 narrative summary。
- [x] 提示词要求 narrative summary 只能使用 claims 支持的事实。
- [x] 默认目录使用短摘要，完整 claims 和原文通过 read 获取。
- [ ] 生产观察 narrative/emotional/outcomes 的事实支持率。

## 10. H 类：可观测性与质量门槛

TODO：

- [ ] Planner degraded rate。
- [ ] Planner latency/output tokens。
- [ ] recall queries per intent。
- [ ] search/read calls per Turn。
- [ ] 重复搜索率。
- [ ] Episode directory tokens。
- [ ] Owner Turn input tokens P50/P95。
- [ ] 大 Episode 数量和增长速度。
- [ ] 明确主题 recall@10。
- [ ] 时间浏览 topic coverage。
- [ ] 精确原话定位成功率。

## 11. 推荐实施顺序

### 下一批：轻量且直接改善体验

1. 时间浏览摘要只使用窗口内 claims。
2. `conversation_search` 返回 archive coverage。
3. 进一步明确 memory search 与 conversation search 的 schema 边界。
4. 观察空 query 是否消除关键词猜测。

### 再下一批：控制深度搜索

1. Turn 内证据去重。
2. `no_new_evidence`。
3. 观察模型是否主动停止。
4. 再决定是否增加硬调用上限。

### Episode 数据治理

1. closed empty Episode 后台 claims。
2. Episode 尺寸滚动。
3. 高污染混合 Episode 离线拆分。

### 重型检索升级

1. 建评估集。
2. SQLite 向量实验。
3. 混合检索离线比较。
4. 达标后再接入生产。

## 12. 边界速查

- “某段时间聊了什么”找不到：时间浏览/工具协议问题。
- “换一种说法搜不到”：语义检索问题。
- “搜了十几次不停止”：工作流/进展反馈问题。
- “搜到的 Episode 什么都有”：Episode 边界问题。
- “默认上下文太大”：注入预算问题。
- “原话定位不到”：message-level read/index 问题。
- “Planner 生成太多 query”：Planner 提示词与模型质量问题。
- “Memory 返回门锁规则但用户问旧聊天”：工具职责或检索排序问题。
