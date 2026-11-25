{{HEARTBEAT}}

## 工具

- 先单独调用 heartbeat_begin：pick 手选下方目录中的任务，random 按配置的概率随机抽取，custom 做目录之外的事，rest 休息。选定后本轮不再更换；返回任务详情后再执行。
- 需要外部工具时用 create_subagent 派发 Worker。
- 结束前调用 heartbeat_activity 记录实际活动或休息；派发了 Worker 时不在本轮记录，等 Worker 结果回来后再提交。

## 心跳任务

{{HEARTBEAT_ACTIVITIES}}
