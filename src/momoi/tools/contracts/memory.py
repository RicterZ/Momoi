from typing import Any

from ...storage.memory.catalog import MOMOI_MEMORY_TAGS
from ...memory.storage.records import MEMORY_KINDS


MEMORY_TOOL_POLICY = """### 记忆工具

只有经过确认的用户证据支持新增、修正或遗忘记忆时，才使用 memory_operation。不要逐条记录消息，也不要只为复述旧记忆而查询再写回。待处理请求不等于已确认的事实或已完成的删除。
scope=current_state 只用于更新或删除已有的临时状态；新状态由后台状态维护创建。
长期记忆的分类、激活条件和去重由后台审核处理。
"""


MEMORY_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "memory_search",
        "description": (
            '搜索已确认记忆。默认搜索全局；只有明确需要缩小范围时才设置 filters，避免漏掉未标标签的相关记忆。'
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        '简洁的主题；使用 `|` 表示同一主题的替代名称。'
                    ),
                },
                "filters": {
                    "type": "object",
                    "additionalProperties": False,
                    "description": "各字段之间取交集；省略或空数组不限制主题或分类。scope 默认全局，不跨作用域搜索。",
                    "properties": {
                        "tags_any": {
                            "type": "array", "uniqueItems": True,
                            "maxItems": len(MOMOI_MEMORY_TAGS.tags),
                            "items": {"type": "string", "enum": sorted(MOMOI_MEMORY_TAGS.tags)},
                            "description": "主题之间为 OR，例如 [health, food_drink] 匹配健康或饮食。会排除未标这些标签的记忆。目录："
                                + "; ".join(f"{key}: {value}" for key, value in MOMOI_MEMORY_TAGS.tags.items()),
                        },
                        "kinds": {
                            "type": "array", "uniqueItems": True, "maxItems": len(MEMORY_KINDS),
                            "items": {"type": "string", "enum": sorted(MEMORY_KINDS)},
                            "description": "记忆性质之间为 OR；不是主题标签。",
                        },
                        "scope": {
                            "type": "string", "maxLength": 200,
                            "pattern": "^(?:|heartbeat|webhook|goal:[^\\s]+)$",
                            "description": "空字符串表示全局；也可明确指定 heartbeat、webhook 或已知的 goal:<任务ID>。",
                        },
                    },
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 6,
                    "default": 6,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "episode_search",
        "description": (
            '按关键词或时间搜索已归档的话题。返回分页摘要及证据位置，而非原始消息。'
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        '简洁的主题，可选带 `|` 别名；为空时则按时间顺序浏览时间范围。'
                    ),
                },
                "time_range": {
                    "type": "object",
                    "description": (
                        '窗口；默认为 30 天。仅在旧历史记录重要时使用 all。'
                    ),
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": ["recent", "range", "all"],
                        },
                        "days": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 3650,
                        },
                        "from": {"type": "string"},
                        "to": {"type": "string"},
                    },
                    "required": ["kind"],
                    "additionalProperties": False,
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "default": 5,
                },
                "cursor": {
                    "type": "integer",
                    "minimum": 0,
                    "description": '偏移量作为 next_cursor 返回。',
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "episode_read",
        "description": (
            '读取话题的分页文本时间线：USER/ASSISTANT 表示原始对话，默认不附带执行记录；message_refs 保留引用目标和续读位置。消息正文截断时按 message_id / next_content_offset 续读。使用 execution_cursor=0 按时间遍历全部执行 Turn，按 next_execution_cursor 续读；使用 turn_id 和 after_sequence 对单个 Turn 的历史工具调用进行分页；截断参数通过返回的 arguments_read 再调用 episode_read 读取，结果过大时用 result_ref 和 read_tool_result 读取完整快照；执行详情以 TOOL_CALL/TOOL_RESULT 编号配对，按可用日志时间与序列穿插对话；ASSISTANT_NOTE 是内部评论，时间是记录时间，并非发言或完成证明。不展示 end_turn 等控制调用。'
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "episode_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                    "description": '来自 recall 或 episode_search 的话题 id。',
                },
                "execution_cursor": {"type": "integer", "minimum": 0,
                    "description": "从 0 开始遍历全部执行 Turn；续读 next_execution_cursor，不能与其他游标或时间范围组合。"},
                "turn_id": {"type": "string", "description": '读取该 Turn 在话题内的执行证据。'},
                "tool_call_id": {"type": "string", "minLength": 1, "description": "从 arguments_read 复制，与 turn_id 和 after_sequence 一起读取完整工具参数。"},
                "after_sequence": {"type": "integer", "minimum": 0, "description": '执行游标作为 next_after_sequence 返回；需要 turn_id。'},
                "before_ordinal": {
                    "type": "integer",
                    "minimum": 2,
                    "description": (
                        '旧页面的 next_before_ordinal；最新页面则省略。'
                    ),
                },
                "time_range": {
                    "type": "object",
                    "description": (
                        '精确的消息时间窗口；因原始消息冗长，请优先使用较窄的范围。'
                    ),
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": ["recent", "range", "all"],
                        },
                        "days": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 3650,
                        },
                        "from": {"type": "string"},
                        "to": {"type": "string"},
                    },
                    "required": ["kind"],
                    "additionalProperties": False,
                },
                "message_id": {
                    "type": "integer",
                    "minimum": 1,
                    "description": ('消息 id 随 next_content_offset 返回。'),
                },
                "content_offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": ('同一消息 id 的 next_content_offset。'),
                },
            },
            "required": ["episode_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "memory_operation",
        "description": (
            '更改持久化记忆（默认 scope=memory）或临时状态（scope=current_state）。对于现有临时状态，使用 current_state：replace 用于更新，forget 用于删除。新状态只能由后台状态维护添加；此范围内禁止 add。请使用现有的主题/键；缺失或过期的维度无法通过 replace 创建。为 replace 提供 ttl_seconds，forget 则省略。若无新鲜证据，请勿续期。仅记录当前用户输入直接支持的临时事实或行为，而非您的猜测。在 current_state_maintenance 中使用 current_state_finish。以下审查规则仅适用于持久化记忆：运行时附加召回的记忆和对话；私有审查在该 Turn 提交后运行。接受不意味着更改已生效。请勿重复已接受的请求。'
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string", "enum": ["add", "replace", "forget"],
                    "description": '操作类型，而非记忆类别。add 仅创建持久化记忆请求；replace 更新或更正现有事实；forget 删除已结束、被证伪或明确不需要的事实。对于 current_state，replace 原子性地替换现有主题/键的值和 TTL；无需单独的 forget。',
                },
                "content": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 2000,
                    "description": 'add/replace 的新范围化事实；forget 则受其约束。保留用户的极性、对象、条件和持续时间。持久化记忆正文只写结论，不附证据、记录日期或纠正经过；事实本身必要的日期保留，依据单独填 evidence。',
                },
                "evidence": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 500,
                    "description": '来自当前认证用户消息的精确连续引用。',
                },
                "scope": {"type": "string", "enum": ["memory", "current_state"],
                          "description": '存储类别：memory（默认）用于持久化事实、偏好、规则或关系；current_state 仅用于更新或删除现有临时事实或限时行为（最长 24 小时）。持久化记忆种类和激活由后台审查分类，而非 type 参数。'},
                "subject": {"type": "string", "minLength": 1, "maxLength": 128,
                            "description": 'current_state 仅限：用户、助手或已建立的实体。'},
                "key": {"type": "string", "minLength": 1, "maxLength": 64,
                        "pattern": "^[a-z][a-z0-9_.-]*$",
                        "description": 'current_state 仅限：不带主题前缀的维度；复用现有键。对于 owner.diet.intake 使用 subject=owner，key=diet.intake。replace/forget 需要现有维度；不允许添加。'},
                "ttl_seconds": {"type": "integer", "minimum": 1, "maximum": 86400,
                                "description": 'current_state replace 仅限：本次写入中由证据支持的剩余持续时间。'},
                "target_id": {
                    "type": "integer",
                    "minimum": 1,
                    "description": '持久记忆：仅当本回合已显示可选的 memory_id 时使用；未知时省略。当前状态请使用 subject/key 替代。',
                },
            },
            "required": ["type", "content", "evidence"],
            "allOf": [{
                "if": {"properties": {"scope": {"const": "current_state"}}, "required": ["scope"]},
                "then": {
                    "required": ["subject", "key"],
                    "properties": {"type": {"enum": ["replace", "forget"]},
                                   "target_id": False, "content": {"maxLength": 512}},
                    "allOf": [{
                        "if": {"properties": {"type": {"const": "forget"}}},
                        "then": {"properties": {"ttl_seconds": False}},
                        "else": {"required": ["ttl_seconds"]},
                    }],
                },
                "else": {"properties": {"subject": False, "key": False, "ttl_seconds": False}},
            }],
            "additionalProperties": False,
        },
    },
]
