# 交互表达评估

本目录评估真实 Replyer 的表达，不评估 Planner、检索、真实渠道投递或长期记忆。
所有用例从零编写，均为虚构、无真实身份的日常交互；不读取应用配置、聊天记录、
私人 SOUL、记忆库或现有生产场景。新用例也不得包含真实用户资料、凭据或私聊原文。
这不是对真实记录的自动脱敏器，请勿直接导入私人数据。

## 运行

仓库根目录执行，无需新增依赖：

```bash
# 离线烟测：真实 Replyer + 固定占位模型；不代表语义质量通过
make eval
make eval EVAL_ARGS='--case brief_choice'

# 显式启用真实模型；配置通过环境变量提供，不加载项目 providers.yaml
# MOMOI_EVAL_BASE_URL、MOMOI_EVAL_API_KEY、MOMOI_EVAL_MODEL
make eval EVAL_ARGS='--live'

# 另配 MOMOI_JUDGE_BASE_URL、MOMOI_JUDGE_API_KEY、MOMOI_JUDGE_MODEL
make eval EVAL_ARGS='--live --judge'

# 测评基础设施测试，不调用外部模型
make test TEST_WORKERS=0 TEST_ARGS='tests/test_eval_runner.py'
```

首版仅支持兼容 OpenAI Chat Completions 的接口，使用项目已有 provider。
BASE_URL 按项目 provider 的接口约定设置。不要将密钥写入数据集、命令参数或 Git。
每次请求最多 1024 输出 token、60 秒超时、无自动重试；串行执行。
真实运行会产生模型费用，Judge 会额外接收虚构用例、测试人格和生成的回复。

## 结果与判定

每次自动新建 `/tmp/momoi-eval-*`（Windows 使用系统临时目录），
输出 `manifest.json`、`results.jsonl`、`summary.json`、`report.html`。每条用例另建临时数据库并自动删除。
不连接聊天渠道、MCP 或调度器；QQ 戳一戳只走 Replyer 的 dashboard 无副作用分支。
记录模型可见输入、文本回复、工具调用、用量和耗时，不保存思考或协议续接数据。
模型端点、凭据和异常原文不写入结果。生成文本本身仍应按测试产物保管，不自动上传。

- 确定性检查：非空、用例字符上限。字符上限是场景约束，不是通用表达质量指标。
- Judge：按用例 criteria 分项返回 `pass`、`fail`、`insufficient_evidence` 和理由。
- 未启用 Judge 时，成功执行标为 `unscored`，不能当成语义通过。
- 执行或 Judge 输出校验失败记为 `error`，不计为能力失败，也不忽略。
- 有失败、错误或证据不足时返回非零退出码；烟测成功返回零。

manifest 保存 Git HEAD、dirty 标记、模型名、生成参数及数据/提示词 hash。
dirty 标记不是源码快照；正式版本比较请在干净提交上运行，并保留完整输出目录。
系统临时目录会被清理，需要长期保存时应转存到明确授权的私有 artifact 存储。

## 新增用例

编辑 `datasets/interaction.jsonl`，每行一条：唯一 `id`、`tags`、`input`、`intent`、
`reference`、`history`（虚构的 user/assistant 对话对）、`split`、`expected`、
非空的 `criteria` 对象和正整数 `max_chars`。`expected` 包含 `must_convey` 与
`must_not_convey` 语义列表；criteria 的键是稳定指标名，
值是可观察、可核对的验收标准；不提供唯一标准台词，不因亲密或冗长自动加分。
测试人格与 Judge 提示词分别放在 `prompts/soul.md`、`prompts/judge.md`。

当前包含 60 个合成场景：指令遵循、真实性、不确定性、自然表达、引用指令边界和
历史衔接。包含 6 个回归、36 个开发、18 个保留场景；保留集公开存放，不是隐藏测试集，
请勿反复针对它调参。历史衔接提供固定合成历史，不代表完整动态多轮 Agent 回放。

## 重复运行、基线与报告

```bash
make eval EVAL_ARGS='--split dev --repeat 3'
make eval EVAL_ARGS='--live --judge --split holdout --repeat 3'
make eval EVAL_ARGS='--baseline /tmp/momoi-eval-PREVIOUS'
```

打开输出目录的 `report.html` 查看分类计数、分项有效判定通过率、场景稳定性、阶段耗时、
用量和失败优先详情。基线按场景与重复序号配对，展示前后回复；用例或评分模式变化时
标为不可比。更换模型/Judge 版本需要结合两个目录的 manifest 人工确认，不自动认定公平对比。
HTML 对模型文本转义，不使用外部脚本或 CDN。

Replyer 耗时是各模型调用累计时间，Judge 单独计时，整体耗时包含隔离数据库等开销。
失败尝试也计入耗时并单独报告错误阶段；不采集 TTFT，也不推断真实消息投递耗时。
真实运行报告 P50，满 100 次记录才展示 nearest-rank P90/P95；这只是展示阈值，不保证
统计充分。烟测不展示分位数。重复次数不等于独立场景数。无价格配置，不估算费用。

## Judge 校准样本

`datasets/judge_calibration.jsonl` 包含 12 个好/坏合成回复及目标判定，每条只校准一个
明确指标。这些标签是编写时给出的 editorial 标注，**未经主人或独立人工复核**，
不能当成人工金标准或证明 Judge 已经可信。

配置 `MOMOI_JUDGE_*` 后，显式执行以下命令会调用付费 Judge：

```bash
uv run --locked python -m evals.calibrate
```

结果写入独立 `/tmp/momoi-eval-calibration-*`，报告有效标注一致率、违规漏判数、
误报数、错误与证据不足。分母及未判定数量一起保留，不静默排除错误。
暂不实现离线重评分、平台上传和综合分数。
