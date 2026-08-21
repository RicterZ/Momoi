# Context Planner 架构审查

Context Planner 的总体分层是合理的：它先解释 Owner 输入，再生成自动召回表达式、Episode 归档决定和执行 handoff；主模型保留最终判断与工具权限。当前最需要修复的不是提示词长度，而是 Episode 链接安全、降级召回、查询上限和跨字段契约之间的不一致。

> 结论：保留“每个 intent 必须提供 recall_queries”、OR 查询、Recent Turn 紧凑投影、advisory handoff 和 MCP 按需预载。优先修复 7 项契约或安全问题，另外 5 项先观察或小幅澄清。

## 决策摘要

| 项目 | 决策 | 原因 |
| --- | --- | --- |
| correction 如何拆 intent | 必须修改 | 当前文字可能让已撤销请求继续成为可执行 unit 和 Episode |
| event id 完整覆盖 | 必须修改 | parser 强制校验，但 prompt 未说明跨字段约束 |
| `episode_links` | 保留并必须加固 | 链接永久落库、影响候选排序；当前可修改无关历史 Episode，甚至创建 `continues` 环 |
| degraded plan 的 recall | 必须修改 | 声称“不自动召回”，实际仍执行 raw sentence 查询 |
| recall 全局六条上限 | 必须修改契约与选择策略 | prompt 声称所有查询都会执行，代码只执行前六条，并可能饿死后续 unit |
| Planner 内部工具目录 | 必须修改 | 目录声称列出可用能力，但遗漏 Thinking、Agenda、Builtin 等实际常驻工具 |
| 配置文档中的召回说明 | 必须修改 | 文档仍声称 Planner 不再提交自动召回关键词，与当前实现相反 |
| Context Planner 中的 Momoi 身份字样 | 必须修改 | 主身份已约定完全由 Soul 决定，规划器应只声明功能角色 |
| `clarify` mode | 建议澄清 | 当前只是 advisory/日志字段，不控制权限，但语义未定义 |
| context need 的 evidence 枚举 | 建议对齐 | `memory_search` 的 durable fact 没有直观枚举值 |
| `plan_adjustment` 权威性 | 轻微澄清 | 当前只应覆盖旧 Planner 解释，不能升级为 owner/tool 事实 |
| recall 后才发现旧 Episode | 先监控 | 会造成已绑定 Turn 的 Episode 分裂，但需要先量化发生率再改两阶段架构 |
| mandatory recall | 不修改 | 是最新明确业务设计，schema、测试和召回排序均以此为前提 |
| OR 表达式 | 不修改 | 后端确实逐 alternative 搜索，并优先排序多锚点命中 |
| Recent Turn 语义说明 | 不修改 | 与紧凑投影实现一致，且固定 prompt 可使用前缀缓存 |
| 首尾安全声明 | 不修改 | 重复很短，属于合理的 untrusted-data 防御 |

## 实际数据流

1. `ContextService._plan_owner_context` 组装固定记忆、Recent Turn Base/Append、Focus、Goals、Reminders、Episode candidates 和当前 Owner events。
2. Context Planner 通过唯一的 `submit_context_plan` 返回 intent units、Episode actions/links、recall queries 和 owner handoff。
3. `parse_context_plan` 执行结构和跨字段校验；第一次失败会把错误返回模型重试，第二次失败进入 deterministic degraded plan。
4. `build_plan_retrieval` 读取 Planner 的 recall queries，自动搜索 durable memory、reflection memory、Episodes 和 matched Turns。
5. 主模型得到 recall 结果与 handoff；所有内部工具常驻，Planner 只决定预载哪些 MCP server。
6. Owner Turn 成功提交时，Episode actions 和 links 才会落库；链接随后参与未来 Episode candidate 排序。

这个顺序说明 Planner 不是权限控制器，`execution.mode` 也不是 runtime 分支；但 Planner 的 Episode 决定和 recall expressions 会产生真实的长期状态与成本，因此必须比普通 advisory outline 更严格。

## Prompt 注入边界

远端实际 Thinking 抽样推翻了最初“Planner 只做上下文选择”的假设：Planner 会在 `execution.outline` 中提前决定回复节拍、消息数量、语气、收尾方式，甚至拟定接近可见文案的内容。仅让后续 Owner 主模型读取 `system.md`，无法阻止一个信息不完整但非常具体的 handoff 先行定调。

