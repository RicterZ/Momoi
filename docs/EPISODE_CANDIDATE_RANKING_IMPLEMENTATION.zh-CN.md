# Episode 候选特征向量排序实施文档

## 1. 修改范围

- `runtime/context_candidates.py`
  - 合并更大的候选池；
  - 计算特征向量、加权分数和主要信号；
  - 统一排序后截取 Planner 候选。
- `storage/store.py`
  - 根据 recent Turn IDs 返回直接 Episode 连续性和一跳 link 连续性。
- `runtime/turns.py`
  - 先读取 recent conversation，再排序 Episode 候选；
  - DEBUG 日志记录候选分数和完整特征。
- `prompts/context_planner.md`
  - 明确候选分数只是提示，不能强制 `continue`。

不修改数据库 schema，不生成 embedding，不改变 Episode 尺寸滚动。

## 2. 数据流

```text
Owner messages
  + recent conversation turn ids
  + recent Episode pool
        ↓
field/message/context/link features
        ↓
weighted score
        ↓
top 18 compact candidates
        ↓
Context Planner: none / continue / new
```

## 3. 可观测性

新增 DEBUG 事件：

```text
episode_candidates_ranked
```

每个候选记录：

- id/title/status；
- score；
- 各特征值；
- 主要贡献信号。

不记录原始消息正文。

## 4. 离线评估

使用生产 SQLite 一致性副本：

```text
.production-shadow/candidate-ranking.sqlite3
```

副本只用于本地只读评估，不进入 Git。评估比较旧拼接顺序和新排序在真实场景中的
top-N，并记录到本文。

初次离线结果：

- “我准备下班了”：最近的独处/open-loop Episode 从旧拼接中的弱位置升到第 1；
- “明天还得加班”：周末加班 Episode 保持第 1；
- “薪资好像报低了”：HR 面试/offer Episode 第 1；
- “刚升级了提示词和代码”：两个真实升级候选保持前 2，旧混合 Episode 仍可见；
- “还记得昨晚玩什么”：回忆元话题和真实《湖之仆从》Episode 同时进入前 2，交给
  Planner 按协议选择真实经历；
- 相同内容但 turn 数不同的 Episode 得到相同特征，证明体积不参与相关性评分。

完整测试：225 项通过。

## 5. 发布判断

上线前必须：

- 完整单元测试通过；
- 真实数据库场景中明确对象和 recent continuity 均能进入 top candidates；
- 没有按 Episode 大小惩罚；
- Planner 输入规模没有显著增长。

上线后观察：

- `episode_candidates_ranked`；
- Planner 选择的 Episode；
- `new/continue/none` 分布；
- 错误继续永久分类 Episode 的比例。
