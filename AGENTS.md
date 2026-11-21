# 测试

在仓库根目录运行 Python 测试。优先使用 Makefile 入口；它通过 uv 使用锁定的 `test` 依赖组（pytest、pytest-xdist）。

```bash
# 全量回归，默认 4 个独立进程并行
make test

# 指定文件或筛选测试
make test TEST_ARGS='tests/test_exec_tool.py'
make test TEST_ARGS='-k recall'

# 串行调试；单个测试或少量测试优先串行，避免进程启动开销
make test TEST_WORKERS=0 TEST_ARGS='tests/test_exec_tool.py -x'

# 查看最慢的测试
make test TEST_ARGS='--durations=20'
```

没有 make 时，等价命令为：

```bash
uv run --locked --group test pytest -q -n 4
```

不要再以 `uv run --with pytest pytest -q` 作为默认全量入口：它默认串行，也没有使用锁定的测试依赖组。需要串行运行时，显式设置 `TEST_WORKERS=0`（或 `-n 0`）。

测试必须支持进程隔离：文件和数据库使用各测试自己的临时目录，测试 HTTP 服务使用动态端口，避免共享固定路径或端口。不要为了加速跳过断言或缩减覆盖。

前端改动另运行 `npm run build` 验证构建；Python 测试不替代前端构建检查。修改前端后都要用浏览器渲染截图，随结果一起发出效果图。

# 修复原则

禁止启发式修复：不要针对单个 case 加特判、关键词匹配、硬编码分支或"碰巧能过"的补丁。先定位问题所属的通用机制（数据流、职责边界、协议或抽象缺口），在该层面修复，让同类问题一并解决。

修复涉及业务逻辑取舍或与现有行为、职责边界冲突时（例如改变 Planner / Worker 职责、回合流程、用户可见行为），先向用户说明冲突和可选方案，讨论确认后再实现，不要自行决定。

# 线上部署

Momoi 服务器通过 `ssh -p 22 root@server` 访问。正式部署入口是服务器上的 `/root/docker-services/build/momoi/deploy.sh`（服务器本地文件，不在本仓库内）。

部署前完成必要测试并将待部署提交推送至 GitHub，然后通过 `ssh -p 22 root@server 'bash -e /root/docker-services/build/momoi/deploy.sh'` 执行。脚本会在服务器仓库 `git pull`、使用 `Dockerfile.chrome` 构建 `momoi` 镜像，再到 `/root/docker-services` 执行 `docker compose up --build -d`。不要绕过此入口用容器内补丁、覆盖安装文件或叠加镜像代替正式部署。

部署后核对服务器 Git HEAD、容器使用的镜像、Dashboard `/api/health` 和频道连接日志。健康接口需要 Dashboard JWT，勿将令牌打印到日志。

# 分支清理

分支合并进 `main` 后立即删除：GitHub 远程分支（`git push origin --delete <branch>`）和本地分支都要删。服务器部署仓库只保留 `main`，不建本地分支；部署后执行 `git fetch --prune` 清理已删除分支的跟踪引用。未合并或有开放 PR 的分支不要删除。

# 排查线上记忆

直接查询服务器 `momoi.sqlite3` 时只读打开，并且只把生效记忆当作现状：`memories` 必须使用与代码一致的 `LIVE_MEMORY_SQL`（`src/momoi/memory/storage/records.py`）：`superseded_by IS NULL`、`expires_at` 为空或晚于当前时间，并且不存在 `kind`、`key`、`scope_key` 相同的 `memory_tombstones` 行。删除记忆不会修改原行，只会写入墓碑，漏掉墓碑过滤就会把已删除的记忆误当成生效记忆。`reflection_memories` 同样要排除 `kind`、`key` 相同的 `reflection_memory_tombstones` 行。`events`、`memory_evidence`、`memory_operation_batches` 等是历史记录，不代表当前记忆。引用记忆编号前先确认其仍生效，并区分 `activation`（`always` 常驻注入，`recall` 按需检索）。

# Planner / Worker 职责边界

