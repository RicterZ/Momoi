# Memory 写入计划与提交（Milestone 9A）

本阶段把用户请求的决策校验和提交移入 `src/momoi/memory/writing`，提供 `await memory.plan(...)` 和同步 `memory.apply(...)`。Momoi 保留模型适配、用户身份认证、事件原文复核，以及后台回合和日志管理。

**本阶段不是整个 Milestone 9 的完成。** 有界去重召回、遗忘候选检查、维护合并统一、noop 追加证据和计划内纯标签修订留在 9B。当前 planner 仍由宿主提供候选与模型调用，不会自动完成 mem0 式语义判重，也没有提供 `add(text)`。

**Schema 从 34 升为 35，新增持久回执表。新版 Store 启动会自动执行迁移；本轮未连接生产库或执行线上迁移。** 部署前应审阅迁移、暂停写入、处理在途批次并保存可恢复的持久备份。

## 调用顺序

宿主先认证用户事件，将原文和来源 ID 交给库。下面代码的 `memory` 是组装好的实例，`planner` 是宿主注入的异步适配器：

```python
requests = [{
    "id": "remember-drink",
    "type": "add",
    "event_id": "owner:123",
    "content": "用户喜欢无糖咖啡",
    "evidence": "喜欢无糖咖啡",
}]
evidence = {"owner:123": "我喜欢无糖咖啡"}

plan = await memory.plan(requests, evidence=evidence, planner=planner)
# 宿主在外层事务内重新认证/核对来源原文后调用：
result = memory.apply(plan, operation_id="owner-memory:batch-123")
```

请求字段固定为 id、type、event_id、content、evidence，可额外包含整数 target_id。type 为 add、replace、forget。引用必须是提供原文的连续子串，每个请求 ID 唯一。库只检查结构和原文关系，不能凭 event_id 或字符串 `owner` 判断身份。

`planner` 也可在 `Memory(database, planner=...)` 初始化时注入；单次参数优先。没有适配器时 plan 报错。库不会在数据库事务打开时调用 planner，避免在持有写锁期间等待模型。

适配器接受 `PlanningContext`：requests、evidence、snapshots。上下文是调用方输入的深拷贝。适配器可以从宿主补充经过认证的 evidence 和读取到的 snapshots，但不能改写最初请求或其原始证据。不要把这个对象直接交给不受信任的插件修改。

适配器返回以下形式的模型决定；这个例子只展示协议，不表示无目标就可以跳过重复检查：

```json
{
  "decisions": [{
    "operation_ids": ["remember-drink"],
    "action": "write",
    "reason": "用户明确提供的饮品偏好",
    "target_ids": [],
    "memory": {
      "kind": "preference",
      "key": "drink.coffee",
      "content": "用户喜欢无糖咖啡",
      "activation": "recall",
      "expires_at": null,
      "meta": {"tags": ["food_drink"]}
    },
    "evidence": [{"event_id": "owner:123", "quote": "喜欢无糖咖啡"}]
  }]
}
```

每个请求必须恰好被一个决定解决。`write` 的 target_ids 为空表示新增，一个目标表示替换，多个目标表示合并；库创建新记录并用 superseded_by 替代旧版本，继承证据。`forget` 要求至少一个当前目标，写 tombstone。`noop/defer` 不修改记忆，但仍保存提交回执。标签与 activation/key 沿用现有规则，scope 尚未独立。

## 规划和提交的边界

| 步骤 | 记忆库 | Momoi 宿主 |
| --- | --- | --- |
| 规划前 | 校验请求结构和引用 | 认证用户，提供事件与初始快照 |
| 规划中 | 调用注入的 planner，校验决定 | 调用模型，追加相关快照和经认证的证据，记录模型用量 |
| 生成计划 | 保存不可变 JSON artifact，属性访问返回新副本 | 可审阅 plan；尚未生效 |
| 提交前 | 再校验决定、标签、目标快照和 key 冲突 | 外层事务复核完整事件原文和批次身份 |
| 提交 | 同步写版本、证据、删除标记和幂等回执 | 同一事务完成 batch、turn 和 journal |