修订后的边界是：构造 Context Planner system prompt 时，在自身功能协议之后附加打包版本中同一份 `system.md` 原文，并明确标记为“下游 Owner contract”。它是可信规划约束，不是 Planner 身份、工具权限或执行指令；其中第二人称命令解释为下游 Owner 的要求。`{{SOUL}}` 和 `{{STYLE_CARD}}` 在 Planner 引用中保持未解析，身份与最终可见措辞仍只由后续 Owner 调用决定。Planner outline 同时收窄为证据、动作、验证、澄清和必要沟通节点，禁止起草可见措辞、指定人设/语气或决定精确气泡数量。

这样 `system.md` 中关于证据、沉默、气泡、进度消息、失败通知、`respond`、`reply_wait` 和 Turn 收尾的细节能在规划阶段生效，又不会复制规则或建立第二份易漂移契约。

随后将 `context_planner.md` 收敛为相对 `system.md` 的增量协议：共享的 delivery、reconciliation、`plan_adjustment`、`reply_wait`、陌生实体搜索细节和通用行为规则不再复述；只保留 Planner 输入权威映射、intent/event 转换、自动召回、handoff 字段映射、紧凑 Turn 解码和 Episode 规划约束。OR 查询示例也改为纯结构占位符，避免具体业务样本影响检索倾向。

## 必须修改

### 合并 batch 内的 correction，并说明 event 覆盖

当前 prompt 说 correction 可以触发拆分 intent unit，但“查 A；不对，查 B”不应产生两个可执行 unit。`conversation_guidance` 会把所有 unit 都展示给主模型，每个 unit 还必须绑定一个 Episode action；错误拆分可能让 A 继续执行或写入归档索引。

建议规则：

- 先按顺序应用当前 batch 内全部 correction；
- correction 改写同一目标时，将相关 event ids 合并到最终 operative unit；
- unit 的 `text` 应保留最终有效语义，避免已撤销关键词进入 Episode recall index；
- 只有 correction 本身是独立、仍有效的目标时才单独成 unit；
- 每个 supplied event id 至少属于一个 unit，不得创造 event id。

最后一条必须写进 prompt，因为 JSON Schema 无法表达所有 event id 的集合覆盖；parser 已在 `context_planner.py:643-701` 强制执行。

### 给 Episode links 定义语义并增加安全校验

`episode_links` 不是纯输出元数据。它会：

- 在 Turn commit 时永久写入 `episode_links` 表；
- 通过 `episode_context_scores` 给相邻 Episode 增加未来候选分数；
- 让 autonomous Episode 沿 `continues` successor 链寻找当前 Episode。

当前 parser 允许：

- 当前 Turn 没有绑定任何 Episode，却修改两个历史 candidate 之间的关系；
- 同一对 Episode 使用多个冲突 kind；
- `A continues B` 与 `B continues A` 同时存在。

最后一种关系会让 `_ensure_autonomous_episode` 的 successor `while` 循环缺少终止条件。最小复现已确认 `parse_context_plan` 接受双向 `continues` 环。

建议同时修改 prompt、schema 描述、parser 和 storage：

- 默认 `episode_links=[]`；相似、共同关键词或同时出现在 candidate list 中都不足以建立链接；
- `from_episode_ref` 必须是本 Turn Episode action 实际绑定的 Episode，不能任意改写历史图；
- `continues`：较新的 source 延续较旧的 target；
- `references`：source 明确引用但不属于 target 的同一具体经历；
- `supersedes`：source 明确替代或纠正 target；
- 拒绝同一有向 pair 的冲突 kind；
- 拒绝 `continues` 和 `supersedes` 环；
- `_ensure_autonomous_episode` 加 visited set，即使数据库已有坏数据也不能无限循环。

不建议移除整个功能：自动 roll 已经使用 `continues`，linked context 也确实参与候选排序。正确做法是把 Planner 写图权限收窄到当前 Turn。

### 修正 degraded plan 的召回矛盾

`degraded_context_plan` 当前写入：

```text
Context planner protocol failed (...); deterministic message segmentation is used without automatic historical recall.
```

但它同时为每个 segment 写入 `recall_queries=[part[:120]]`。`build_plan_retrieval` 不区分 degraded plan，仍然执行这些查询。最小复现中，`查一下保温杯` 出现在 `query_recall`，与 uncertainty 文案直接冲突；而整句 exact-substring 查询通常质量也很差。

