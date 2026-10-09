# Momoi 单用户记忆：分类、检索与写入方案审计

状态：**Milestone 7 原始方案；Milestone 8 已实施，详见 [标签与过滤](2026-10-09_memory-metadata.md)**。代码基线为 `1e6f12aa`（Milestone 6）。本文保留最初方案供对照；当前可用接口以 Milestone 8 文档为准，plan/apply 和 scope 仍未实现，线上迁移未执行。

建议保留单用户、同一 SQLite 数据库和 `src/momoi/memory`。普通调用保持 `await memory.search("用户喜欢什么饮品")`；新增主题标签作为可选过滤条件，把工作流范围从 key 中分离。写入分成库内的计划与原子提交，Momoi 继续验证所有者证据并管理后台回合。

本轮需要审计四个决定：

1. 保留现有 kind；增加代码预定义、模型选用的多值主题标签，不按主题分库或建立 namespace 树。
2. 普通 search 不要求标签；显式标签过滤必须在候选截断之前执行。
3. 用有限候选判断同一事实，沿用目标 ID、证据和快照校验；相似度本身不触发合并。
4. 写入公开接口先采用 `plan/apply`；schema 与线上数据迁移另设里程碑，最后删除旧导入和 Store 转发代码。

## 当前已经具备什么

| 部分 | 当前实现与边界 | 本地依据 |
| --- | --- | --- |
| 统一入口 | `Memory` 提供 search、rank、search_literal、snapshots；拥有共享连接上的 repository 和纯记忆 index source | [公开 API](../src/momoi/memory/service.py) |
| 分类 | 写入允许八种 kind；它们混合了事实性质与内容领域，不足以表示“饮食、旅行、技术”等横向主题 | [分类常量](../src/momoi/storage/memory/memory_values.py) |
| 作用范围 | activation 为 always、recall、scoped；scoped 的工作流身份编码在 key 前缀中 | [写入校验](../src/momoi/runtime/workflows/memory_operation/parsing.py) |
| 召回 | 注入编码器后支持向量与字面匹配结合；只返回有限结果。当前 rank 每池最多六条，kinds 在评分阶段检查 | [召回服务](../src/momoi/memory/retrieval/service.py)、[向量查询](../src/momoi/memory/retrieval/dense.py) |
| rerank | 库接受注入的 reranker；未注入时返回评分结果。Momoi 上下文适配层做一次 memory + Episode 联合模型选择 | [联合选择](../src/momoi/runtime/context/service.py) |
| 写入决策 | 前台提出请求，后台读取证据与候选，模型返回 write、forget、noop、defer；库尚无公开 plan/apply | [工作流](../src/momoi/runtime/workflows/memory_operation/workflow.py)、[当前提示词](../src/momoi/prompts/memory_operation.md) |
| 持久化 | write 可以创建新行并替代目标，继承证据；另一条维护路径的 merge 保留 survivor ID。两者目前语义不同 | [repository](../src/momoi/memory/storage/repository.py) |
| 遗忘与冲突 | tombstone 以 kind/key 定位；快照指纹保护已读取记录。数据库只有活动 kind/key 普通索引，不能描述成唯一约束 | [schema](../src/momoi/storage/core/schema.sql)、[指纹](../src/momoi/memory/storage/records.py) |

当前 sparse 评分仍会读取可召回记录，向量快照仍在内存中计算相似度；“不给 LLM 全部记忆”不等于底层完全不扫描记忆。单用户阶段保留这种简单实现，先约束模型输入与查询结果，不引入外部向量数据库。

目前关于自然语言命中的回归测试使用模拟编码器，只证明调用链和排序边界；真实中文同义表达的召回质量还需要独立评测。

## 借鉴 mem0 的哪些机制

以下分别是源码事实和平台文档行为，不把不同版本混在一起：

