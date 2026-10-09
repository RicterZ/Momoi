# Memory 主题标签与过滤（Milestone 8）

本阶段已实现受控标签、持久化、前置过滤和独立候选预算。仍在 Momoi 数据库中存储，不改变 scope/key 语义，不执行线上迁移。

**部署前先审阅迁移：本提交将 schema 从 33 升为 34。启动新版 Store 会自动执行注册的迁移，不能在未准备备份时直接启动新版连接生产库。** 历史记忆保持未标注，不自动调用模型补标签。

## 查询记忆

在已组装好 `self.memory` 的 Momoi agent/context 中：

```python
m = self.memory
results = await m.search("用户喜欢什么饮品")
results = await m.search(
    "用户喜欢什么饮品",
    filters={"tags_any": ["food_drink"], "kinds": ["preference"]},
)
for row in results:
    print(row["id"], row["content"], row["meta"]["tags"])
```

普通搜索不猜主题、不增加分类调用，也不排除未标注记忆。`tags_any` 内部取并集，`kinds` 内部取并集，两个字段之间取交集；省略或空列表不限制。未知字段、标签、kind 和重复值报 `ValueError`。没有满足过滤条件的记录就返回空，不自动扩大范围。`search_literal` 和 `rank` 也接受同样的 filters。

标签目录由宿主提供，位于 [memory_values.py](../src/momoi/storage/memory/memory_values.py)。包括 food_drink、health、work_study、technology、travel、leisure、daily_life、social、communication。库中的 [TagCatalog](../src/momoi/memory/metadata.py) 只校验目录，不依赖这些具体主题。自行组装时使用 `Memory(database, tags=TagCatalog({...}), dense_recall=...)`，其中 database 是已建表且使用 sqlite3.Row 的连接；编码器和模型客户端仍由宿主注入。

## 写入和修改标签

现有后台写入模型在同一次决策中选择 `meta.tags`，校验失败则拒绝提交。最多三个标签，显式空数组表示无标签。旧调用省略 meta 时，新记录为空；替换和合并继承目标标签的并集。并集超过三个时拒绝操作，需要重新审阅并显式选择标签，不能静默裁掉。维护合并也保留标签；其 survivor-ID 规则仍待下一里程碑统一。

底层可信调用方可对已经审阅的标签修订使用：

```python
snapshot = m.snapshots([memory_id])[memory_id]
m.repository.update_meta(snapshot, {"tags": ["food_drink"]})
```

这是存储层接口，不是 agent 的用户授权入口。它校验目录和完整快照，在同一事务内更新 meta/updated_at。相同标签不重复写入；外层事务回滚时标签修改一起撤销。快照指纹包含 meta，即使时间戳相同也能识别标签变化。

旧标签不会因目录变化自动重写；读取历史记录不要求旧标签仍在目录中。新写入或修订仍必须通过当前目录校验。`meta.scope` 尚未开放，传入会报错。

## 过滤和排序边界

数据库按当前可见性和 JSON 标签选择合格 ID，向量快照在 top-k 前限制到这些 ID。标签不复制进向量快照，也不进入 embedding 正文，所以改标签不需要刷新快照或重编码。kind 查询限制也用于候选截断前的 ID 选择；联合 Episode 查询仍共用一次 embedding。

`search` 最终默认、最多返回六条，较小 limit 生效。注入 reranker 时，评分候选最多 24 条；未注入时直接返回评分排序结果。`rank` 是候选评分接口，最多 24 条；宿主上下文回退仍限制六条。向量层可以检索更宽的中间集合，这些向量命中不会全部交给 LLM。

显式非空 filters 不能和外部预计算的 dense_evidence 同时交给 `search`：库无法证明外部候选曾在截断前执行过滤，因此报错；请让 search 自行获取受约束的向量证据。现有无 filters 联合 Episode/记忆路径继续复用预计算证据。编码失败仍可按相同过滤条件进行字面回退。

## 审阅和演练迁移

迁移只有一条增量 DDL：

```sql
ALTER TABLE memories ADD COLUMN meta_json TEXT NOT NULL DEFAULT '{}';
```

[注册迁移](../src/momoi/storage/core/migrations.py) 幂等检查字段。旧 ID、正文、kind/key、证据、删除标记、时间和索引均不改写。不修改 semantic trigger：当前 trigger 只监听正文、身份和激活状态，单纯更新 meta/updated_at 不会令向量失效。

在仓库根目录对**待审阅的本地数据库副本**执行以下命令，将路径替换为副本实际位置：

```bash
uv run --locked python scripts/check_memory_metadata_migration.py /absolute/path/to/momoi-copy.sqlite3
```

[演练脚本](../scripts/check_memory_metadata_migration.py) 只读打开原文件，通过 SQLite backup 生成临时备份，恢复到另一临时库，在该库执行迁移，再从旧备份恢复。它检查 integrity/foreign keys、七个相关表的行数和旧字段摘要、schema 版本与默认空标签，并报告历史 kind、activation 和未完成操作数量。源库必须是版本 33 且没有 meta_json；遇到其他版本直接拒绝，避免顺带演练未审阅的历史迁移。报告只含统计和摘要，不输出记忆正文。临时备份随演练删除，不能代替上线时的持久备份。

上线执行另行授权。执行时暂停记忆写入与维护 worker，先处理未完成批次，保存可恢复的持久 SQLite 备份并验证，再启动新版迁移，核对上述不变量后恢复处理。回滚采用旧代码和迁移前完整数据库备份；上线后新增的数据需另外保存，不能直接恢复旧备份将其丢弃。

## 验证与下一步

专项测试覆盖未知标签拒绝、字段交集、未标注默认召回、截断前过滤、标签实时变化、无重新编码、快照冲突、事务回滚、合并标签以及迁移备份恢复。测试使用模拟向量，不证明真实中文编码器的语义质量。

验证结果：28 项新增专项测试通过；以 `3cf571d8` 加本阶段文件构建隔离副本，运行 `make test`，1297 项测试与 289 个子测试通过。1 项既有 Dashboard 静态资源测试因副本未构建前端而跳过，另有 4 条现有 Starlette/httpx 弃用警告。当前共享工作区的全量运行还遇到其他未提交 embedding 配置改动导致的 endpoint 校验失败，该改动未纳入本阶段。

本阶段结束后停下审阅。下一阶段为 Milestone 9：有界写入候选、库级 plan/apply、幂等提交回执与两条合并路径统一。scope/key 分离和旧兼容代码清理继续留在 Milestone 10、11。