建议保持 degraded path 的保守语义：degraded unit 使用空 `recall_queries`，跳过自动历史召回。正常 plan 的 schema 仍要求 1–3 条，不需要放宽。若业务希望 degraded 也召回，则必须改 uncertainty，并实现确定性的关键词提取，不能直接拿整句。

### 让六查询上限与业务契约一致

正常 schema 允许 12 个 intent、每个 1–3 条查询，理论上最多 36 条；`build_plan_retrieval` 实际只收集前 6 条。当前是按 unit 顺序逐个吃满，因此三个 unit 各三条时，只会执行 unit 1 和 unit 2，unit 3 完全没有当前 Turn recall。

这不要求取消 mandatory recall。建议：

- prompt/schema 描述区分“每个 unit 保存查询用于索引连续性”和“当前 Turn 自动执行有全局上限”；
- 明确每个 unit 的第一条是最高价值主查询；
- 收集算法按 query rank 轮询 unit：先取各 unit 第一条，再取第二条，而不是先吃满前面的 unit；
- 日志记录 emitted count、executed count 和 skipped unit ids；
- 若真实多意图 Turn 经常超过六个，再考虑批量检索或提高上限。

同时更新 `context_assembler.py:98-100` 的陈旧注释；它仍声称 Planner 只在 supplied context 不足时产出查询。

### 同步 Planner 的内部工具目录

`<available_internal_tools>` 当前只列出五项 Memory/Conversation 工具，却声称列出主模型可使用的能力。实际 Owner Turn 常驻：

- 7 个 Memory/Conversation/Thinking 工具；
- 6 个 Goal/Reminder 工具；
- 6 个 HTTP/File/Sleep builtin 工具。

最直接的功能缺口是 prompt/schema 允许 `thinking_search` 和 `thinking_read`，但目录没有它们。建议从实际 tool specs 自动生成精简目录，避免继续维护第二份手写列表；`context.needs` 仍只允许历史证据查询工具，不需要因此放宽。

### 更新陈旧配置文档

`docs/CONFIG.md` 和 `docs/CONFIG.zh-CN.md` 仍写着：

> Owner Context Planner no longer submits keywords for framework-executed Memory/Episode search.

这与最新 mandatory recall 完全相反。文档还会误导后续维护者认为 degraded path 没有自动 recall。应改为说明：每个 intent 提供查询、runtime 有界执行、结果注入主模型、context.needs 是之后可能进行的精确 resident lookup。

### 移除 Planner prompt 中的具体身份绑定

主 system contract 已把身份完全交给 Soul。Context Planner 是功能角色，不需要声明为 “Momoi's private Context Planner”，也不应写 “why Momoi made a past decision”。建议使用 “private Context Planner for the executing model” 和 “why the executing model made a past decision”。这不要求删除代码仓库中的 Momoi 产品命名或默认搜索停用词。

## 建议修改

### 定义 `clarify`，但不要把它当权限模式

`execution.mode` 只被渲染进 handoff 和日志，不控制工具面。所有内部工具仍常驻，遗漏 MCP 也可通过 `tool_enable` 补载。因此无需增加 runtime 分支，只需定义：当缺少只有 owner 能提供的事实或选择、且无法安全开始执行时使用 `clarify`；普通期待回复不算 clarify。

### 对齐 context need evidence 枚举

Prompt 说 `memory_search` 用于 durable fact，Owner schema 却没有 `durable_fact` 或 `relevant_history`；Heartbeat schema 已有 `relevant_history`。建议统一使用 `relevant_history`，或明确 durable fact 应映射到哪个现有值。该字段当前只用于 handoff 展示，短期不会破坏执行。

### 限定 `plan_adjustment` 的证据地位

保留“比它所纠正的旧 Planner interpretation 更强”是合理的，因为主 system 只允许在当前 owner intent 或已验证 tool evidence 推翻 handoff 时生成 adjustment。建议补一句：它不高于原始 owner messages、delivery records 或 tool results，也不能单独证明外部事实。

### 只在适用时写 execution steps

当前固定模板容易让 direct response 也生成“lookup → work → verify → respond”样板。建议允许一条 direct-response outline；工具任务仍要求在 success claim 前验证。

## 暂不修改

### 保留 mandatory recall

