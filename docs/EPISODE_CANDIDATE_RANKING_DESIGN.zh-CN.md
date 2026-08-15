# Episode 候选特征向量排序设计

## 1. 目标

Context Planner 仍负责最终判断 `none / continue / new`。Runtime 只负责把真正可能
相关的 Episode 排在前面，并给出可解释的匹配信号。

本设计解决当前的候选拼接问题：

- 词法搜索、活跃 Episode、最近目录此前按来源顺序拼接，没有统一比较；
- 标题中的宽泛词可能压过最近对话中的真实连续性；
- open loop、最近上下文和 Episode links 没有进入统一排序；
- Episode 体积曾被误认为候选资格，但体积只应影响写入后的 successor 滚动。

## 2. 明确不做

- 不用 Episode turn 数或 raw token 数降低相关性分数；
- 不因 Episode 很大而排除候选；
- 不让 Runtime 根据最高分自动 `continue`；
- 不引入向量数据库或 embedding；
- 不改变 `conversation_search` 的用户工具排序；
- 不增加关键词语义启发式。

## 3. 候选池

默认在最近 30 天中合并：

1. 当前 Owner 文本的词法搜索结果；
2. open / closing Episode；
3. 最近 Episode 目录。

候选池可以大于最终提供给 Planner 的数量。所有来源合并、去重后统一评分，再取前
18 个。来源顺序不再决定最终顺序。

## 4. 特征向量

对查询 `Q`、最近对话 `C` 和 Episode `E` 计算：

```text
v(E,Q,C) = [
  exact_metadata,
  title_overlap,
  topics_overlap,
  entities_overlap,
  summary_overlap,
  open_loops_overlap,
  owner_message_overlap,
  assistant_message_overlap,
  recent_context,
  recent_open_loop,
  linked_context,
  recency,
  status
]
```

所有特征归一化到 `[0, 1]`。

### 4.1 exact_metadata

Episode 的具体 topic/entity/open loop 短语直接出现在当前文本中。它用于提升
《湖之仆从》、offer、加班等明确对象，不使用 title，避免宽泛标题自行获得强加成。

### 4.2 字段 overlap

分别计算 query terms 在 title、topics、entities、summary、open loops 中的覆盖率。
terms 沿用现有 `lexical_units(strict=True)`，不另建分词系统。

### 4.3 owner / assistant message overlap

使用现有 message recall index 返回的匹配消息，计算当前文本与实际历史消息的最大
词法覆盖率。主人原话权重大于 Momoi 回复；它们都高于“Episode 全局出现过某词”，
但仍只是候选信号。

### 4.4 recent_context

最近对话中的 Turn 如果已经属于某 Episode，则该 Episode 获得连续性分数。越新的
Turn 权重越高，Owner Turn 高于 autonomous Turn。它用于识别“它”“继续”“等
offer”等当前正在延续的上下文。

`recent_open_loop` 是 `recent_context` 与“Episode 仍有明确 open loop”的组合特征。
它帮助“我回来了”“继续吧”等没有复用关键词的消息定位刚才的等待状态，但不会单独
让所有 open Episode 获得高分。

### 4.5 linked_context

与 recent-context Episode 有一跳 `continues / supersedes / references` 关系的
Episode 获得较低分数。link 只扩大候选，不等价于当前 Turn 应归档进去。

### 4.6 recency 与 status

按 Episode 最后实际活动时间给小幅衰减分；open / closing 只有很小的辅助分。它们
不能压过明确 topic/entity/message 匹配。

## 5. 初始权重

```text
exact_metadata     0.28
title_overlap      0.22
topics_overlap     0.18
entities_overlap   0.14
summary_overlap    0.10
open_loops_overlap 0.18
owner_message      0.18
assistant_message  0.09
recent_context     0.38
recent_open_loop   0.22
linked_context     0.06
recency            0.04
status             0.02
```

分数不是概率，不要求权重和为 1。

明确对象可以超过 recent context；没有明确对象的承接话则主要依赖 recent context。
Episode 大小不在向量中。

## 6. Planner 输入

Planner 继续看到原有 Episode 字段，并增加：

- `match_score`
- `match_signals`

`match_signals` 只给出贡献最高的少量特征，避免把完整内部向量灌入提示词。完整向量
写 DEBUG 日志用于评估。

提示词必须说明：评分是检索提示，不是归档决定。即使最高分候选存在，Planner 仍可
选择 `new` 或 `none`。

## 7. 失败与降级

- query 为空时主要按 recent context、links、recency 和 status 排序；
- 没有 recent context 时退化为字段匹配与时间排序；
- 某字段为空时对应特征为 0；
- 评分代码失败不应影响原始消息保存，现有 Planner degraded 路径不变。

## 8. 验收场景

至少覆盖：

1. “我准备下班了”：最近的独处 Episode 排在永久“下班前后陪伴”分类之前；
2. “明天还得加班”：明确的周末加班 Episode 排在刚刚的普通下班上下文之前；
3. “薪资好像报低了”：HR 面试/offer Episode 优先；
4. “刚升级了提示词和代码”：本次 Momoi 升级 Episode 优先于旧混合 Episode；
5. “还记得昨晚玩什么”：真实游戏 Episode 与回忆元话题都保留给 Planner，但提示词
   继续禁止把回忆行为本身当作永久 Episode；
6. 两个语义完全相同、体积不同的 Episode 不因 turn 数不同得到不同相关性分数。
