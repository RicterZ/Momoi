# 记忆写入审计与 transcript 回放（2026-10-09）

夜间记忆整理已移除；手动编辑正文和触发词改为新版本。写入审阅修复了 scope 丢失、候选排序及预算反馈问题。14 种合成场景各运行两次，28 次通过预期检查。测试使用真实 DeepSeek 模型与本地 BGE，数据库隔离，没有运行消息调度或投递。

## 改动与边界

- 移除 memory_maintenance 工作流、提示词、队列接口、配置字段及 `/tidy`。此前入队函数已经是空实现，但每日复盘仍产生待整理记录。schema 39 取消旧 running 整理任务，保留历史任务及审计记录；加载旧配置时忽略退休 stage。每日和每周复盘继续运行。
- Dashboard PATCH `/api/memories/{id}` 调用 repository.replace，正文或 triggers 改变就返回新 ID；完全相同的保存不产生新版本。保留 kind/key、activation、scope、tags、期限及历史证据，旧记录通过 superseded_by 指向新版本。过期 ID 返回 404。前端保存后已有重新加载机制。
- 后台修改生成唯一 dashboard:memory 证据，记录实际改动和时间。它存在 memory_evidence，不进入聊天事件队列。私有审阅能看到该证据，提交时重新核对数据库原文；已有计划遇到手动编辑会因旧快照失效而拒绝提交。
- memory_operation 的 metadata 操作仍按现有设计原地更新标签/触发词并追加证据；此次“手动编辑版本化”指 Dashboard 用户编辑。删除仍使用 tombstone；没有增加历史版本浏览或回滚 UI。

## 检索策略

写入侧使用私有 memory_operation_search，区别于前台 memory_search/recall。

1. 宿主先按请求正文做语义和字面召回。指定目标及完全同文候选优先保留；scope 在检索前限制候选池，也明确出现在模型输入中。
2. 每条查询的语义列表按余弦相似度排序；字面列表按关键词覆盖比例排序。交替取两个列表、按 ID 去重，每条查询最多八个候选，避免所有字面命中挤掉同义事实。
3. 本轮累计最多 32 条候选、正文估算 8000 tokens；不能容纳新候选时标记 candidate_budget_exceeded，禁止继续 write。指定目标超预算或向量检索故障会 defer。相似度仅用于选候选，合并仍由审阅模型依据证据决定。
4. 模型需要补查时，query 同时用于向量检索和空格分隔的字面 OR 检索。没有自动中文分词；未启用向量时要用短关键词。返回 memories 仅是新增候选，candidate_ids 包含已有候选，重复查询不是翻页。
5. 不提供全量记忆给 LLM。底层字面候选仍扫描当前 planning_rows，适合现有单用户轻量规模；没有增加数据库全文索引或额外 reranker。

提示词同步明确了：请求范围与记录范围的区别、旧请求不能覆盖较新手动修正、修改触发词不等于重新确认正文、noop 无目标时应省略字段而非填空数组、遗忘前旧证据必须 defer，以及临时状态/单次经历不写长期记忆。正文由原 64 行缩到 43 行。

## 构造的对话与真实请求

完整输入在 [memory_operation_transcripts.json](../tests/fixtures/memory_operation_transcripts.json)。每个场景包含用户/助手 transcript、初始记忆、请求以及结果断言；不是从线上用户聊天复制的。

| 场景 | 构造目的 | 两次实际决定 |
|---|---|---|
| new_fact | 记录平常喝无糖咖啡 | write / write |
| paraphrase | “不放糖”与“无糖”判重 | noop / noop |
| correction | 加糖偏好改为无糖，替代旧版本 | write / write |
| merge_duplicates | 两条重复咖啡偏好合并为一条 | write / write |
| independent_rules | 简短表达与外部发送需确认分别保留 | write / write |
| forget | 忘记口味，不创建替代事实 | forget / forget |
| assistant_suggestion | 助手提议跑步，用户尚未采纳 | noop / noop |
| scope_isolation | Heartbeat 规则另在全局生效，保留原范围 | write / write |
| manual_correction | 较早请求不能把手动改成的无糖覆盖回甜咖啡 | noop / noop |
| forgotten_old_evidence | 遗忘前的旧请求不能恢复记忆 | defer / defer |
| literal_only_search | 关闭向量，模型补查拿铁后更新原记录 | write / write |
| trigger_metadata | 呼唤记忆增加“喵”触发词 | metadata / metadata |
| single_episode | 偶尔买一次拿铁，不推断长期偏好 | noop / noop |
| negated_preference | 保留“不喜欢甜咖啡”的否定含义 | write / write |

验证使用 deepseek-flash（配置 thinking=high、temperature=0.6）和 BAAI/bge-small-zh-v1.5。28 次完成共 44 次模型请求。校验同时检查决定类型、有效记忆数量、旧版本是否被替代、独立记录是否保留、修正内容、触发词和必要补查。它们验证这些合成案例，不是对所有表达的正确率保证。

修改前，三个确定性回归分别复现字面噪声挤占、覆盖度丢失、预算耗尽不报告。首轮真实模型回放中，新增、纠正和字面补查通过；scope 与手动修正用例未能在回放预算内完成，其中手动修正已识别新证据，却提交了不合法的 noop 空数组。修订提示词后的一轮中，旧遗忘证据得到安全的 noop，但不符合预期 defer；补明规则后，两次正式重复均为 defer。上述过程保留在本地输出，未将失败试验混报为通过。

## 复现

在仓库根目录，使用具有启用 LLM 和 embedding 的 provider 配置。此命令会调用配置的模型服务，生成新临时数据库；不读取应用记忆数据库，也不启动频道或 MCP。

```bash
uv run --locked python scripts/replay_memory_operation.py \
  --providers /tmp/momoi-planner-replay/providers.yaml \
  --output /tmp/momoi-memory-operation-new-run \
  --repeat 2

make test TEST_WORKERS=0 TEST_ARGS='tests/test_memory_candidates.py tests/test_manual_memory_versions.py tests/test_memory_operation_replay.py tests/test_memory_operations.py tests/test_memory_write_host.py tests/test_memory_xml.py'
```

`--providers` 指向本机已有配置；本次配置文件不随仓库发布。输出目录必须是新的；每个场景的 result.json 保存完整模型输入、工具参数、返回上下文、最终决定及断言结果，summary.json 汇总通过情况。普通 pytest 使用脚本化模型，不联网；真实模型测试只在显式运行回放脚本时发生。

本次本地结果：`/tmp/momoi-memory-work/verified-results.json`；完整请求位于同目录的 `final/` 与 `extra/`。全量测试 1423 passed、1 skipped、260 subtests passed，前端构建通过。为避开其他会话正在修改的频道重命名，回归在 HEAD 加本次改动的干净临时快照运行；未把其他会话的文件带入提交。

## 尚未覆盖

没有做生产库迁移、部署或 COS 发布。没有全库历史去重；移除整理后，旧重复记录只在写入相关事实时被比较。bounded recall 可能漏掉表达差异很大的旧事实，未配置向量时尤其如此；需要模型主动补查，不能宣称全局判重完备。语义判断与证据新旧冲突仍由模型审阅，宿主负责证据真实性、scope、版本并发、遗忘时间边界及原子提交。