这是 2026-08-21 最新提交明确引入的产品选择，不是偶然重复。Schema 强制 minItems=1，测试明确拒绝缺少 recall_queries 的 unit，配置也为自动 memory/Episode recall 提供独立上限。它符合长期私人 agent 对隐式连续性的需求。

低信息消息的噪声已有三层抑制：查询通常 miss、结果数量有界、主 system 禁止仅因旧 Episode 出现就主动续聊。因此现阶段应修真实契约和公平性，而不是恢复“上下文足够就不召回”。

### 保留 OR 查询

`search_alternatives` 确实按 `|` 拆分完整 phrase；Memory/Reflection 按命中 alternative 比例评分，Episode 搜索分别查询后合并，并优先命中更多 anchors 的 Episode。协议只需使用 `primary-name | known-alias | exact-identifier` 这类结构示例，避免具体业务样本污染 Planner 的检索分布。

### 保留 Recent Turn 语义说明

Omitted defaults、delivery、short call id、truncated result 和 Focus 都对应真实压缩格式。该段虽偏实现细节，却是 Planner 正确解释证据所必需；它位于固定 system prompt 中，可使用 provider prefix cache。只需去掉重复的 “not a JSON envelope”。

### 保留 advisory handoff 和 MCP 路由

Owner Turn 始终拥有完整内部工具面，Planner 只预载外部 MCP；主模型可通过 `tool_enable` 补载遗漏 server。这个设计兼顾 schema 成本与纠错能力，无需让 Planner 获得执行权限。

## 需要监控的架构限制

Episode candidates 在 Planner 运行前根据 raw owner query 和近期上下文选定；Planner 产生的 recall queries 随后才执行。因此自动 recall 可能让主模型找到一个旧 Episode，却来不及让 Planner 把当前 Turn 绑定到该 Episode。

Episode consolidation 不能稳定补救：它只处理尚未挂到任何 Episode 的 completed Turn；如果 Planner 已错误创建一个新 Episode，该 Turn 不会再次进入 consolidation。

暂不建议直接引入第二次 Planner 调用。先增加指标：

- recalled Episode 是否不在原 candidate ids 中；
- 此时 Episode action 是 `new`、`none` 还是 `continue`；
- 后续 7 天内是否出现同主题重复 Episode；
- conversation search 是否频繁跨这些 Episode 命中。

若发生率明显，再考虑两种方案：先进行轻量 query planning 后生成最终 Episode plan，或允许一次受限的 post-recall Episode rebind。前者更正确但增加一次模型调用；后者成本低但需要非常保守的证据阈值。

## 推荐实施顺序

1. 修 `continues` 环、历史—历史 link 写入和 traversal visited guard。
2. 修 degraded recall 自相矛盾。
3. 明确 correction 合并、event 覆盖和 link 语义，并更新 prompt tests。
4. 修六查询的轮询策略、日志和文案契约。
5. 从真实 tool specs 生成 Planner 内部能力目录。
6. 更新中英文配置文档和相关注释。
7. 定义 clarify、对齐 evidence 枚举、收紧 plan_adjustment 表述。
8. 上线 Episode post-recall mismatch 指标，再决定是否修改两阶段架构。

## 验证要求

实施时至少增加以下回归测试：

- batch correction 只留下最终 operative intent，且覆盖所有 source event ids；
- link source 必须属于当前 Turn binding；
- 双向或间接 `continues` 环被拒绝，已有坏图 traversal 也能终止；
- degraded plan 不产生 `query_recall`；
- 三个多查询 unit 的第一条查询优先于任何 unit 的第二、第三条；
- Planner 内部目录与实际常驻 tool names 保持同步；
- `clarify`、`respond`、`work` 三种典型 handoff；
- CONFIG 中英文文档不再包含“Planner 不提交自动召回关键词”的旧结论。

## 实施结果

本轮已完成上述立即修改项：correction/event/link/clarify 等 Planner 契约已补齐；自动召回改为跨 unit 按查询优先级公平选择；degraded plan 不再自动召回；Planner 工具目录从真实常驻 specs 生成；parser 与 storage 同时拒绝非法 Episode link，历史坏环遍历也可终止；中英文配置文档已同步。两阶段 post-recall Episode rebind 仍按报告结论保持监控，不在本轮修改。

验证结果：`PYTHONPATH=src python -m pytest -q` 全量通过，共 356 项；`git diff --check` 通过。