库内 plan/review 不写入数据库，检索也不再顺带清除过期记忆。宿主 planner 的模型用量和会话日志仍按既有流程持久化。过期记录由查询条件排除，物理清理由原有显式维护入口承担。

`MemoryPlan.payload_json` 可保存/恢复为 `MemoryPlan(serialized_json)`；它不是授权凭据。apply 不信任它曾通过模型审阅，会重新校验内容。调用方仍需认证来源。完整快照指纹包含 meta；计划中的任意已读目标变更、被遗忘或被替代，都拒绝旧计划，而不是在写锁内重新请求模型。

同一个 operation_id：

- 相同计划重复提交，返回已存结果，不重写记忆、不追加重复证据。
- 不同计划报 `memory_operation_id_conflict`，包括正文、标签、请求、引用或快照变化。
- 即使原目标后来被修改、删除，成功计划的重试仍返回原回执，不恢复旧内容。

摘要采用规范化 JSON，覆盖请求、原文、快照和决定；JSON 空白或字典顺序不影响幂等。回执和记忆变更原子落库，外层失败则一起回滚。它不提供跨进程语义去重，也不替代并发快照检查。

## Momoi 已接入哪里

[用户请求工作流](../src/momoi/runtime/workflows/memory_operation/workflow.py) 已通过 `memory.plan` 调用宿主适配器。`memory_operation_finish` 只结束模型审阅，返回 `state="planned"`；模型流程返回后，[Store 业务适配层](../src/momoi/storage/memory/memory_operations.py) 校验原文并调用 `memory.apply`。计划错误可在模型工具循环中修正；提交时的快照/原文冲突则退出本轮，由现有队列重试重新规划。

旧 parse_decisions 和 Store 的 decisions/snapshots 调用形式暂时转发到库，最后的兼容清理阶段再删除。维护工作流仍使用原来的 replace/merge 路径，不能把它描述成已经统一。

## 审阅迁移

注册迁移只创建下表，既有记忆、证据、标签和索引数据不改写：

```sql
CREATE TABLE IF NOT EXISTS memory_commits (
    operation_id TEXT PRIMARY KEY,
    input_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    committed_at REAL NOT NULL
);
```

在仓库根目录，对版本 34 的本地数据库副本演练，路径替换为副本实际位置：

```bash
uv run --locked python scripts/check_memory_metadata_migration.py /absolute/path/to/momoi-copy.sqlite3 --stage commits
```

[演练脚本](../scripts/check_memory_metadata_migration.py) 复用上一阶段的只读备份、迁移、重复执行和恢复检查。commits 模式将 meta_json 也计入旧数据摘要。只执行所选阶段的迁移，不顺带执行后续迁移；默认 metadata 模式仍只演练 33→34。源文件不会被改写，临时备份会删除，不能用作生产回滚备份。

## 验证与下一步

专项测试覆盖独立加载、规划只读、非法模型输出、标签校验、完整原文复核、快照冲突、同 ID 计划冲突、跨连接重复提交，以及日志失败时的整体回滚。迁移测试覆盖带标签旧记录保留、DDL 失败回滚、重复运行和备份恢复。没有调用真实模型，不据此判断同义事实是否会被正确合并。

验证结果：36 项新增专项测试通过。以 `a038a893` 加本阶段文件创建隔离副本，在 Python 3.12.6 下运行 `make test`：1333 项测试、289 个子测试通过；1 项 Dashboard 静态资源测试因副本未构建前端而按既有规则跳过，4 条现有 Starlette/httpx 弃用警告。其他工作区改动未纳入该验证或本阶段提交。

9B 将把每事实候选、批次总候选和正文预算落实到写入规划，纳入 always/scoped 和遗忘候选，处理编码失败的保守决策，并统一维护合并与用户写入。之后才是 scope/key 分离和最终兼容代码清理。