- OSS `v0.1.118`：先抽取新事实，每条事实向量检索最多五条旧记忆，按 ID 去重后交给模型决定 ADD、UPDATE、DELETE、NONE。候选集并不保证覆盖所有同义事实。[固定版本源码](https://github.com/mem0ai/mem0/blob/ed5a1e9fc68fcd7671a70c33a540d24573755449/mem0/memory/main.py#L333-L425)
- 本轮读取的主线提交 `b7ad69afda6b6ed030347c66d48a13e4de9dec08`：`infer=True` 写入路径先用消息检索十条候选，再做增量抽取、哈希判重和新增持久化。这里的哈希集合来自候选及本批次，不能说是全库语义去重；该路径也不是旧版的四动作更新流程。[固定提交源码](https://github.com/mem0ai/mem0/blob/b7ad69afda6b6ed030347c66d48a13e4de9dec08/mem0/memory/main.py#L839-L1000)
- Platform 的 custom categories 由应用配置目录，服务写入时从目录中自动选择标签；修改目录不会自动重标历史记录。这是平台文档行为，不据此断言 OSS 有同样的分类实现或公开了分类模型细节。[官方分类文档](https://docs.mem0.ai/platform/features/custom-categories)

Momoi 借鉴有限候选、结构化决策和受控标签目录。继续保留现有证据约束、版本替代、遗忘记录以及一次联合 rerank；不引入 mem0 依赖、多租户、实体图谱或第二套数据库。

## 让 kind、主题和 scope 各自承担一个职责

以下为拟议的公开记录形状。ID、证据和系统字段由代码生成；示例中的咖啡偏好仅用于说明设计。

```json
{
  "id": 42,
  "kind": "preference",
  "key": "coffee.sweetness",
  "content": "用户喝咖啡时喜欢无糖。",
  "activation": "recall",
  "meta": {"tags": ["food_drink"], "scope": null}
}
```

| 字段 | 表达什么 | 谁决定 |
| --- | --- | --- |
| kind | 事实的性质或领域，第一阶段保留现有八种合法值 | 应用给枚举，模型建议，库校验 |
| meta.tags | 可跨 kind 的主题，例如 food_drink | 应用定义目录；调用方显式指定，或写入模型从目录选择 |
| activation | 是否固定注入、按需召回或仅在工作流内生效 | 模型可建议；Momoi 验证既有策略 |
| meta.scope | scoped 记忆适用的具体工作流 | 宿主根据真实工作流身份设置或验证；分类模型不能自由创建 |
| key | 同一范围内的稳定事实名称，用于定位、冲突检查及遗忘 | 新建时模型可提议；更新优先复用已有值，最终由库验证 |
| ID、时间、来源和证据 | 版本身份与来源追踪 | 代码或宿主提供，不能从自由 meta 覆盖 |

key 和 tags 都不是“同一事实”的充分条件。同一个 food_drink 标签可包含互不相关的事实；两个不同 key 也可能需要合并。

建议第一版标签目录如下，属于应用配置而非库内硬编码。库只实现目录与校验协议。

| 标签 ID | 含义 |
| --- | --- |
| food_drink | 饮食、饮品、口味及相关限制 |
| health | 健康、身体状况与长期健康习惯 |
| work_study | 工作、学习及相关安排和方法 |
| technology | 软件、设备、编程和技术偏好 |
| travel | 出行、地点及旅行偏好 |
| leisure | 游戏、音乐、阅读等休闲活动 |
| daily_life | 作息、居住、生活习惯 |
| social | 家人、朋友、其他人和人际关系 |
| communication | 称呼、表达方式、互动边界 |

每个标签有稳定 ID 和描述。默认允许零到三个标签；没有合适标签就留空，不让模型造新标签，也不强迫塞进 misc。分类并入写入决策的模型调用，不为每次 recall 再增加一次分类请求。

调用方提供 tags 时按显式值校验和使用；未提供时模型才选择。未知标签返回校验错误，不能静默忽略。目录变更不会改写历史记录；需要重新分类时，另做可审阅的批处理。

## 如何从“用户喜欢什么饮品”找到“无糖咖啡”

当前已支持的调用，前提是宿主已为 `memory` 配置编码器与有效索引：

```python
results = await memory.search("用户喜欢什么饮品")
```

以下仅为拟议接口，尚未实现：

```python
results = await memory.search(
    "用户喜欢什么饮品",
    filters={"tags_any": ["food_drink"], "kinds": ["preference"]},
)
```

建议检索顺序：

1. 根据 activation、显式 scope、遗忘和版本状态确定可见集合。默认搜索全局 recall 记忆；always 由现有固定上下文使用，scoped 只在宿主明确提供范围时检索。
2. 如有显式 filters，先在可见集合中筛出符合 tags/kinds 的 ID。`tags_any` 为任一标签命中，`kinds` 为任一 kind 命中；不同字段之间取交集。省略或空列表表示不限制；首版不支持任意 JSON 表达式。后续增加的 filters.scope 为精确范围匹配，显式指定时不混入全局记录；宿主若需要两者，应明确组合两个范围。
3. 编码自然语言问题，对合格 ID 的向量评分，同时做字面召回。必须在 top-k 之前限制 ID，不能先截断全库向量结果再筛标签。
4. 合并评分候选，再交给注入的 reranker，返回 limit 条。Momoi 联合路径继续共用一次 embedding、一次 memory + Episode 选择。

默认不自动给问题加 food_drink 硬过滤。未标注、错标或跨主题的记忆仍有机会通过语义命中。显式过滤则严格生效：如果没有匹配项，返回空，不偷偷扩大范围；旧记录 tags 为空时不会通过显式标签过滤。

“无糖咖啡”与“喜欢什么饮品”由编码器的语义相似度建立联系，标签本身不完成这种推理。编码器不可用时保留字面回退，但不能承诺这类无词面重合的查询仍能命中。

实现时需把候选预算与最终 limit 分离。目前六条上限发生在 rerank 之前，不能简单给现有 search 传更大 limit 就认为模型见到了更多候选。第一版建议候选最多 24 条、最终默认六条；具体阈值继续沿用现有模型校准，先以评测验证候选预算。

## 如何判断相同记忆，而不把全库交给模型

写入使用单独的候选选择过程，不复用“给用户返回六条答案”的截断策略：

1. 接收宿主已认证的变更请求与引用。若一条请求含多个独立事实，拆成有界批次；第一版建议最多八条事实，超过则分批，不静默丢弃。
2. 在同一有效作用范围中查精确正文、现有 kind/key 冲突、显式目标，再做每事实最多八条的语义/字面候选召回。普通问答不检索的 always/scoped 记录，在写入流程中须按请求范围纳入候选；不能直接调用默认 search 代替。
3. 合并重复候选，单批最多 32 条，候选正文预算建议 8,000 token。显式目标与精确冲突记录优先保留；它们本身超过预算就拆批或 defer，不能裁掉后继续写。
4. 模型比较主体、事实对象、肯定/否定、时间条件、行为要求与作用范围，只能引用所给候选 ID。标签只帮助组织内容，不用作判重的强制分区。
5. 库验证动作和目标，提交前重读快照。相似度高只代表值得比较，不能据此直接替换或删除。

正文完全相同且语义范围一致，可走确定性重复检查；规范化最多采用 Unicode NFC 和首尾空白处理，不抹掉否定词、数字、标点或有意义的大小写。跨主体或跨 scope，即使同文也不能直接去重。

| 新证据与旧记忆 | 建议决策 |
| --- | --- |
| 都表示“用户喝咖啡喜欢无糖”，只换了说法 | noop，必要时给目标追加经验证的证据 |
| 旧记录说喜欢咖啡，新证据明确“喝咖啡不加糖” | 如果描述同一偏好，可 write 更新并保留条件 |
| 旧记录说无糖，新证据明确“现在改成加糖” | 同一主体和范围内 write 替代旧版本 |
| 旧记录说早上无糖，新证据说聚会时喝甜饮料 | 条件不同，不能当作简单矛盾覆盖 |
| 用户喜欢无糖，朋友喜欢加糖 | 主体不同，分别保存 |
| 两条重复记录补充了同一规则的条件 | write 多个 target_ids，合成一条完整事实并继承证据 |
| 意图、目标或证据不充分 | defer，不以“没搜到”证明新事实不存在 |

有限召回不能保证全库无语义重复；候选漏召回、不同表达和索引延迟都可能影响结果。遇到编码失败，不让模型把空候选当作“可以安全新增”：除确定性精确操作外，返回 defer 或可重试错误。数据库中的新增/更新但尚未编码的记录，还应参与字面和精确候选查询。

遗忘记录也必须参加写入校验。普通查询隐藏 tombstone；写入检查需要单独检索已遗忘目标及其保留的正文/证据，避免仅换 key 后从旧上下文复活。重新添加要求新的明确用户证据。语义改写的遗忘匹配同样不是可保证完备的自动判定。

## 写入接口怎样与 Momoi 解耦

建议第一版仅增加 `plan` 和 `apply`，不同时引入立即写库的 add 与另一套后台写入机制。下面是拟议宿主调用过程，变量由 Momoi 适配层构造，并非当前可运行 API：

```python
plan = await memory.plan(requests, evidence=verified_evidence)
with transaction(database):
    verify_current_owner_evidence(plan)
    result = memory.apply(plan, operation_id=batch_id)
    complete_memory_batch_and_turn(batch_id, result)
```

- `plan` 可调用模型但不修改数据库。输入是库定义的请求、原文引用和范围值，不接受 IncomingMessage、TurnDraft、Store 或 provider 对象。
- `apply` 同步执行，不调用模型。它重校验结构、标签目录、目标存在性、scope、快照指纹和 key 冲突，随后在同一 SQLite 事务中提交版本、证据、遗忘状态、索引失效与提交回执。
- Momoi 在外层事务中核对引用仍是已认证 owner 事件的精确子串，同时完成 batch、turn 和 journal。库把来源视为不透明引用；不能把字符串 `authority="owner"` 当成认证。
- 已注入的 LLM 适配器负责模型传输、结构化输出和错误处理；推理结果必须经过库的命令校验。来源材料不是可以改变系统规则的指令。

保留 write/forget/noop/defer 的动作族：write 无目标为新增，一个目标为替代，多个目标为合并。目标替代统一采用新行加 superseded_by，继承证据。当前维护路径的 survivor-ID merge 需在后续接入时改用同一规则，并验证所有保存快照的消费者；本轮不暗改它。

拟议的 noop 允许引用目标并追加证据，但不生成新记忆、不重新编码；这是对当前 noop 不带 evidence/target_ids 协议的明确扩展。纯标签修订形成计划中的独立元数据变更项，仍由 apply 检查指纹和标签目录；只更新 meta/updated_at，不生成内容新版本。forget 仍要求用户的遗忘或明确否定证据，不因相似度、低置信度或“看起来过时”自动执行。

`operation_id` 支持幂等：相同 ID 与相同已验证计划重复提交返回原结果；相同 ID 携带不同计划报冲突。输入摘要须覆盖动作、目标、正文、meta 和引用。提交回执与修改原子落库，宿主失败回滚时一并撤销。

快照变化则拒绝旧计划并重新读取、规划，不在持有写锁时请求模型。Momoi 继续按既有队列顺序处理写入。目标指纹和幂等回执不等于全库语义串行化；多个独立进程并发规划仍可能新增重复事实，首版不承诺解决这个场景。

## schema 与迁移建议

仍使用 Momoi 数据库，由 Momoi 管理连接、schema 版本和上线。建议按下面两段审计、实施，当前不执行任何 DDL。

| 变更 | 用途 | 迁移原则 |
| --- | --- | --- |
| memories.meta_json，默认 `{}` | 保存 tags；第一版只接受预定义字段，不接受任意深层 JSON | 旧数据视为 tags 为空，不自动调用模型回填 |
| memories.scope_key，默认空字符串 | 保存作用范围；公开 API 映射为 meta.scope，JSON 不重复存 scope | 全局为空；工作流 scope 由宿主验证 |
| tombstone 的身份扩展为 scope_key/kind/key | scope 与 key 分离后仍保持遗忘隔离 | 必须和 scoped key 迁移一起完成，不能只改 memories |
| memory_commits(operation_id, input_hash, result_json, committed_at) | 原子提交回执与幂等 | 与 plan/apply 接入一起新增，不能用进程内缓存代替 |

tags 首版放一个 JSON 字段即可，暂不增加标签关系表和向量 payload 副本。使用 JSON 成员查询得到合格 ID，再传给内存向量检索；这会有扫描成本，先测实际规模。若成为瓶颈，再将标签独立成索引表，避免两份标签长期双写。

scope 的确定性迁移规则：`goal.<32位ID>.<key>` 拆成 `goal:<ID>` 和 key；`heartbeat.<key>` 拆成 `heartbeat` 和 key；`webhook.<key>` 拆成 `webhook` 和 key。旧 webhook 范围表示既有整体范围，不能凭空推断单个 webhook 身份。无法识别、冲突或遗忘记录缺少有效范围的行进入迁移报告，不扩大成全局记忆。

迁移前必须扫描实际数据，包括历史 kind、schema 允许但当前写入已不用的 recent activation、重复活动记录、scoped 前缀、遗忘记录和未完成写入批次。不能从单元测试推断线上数据干净；也不能把旧 shared kind 一律猜成某个新类别。

具体迁移脚本需提供 dry-run 报告、备份与恢复验证、行数和关系校验。在切换期间暂停记忆写入及维护 worker，处理未完成计划和旧快照后再恢复；既有 ID 与证据引用尽量保持。旧应用仍依赖前缀 key，scope 迁移后不能直接回退旧代码，回滚方式必须与数据恢复一起审计。线上执行等待另行指令。

meta 和 scope 必须进入快照指纹及读写投影。标签修改更新 updated_at，使并发旧计划失效；标签不进入 embedding 正文，单纯改标签不要求重新编码。过滤读取当前元数据，不能依赖旧向量缓存的标签。scope/activation 变化需要更新索引资格；现有触发器没有 meta/scope 字段，迁移时要明确补齐相应失效逻辑。新旧正文模板不因新增标签而改变。

## 后续里程碑及验收

| 里程碑 | 范围 | 必须交付的验证 |
| --- | --- | --- |
| 8：受控 meta 与过滤 | 标签目录、元数据校验、tags 持久化与前置过滤；范围仍保留旧语义 | 未标注记录可普通召回；显式过滤不越界；无关高分项不能挤占符合标签的候选；标签改变不重编码 |
| 9：写入计划与提交 | 有界候选、plan/apply、证据边界、幂等回执；统一两条合并路径 | 同义/矛盾/条件差异/不同主体/遗忘后重新添加；预算溢出；错误模型 ID；幂等重试；共享事务回滚 |
| 10：scope 与 key 分离 | 显式 scope、tombstone 身份迁移、宿主验证；不扩展工作流种类 | 同 key 不同 scope 不互相覆盖或遗忘；旧前缀迁移无误；恢复演练与未完成批次处理 |
| 11：移除兼容代码 | 删除旧导入 re-export、Store 转发方法和临时别名；业务 adapter 保留 | 全仓旧路径检索为零；独立加载 memory；全量 Python 回归；相关前端变更则另跑构建 |

每个里程碑完成后 commit 并停下来汇报。8—10 涉及 schema 的部分需要先审阅具体迁移产物；本表不授权线上执行。

模型质量评测要覆盖中文无词面重合、同义重复、否定变化和条件差异，分别记录召回命中率与误合并；对比现有实现再设验收阈值。现有模拟测试不能替代此项。需要标注的是候选是否包含正确事实、决策是否保持含义、是否只注入有限记忆，而不只是返回了多少条。

本轮只交付方案。审计通过后的第一步是里程碑 8 的标签目录、过滤协议与迁移草案；不同时推进 scope 迁移和写入模型改造。