Momoi 的回合由 Planner 驱动，回复交给 Replyer，外部执行交给 Worker。三者职责严格分离，改动工具面或回合流程时以此为准。

**Planner 是唯一的状态所有者。** 记忆（memory_*、recall）、议程（goal_create / goal_update / goal_finish / goal_cancel）、心跳活动、Worker 派发（create_subagent 等）都只属于 Planner。无论是 owner、heartbeat、webhook、subagent_report 还是 goal 回合，只要是 Planner 在决策，就必须能读写这些状态；不要按回合类型删减 Planner 的状态工具来规避问题。goal 回合的许可集更窄，因为它只处理当前 Goal 的 review，但同样能创建新 Goal 和派发 Worker。

**Worker 是无状态的执行器。** Worker 没有人格、记忆、聊天记录，也不持有 goal。它只能看到 Planner 写下的 task 文本，用 MCP、内置工具和 Skill 完成任务，再通过 subagent_report / subagent_finish 交回进展和结果。`worker_specs()` 不得包含 agenda、memory、reply 等 Planner 状态或对外发言工具；Worker 需要"记住"或"跟进"的事情，在汇报中说明，由 Planner 决定是否写入记忆或建 goal。

**Worker 汇报会自动唤醒 Planner。** Worker 有进展或完成时，运行时会启动 subagent_report 回合，所以 Planner 不需要用 goal 跟进 Worker 结果；拿到结果后在汇报回合里再决定是否告知或另作安排。goal 只表达 Planner 自己的长期意图，例如定时复查、等待外部条件。

**状态修改在回合内暂存，回合结束时原子提交。** Planner 的状态工具只写 `TurnDraft`（goals、memory_operations、memory_access、heartbeat_activity 等），工具返回 `state: staged`。每条回合流程在提交时都必须把 draft 中的全部状态修改和回复放在同一个事务里落库：owner 走 `commit_turn`，goal 走 `commit_autonomous_turn`，heartbeat 走 `commit_heartbeat`，webhook 和 subagent_report 走 `_queue_turn_reply`。新增回合类型或提交路径时，逐项核对 draft 的字段是否都已提交；丢弃 draft 会让工具"返回成功但没执行"。回合内建立后又被 finish/cancel 的 goal 属于撤回，只从 draft 中移除，不落库。

# Prompt cache 稳定性

Provider 的前缀缓存只能保留到第一个不同的字节。Planner、Replyer、Worker 是三条独立的缓存前缀，各自按"越稳定越靠前"排列，改动时不要破坏这个顺序。

## Planner

请求结构为 `system → tools → user[0] → user[1] → transcript → 当前输入`。

- **system 只放跨回合不变的内容。** `_system(planner=True)` 是 Planner 合约与人格，`_system_with_tool_policies` 追加的能力指引与回合类型无关。回合专属合约（`webhook.md`、`heartbeat.md` 等）放进当前输入的 `workflow_contract` 段，不进 system。
- **工具面持续稳定，不做懒加载。** 所有 Planner 回合（owner、heartbeat、webhook、subagent_report、goal）都发送同一份 `conversation_specs()`，回合差异只体现在 `permitted_names(stage)` 和 harness 的 `tool_not_allowed` 拒绝上。不要为某类回合增删或改写 schema，也不要给 Planner 加 `tool_search` / `tool_enable`；需要外部工具时派发 Worker。按渠道或功能开关决定的 schema 对所有回合一致（例如语音开启时 `reply` 的 `mode` 枚举），可用性在执行时判定。
- **user[0]：记忆与议程快照。** `shared_turn_context` 生成的第一条 `context_data_message`，包含 `long_term_memories`、`memory_overrides`、`goal_directory`、`memory_catalog`，带 `cache_control`。记忆取自 transcript 压缩边界上的快照（`transcript_memory_context` 的 `snapshot`），回合之间不重写；之后的记忆变化以 `_memory_change` 事件插到 transcript 中对应历史回合之后，只追加不改前缀。
- **user[1]：episode 目录快照。** `episode_context_message` 给 transcript 窗口之外的历史 episode 建索引，内容经 `transcript_episode_snapshot` 冻结，窗口不滚动就不重算。
- **transcript 只追加。** 历史回合按原生 assistant/tool 交换重放；后台回执、记忆事件只插在整段交换之间，不插进 tool 调用对内部。压缩边界由 `prepare_transcript_context` 统一推进，边界移动时前缀才整体失效。
- **当前输入在最后。** 回合任务（`due_goal`、`current_webhook_task`、`worker_report`、`autonomous_heartbeat`）、召回结果、近期事件、self_state、时间等每回合都变的内容按 `USER_CONTEXT_SECTION_ORDER` 打包进最后一条 user 消息，并带 `cache_control`，供同一回合的后续轮次复用。不要把这类内容塞进 system、user[0] 或 user[1]。

