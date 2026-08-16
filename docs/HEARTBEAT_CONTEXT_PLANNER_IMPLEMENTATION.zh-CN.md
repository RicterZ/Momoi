# Heartbeat 上下文规划实施记录

## 目标

Heartbeat 不再先按上一次活动加载记忆，再让主模型临时选择新活动。新的顺序是：

1. 专用 Heartbeat Context Planner 选择本次具体活动；
2. Planner 最多给出两条初始召回查询；
3. Runtime 复用 Owner Turn 的检索组装逻辑，加载正式 Memory、Reflection Memory
   和 Episode 摘要；
4. 主 Heartbeat 模型在已经装好相关上下文后执行活动。

工作区 `HEARTBEAT.md` 仍然是主人自定义的活动指导；通用规划协议位于内置
`heartbeat_planner.md`。

## 协议

Planner 只能调用 `submit_heartbeat_plan`：

```json
{
  "version": 1,
  "activity": {
    "intent": "本次要做的具体活动",
    "reason": "简短理由",
    "recall_queries": ["需要预先加载的规则、状态或共同经历"]
  },
  "uncertainty": []
}
```

`recall_queries` 可以为空，最多两条。休息、自由思考等不依赖历史资料的活动不应
为了形式而搜索。

## Runtime 行为

- Planner 输入包括当前自我状态、上一次活动、最近对话、最近 Episode 目录、
  当前 agent Goals 和工作区 Heartbeat 指导。
- Planner 协议错误时修正重试一次；仍失败则继续原活动且不进行查询召回。
- 初始召回由 Runtime 完成，主 Heartbeat 工具集中不再提供 `memory_search`。
- 主模型收到 `<heartbeat_plan>` 以及按查询选出的 Memory、Reflection 和 Episode
  摘要，然后使用实际活动工具。
- 日志增加：
  - `heartbeat_plan_complete`
  - `heartbeat_plan_invalid`
  - `heartbeat_plan_degraded`
  - `heartbeat_recall_complete`
  - DEBUG 级别 `heartbeat_recall_selected`

## 验收场景

当 Planner 选择“浏览微博关注流”，并查询“微博登录错误报告规则”时，Runtime 应在
主 Heartbeat 模型调用微博工具之前加载
`shared.weibo.login_expired_notify`。主模型请求中应同时出现 `<heartbeat_plan>` 和
该正式 Memory，且工具列表不依赖 `memory_search`。

## 测试

- Heartbeat Planner 协议解析和降级；
- 工作区 `HEARTBEAT.md` 进入 Planner 输入；
- Planner 查询命中正式 Memory，并在主模型执行前注入；
- Heartbeat 主模型工具列表不含 `memory_search`；
- 既有静默、发消息和创建 agent Goal 的 Heartbeat 流程保持可用。