## Replyer

请求结构为 `system → [qq_poke] → user[0] → 对话窗口 → 本次回复目标`。

- **system** 是表达人格（`expression_prompt`）、`REPLYER.md` 和表情目录，与 Planner 的 system 不共享，也不随回合变化。
- **工具固定**，只有 `QQ_POKE_SPEC`。
- **user[0]** 只放长期记忆快照（`long_term_memories`、`memory_overrides`），读取 Planner 的同一份快照但不推动其压缩边界（`track_boundary=False`）；不放 goal 和召回。
- **对话窗口按高水位滚动。** `select_history_rows` 在上次窗口基础上只追加新消息，直到达到 `REPLYER_HISTORY_MAX` 才在回合边界整体轮换到最近 `REPLYER_HISTORY_RETAIN` 条；消息被撤回、编辑或迟到时重建窗口。窗口持久化在 `replyer_history_windows`，跨回合复用同一前缀。
- **本次回复目标**（当前用户消息与 Planner 的 intent）放在最后。

## Worker

- Worker 使用自己的 system（简短合约 + 工具策略 + `tool_index("worker")`）和 `worker_specs()`，与 Planner、Replyer 都不共享缓存；system 和基础工具面在任务之间保持不变，任务差异只放在 task 文本里。
- **懒加载只在 Worker。** MCP 等外部工具通过 `tool_search` 查找、`tool_enable` 加载，`append_visible` 只把新工具插到现有工具之后（最后一个固定工具之前），不改已有工具的顺序和内容。Worker 的启用结果只在本次任务内有效，不持久化到 transcript（`persist=False`）。

# 架构文档

架构设计、解耦方案、阶段计划与设计审查文档仅在本地保留，不纳入 Git。统一放在已忽略的 `docs/architecture/` 下；现有 `docs/YYYY-MM-DD_*.md` 也保持忽略。已跟踪的此类文档使用 `git rm --cached` 取消跟踪，保留本地文件，不使用 `git add -f` 强制提交。面向用户的配置、使用和部署说明不属于这类内部设计文档，仍按原有方式维护。

# Agent 临时文件与日志

Agent 执行任务产生的命令日志、测试输出、调试脚本、抓包、截图和临时诊断文件统一放在 `/tmp`，例如先运行 `mktemp -d /tmp/momoi-XXXXXX` 创建本次任务目录。不要写到仓库根目录、`build/`、`docs/` 或创建 `coscli_output/`、`.momoi-debug/` 等临时目录。避免在临时文件中记录令牌和凭据。

调用会自行生成日志的工具时，显式指定临时输出目录。COSCLI 的 `--disable-log` 不会关闭传输与失败记录，还需将 `--log-path`、`--process-log-path`、`--fail-output-path` 指向 `/tmp` 下的任务目录，或显式关闭对应日志。

模型、可复用下载缓存和正式构建产物仍使用项目约定的 `models/`、`build/`、`dist/`；产品运行日志和 CI 需要上传的诊断产物仍遵循各自规则。清理时不要删除用户数据、凭据、有效构建产物或其他任务正在写入的文件。Windows 执行环境使用系统临时目录。
