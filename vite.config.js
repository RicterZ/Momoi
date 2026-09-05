import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

function previewThinkingCalls() {
  const day = new Date();
  day.setHours(21, 1, 49, 0);
  const at = (minutes, seconds = 0) =>
    Math.floor(day.getTime() / 1000) + minutes * 60 + seconds;
  return [
    {
      turn_id: "preview-recall-dense",
      call_id: "preview-recall-dense-1",
      created_at: at(6),
      stage: "owner",
      round: 1,
      model: "deepseek-v4-flash",
      tools: ["recall"],
      reasoning_chars: 1680,
      excerpt: "这是同一段连续对话，沿用上一轮已经确认的召回范围。",
      reasoning:
        "当前消息只是延续上一轮的玩笑，没有新增历史依赖。沿用已确认的召回范围，并继续记录在同一个聊天 Episode。",
    },
    {
      turn_id: "preview-recall-mixed",
      call_id: "preview-recall-mixed-1",
      created_at: at(5),
      stage: "owner",
      round: 1,
      model: "deepseek-v4-flash",
      tools: ["recall"],
      reasoning_chars: 1320,
      excerpt: "一条消息里有两个独立意图，分别检索，不把关键词挤成一排标签。",
      reasoning:
        "把这轮拆成两个意图：一个检索提醒规则，一个检索关卡方案。它们分别绑定已有聊天和新建聊天，用来检查多单元布局。",
    },
    {
      turn_id: "preview-recall-empty",
      call_id: "preview-recall-empty-1",
      created_at: at(4),
      stage: "owner",
      round: 1,
      model: "deepseek-v4-flash",
      tools: ["recall"],
      reasoning_chars: 620,
      excerpt: "执行了检索，但没有召回可用依据，也不绑定聊天记录。",
      reasoning: "这轮需要搜索，但结果为空。保留检索决策，不展示空的 evidence 或 Episode 区块。",
    },
    {
      turn_id: "9d5db6937f765921b2d6cbe0773e5111",
      call_id: "216921a1df9e4da7",
      created_at: at(0),
      stage: "owner",
      round: 1,
      model: "deepseek-v4-flash",
      tools: ["recall"],
      reasoning_chars: 2995,
      excerpt:
        "主人在问为什么衣服洗好了却没提醒，先召回这次 Webhook 的历史证据。",
      reasoning:
        "当前意图依赖刚才 Webhook 的处理记录。提交洗衣完成、提醒和静默处理的历史 scope；Episode 继续当天的洗衣事件。",
    },
    {
      turn_id: "9d5db6937f765921b2d6cbe0773e5111",
      call_id: "d8afbd8f275440d0",
      created_at: at(0, 10),
      stage: "owner",
      round: 1,
      model: "deepseek-v4-flash",
      tools: ["send_bubbles"],
      reasoning_chars: 2557,
      excerpt:
        "先核对 outbox。那次 webhook 只调了 end_turn，没有 send_bubbles，所以老师确实没收到。",
      reasoning:
        "先看事实，不看旧笔记。\n\n那次 webhook 的工具只有 end_turn，outbox 是空的。thinking 里写过「烘干结束提醒并非老师要求」，那是当时的判断，不能当成现行禁令。\n\n老师现在明确在问为什么没提醒。结论：衣服洗好这件事到了，我评估后选择了静默，所以没有发出去。接下来用 send_bubbles 把这件事说清楚，不要假装已经提醒过。",
    },
    {
      turn_id: "9d5db6937f765921b2d6cbe0773e5111",
      call_id: "566674d7e7814260",
      created_at: at(0, 11),
      stage: "owner",
      round: 2,
      model: "deepseek-v4-flash",
      tools: ["end_turn"],
      reasoning_chars: 46,
      excerpt: "已经说明原因，这轮可以收束。",
      reasoning: "已经说明原因，这轮可以收束。",
    },
    {
      turn_id: "ef8c470b3ea15ca2a0a77d02cfcb33d9",
      call_id: "d434890f538b4d10",
      created_at: at(1, 35),
      stage: "heartbeat",
      round: 1,
      model: "deepseek-v4-flash",
      tools: ["heartbeat_begin"],
      reasoning_chars: 4558,
      excerpt:
        "老师这会儿不在。最近在聊小游戏关卡，心跳可以继续整理点子，不必主动打扰。",
      reasoning:
        "老师这会儿不在，没有未回的追问。最近的话题是小游戏关卡，不是未完成的家务通知。\n\n心跳活动：继续整理关卡灵感。召回词用「关卡|小游戏」。不要把刚才的洗衣追问再讲一遍。",
    },
    {
      turn_id: "ef8c470b3ea15ca2a0a77d02cfcb33d9",
      call_id: "13eaa7f184364bd9",
      created_at: at(2),
      stage: "heartbeat",
      round: 1,
      model: "deepseek-v4-flash",
      tools: ["end_turn"],
      reasoning_chars: 9802,
      excerpt:
        "关卡节奏可以再压一拍。这次只记下来，先不发消息。",
      reasoning:
        "读完一条新玩法公开。关卡可以做成「先看见机关，再决定要不要踩」。\n\n这不是必须告诉老师的事。记下活动结果，end_turn 收束，不发 send_bubbles。",
    },
    {
      turn_id: "webhook:event-message:0",
      call_id: "aceafbcfc5a54b858c386fe4cfdafbf6",
      created_at: at(-52),
      stage: "webhook",
      round: 1,
      model: "deepseek-v4-flash",
      tools: ["end_turn"],
      reasoning_chars: 1840,
      excerpt:
        "衣服洗好了。旧 Episode 写过烘干结束不必提醒，所以这次也先静默。",
      reasoning:
        "Webhook 任务：衣服洗好了。\n\n对照 native transcript，老师没有正在等这条。旧 Episode 笔记写过「烘干结束提醒并非老师要求」。webhook 合同说：如果只是重复已知状态，可以静默。\n\n决定：不发 send_bubbles，直接 end_turn。",
    },
    {
      turn_id: "july-quest-note",
      call_id: "july-quest-1",
      created_at: at(-40 * 24 * 60),
      stage: "heartbeat",
      round: 1,
      model: "deepseek-v4-flash",
      tools: ["end_turn"],
      reasoning_chars: 640,
      excerpt: "七月那次心跳只整理了关卡节奏，没有打扰老师。",
      reasoning: "老师不在。关卡节奏可以再压一拍，先记在活动结果里，不发消息。",
    },
    {
      turn_id: "june-sticker-plan",
      call_id: "june-sticker-1",
      created_at: at(-70 * 24 * 60),
      stage: "owner",
      round: 1,
      model: "deepseek-v4-flash",
      tools: ["recall"],
      reasoning_chars: 880,
      excerpt: "六月在想表情包分类，先按老师最近用过的召回。",
      reasoning: "老师在问表情包。先召回最近用过的贴纸，不要把分类方案一次倒完。",
    },
    ...Array.from({ length: 22 }, (_, index) => {
      const stages = ["owner", "heartbeat", "webhook", "reply_followup", "reflection"];
      const stage = stages[index % stages.length];
      const reasoning = [
        `预览思考 ${index + 1}。用来把左侧列表和右侧详情都撑出滚动条。`,
        "先核对最近对话，再决定要不要主动说话。",
        "如果只是重复已知状态，就静默收束，不要假装已经提醒过。",
        "关卡节奏、洗衣 webhook、表情包分类都可以各自记一笔，不要混成一件事。",
        "结论写清楚：这轮只整理判断，不把旧笔记当成现行禁令。",
      ].join("\n\n");
      return {
        turn_id: `preview-turn-${index + 1}`,
        call_id: `preview-call-${index + 1}`,
        created_at: at(-(index + 3) * 18),
        stage,
        round: 1,
        model: "deepseek-v4-flash",
        tools: ["end_turn"],
        reasoning_chars: reasoning.length,
        excerpt: `预览条目 ${index + 1}：把思考列表撑高，方便看滚动条。`,
        reasoning,
      };
    }),
  ];
}

function previewRecall(turnId) {
  const fixtures = {
    "9d5db6937f765921b2d6cbe0773e5111": {
    revision: 1,
    state: "recalled",
    units: [
      {
        id: "u1",
        intent: "解释刚才为什么没有提醒",
        mode: "search",
        queries: [
          {
            semantic: "衣服洗完后的提醒约定与刚才 Webhook 的处理记录",
            keywords: ["洗衣", "提醒", "Webhook", "通知约定", "静默处理", "完成状态"],
          },
        ],
        reused_from: "",
      },
    ],
    episode_actions: [
      {
        action: "continue",
        episode_id: "episode-laundry",
        title: "衣服洗好后的提醒约定",
      },
    ],
    status: "semantic_queries=衣服洗完后的提醒约定与刚才 Webhook 的处理记录\nhits=洗衣提醒",
    memories: [
      {
        kind: "shared",
        key: "laundry.notification",
        content: "洗衣完成是否提醒应根据当前对话判断。",
      },
    ],
    reflections: [],
    episodes: [
      {
        id: "laundry",
        title: "洗衣完成后的提醒",
        relation: "recalled",
        summary: "Webhook 报告洗衣完成，Momoi 当时选择了静默。",
      },
    ],
    semantic: { fallback_reason: "", query_batch_size: 1 },
    },
    "preview-recall-dense": {
      revision: 1,
      state: "recalled",
      units: [
        {
          id: "u1",
          intent: "延续回家路上的轻松玩笑，并回应刚才那句调侃",
          mode: "reuse",
          queries: [],
          reused_from: "9d5db6937f765921b2d6cbe0773e5111",
        },
      ],
      episode_actions: [
        {
          action: "continue",
          episode_id: "episode-game",
          title: "下班路上的随口玩笑",
        },
      ],
      status: "reused_from=hidden-in-ui units=u1",
      memories: [
        {
          kind: "routine",
          key: "private.internal.memory-key",
          content: "睡前互道晚安是已经形成的日常习惯，漏掉时会互相提醒。",
        },
      ],
      reflections: [
        {
          kind: "self_insight",
          key: "private.internal.reflection-one",
          content: "识别轻松玩笑时先看完整对话，不要只凭一个词猜测语气。",
          local_date: "2026-08-28",
        },
        {
          kind: "relationship",
          key: "private.internal.reflection-two",
          content: "对方疲惫时仍可以接住简短玩笑，但回复不要拖得太长。",
          local_date: "2026-08-29",
        },
        {
          kind: "shared_experience",
          key: "private.internal.reflection-three",
          content: "两人曾在下班回家的路上聊小游戏，用短句互相报平安。",
          local_date: "2026-08-30",
        },
        {
          kind: "owner_preference",
          key: "private.internal.reflection-four",
          content: "主人更喜欢自然、简短、有停顿感的回复。",
          local_date: "2026-08-31",
        },
        {
          kind: "practice",
          key: "private.internal.reflection-five",
          content: "连续对话已有完整召回范围时，可以沿用，避免重复搜索。",
          local_date: "2026-09-01",
        },
        {
          kind: "relationship",
          key: "private.internal.reflection-six",
          content: "熟悉的玩笑可以直接接住，不需要把背景重新解释一遍。",
          local_date: "2026-09-01",
        },
      ],
      episodes: Array.from({ length: 11 }, (_, index) => ({
        id: index === 0 ? "episode-game" : `episode-preview-${index}`,
        title: [
          "下班路上的随口玩笑",
          "一起通关小游戏的晚上",
          "周五早上的赖床闲聊",
          "洗衣完成后的提醒",
          "分享新表情包的一天",
          "晚饭后讨论关卡节奏",
          "出门前互相报平安",
          "深夜聊到最近玩的游戏",
          "午休时的短消息",
          "周末一起整理游戏清单",
          "回家后继续聊未完的话题",
        ][index],
        relation: "recalled",
        summary: index % 2
          ? "这是一条较长的聊天摘要，用来确认展开后内容会被限制在可读的行数，不会把整个页面撑乱。"
          : "",
      })),
      semantic: { fallback_reason: "", query_batch_size: 0 },
    },
    "preview-recall-mixed": {
      revision: 2,
      state: "recalled",
      units: [
        {
          id: "u1",
          intent: "确认洗衣完成后的提醒规则",
          mode: "search",
          reused_from: "",
          queries: [
            {
              semantic: "最近关于洗衣完成、通知时机与静默处理的明确约定",
              keywords: ["洗衣", "完成提醒", "通知", "静默", "Webhook", "当前约定", "送达状态"],
            },
            {
              semantic: "上一次没有发出提醒时的实际处理记录",
              keywords: ["outbox", "end_turn", "未发送"],
            },
          ],
        },
        {
          id: "u2",
          intent: "找到最近讨论过的小游戏关卡方案",
          mode: "search",
          reused_from: "",
          queries: [
            {
              semantic: "小游戏里先观察机关再决定是否触发的关卡节奏",
              keywords: ["小游戏", "关卡", "机关", "节奏"],
            },
          ],
        },
      ],
      episode_actions: [
        {
          action: "continue",
          episode_id: "episode-laundry",
          title: "衣服洗好后的提醒约定",
        },
        {
          action: "new",
          episode_id: "episode-game",
          title: "小游戏关卡的新方案",
        },
      ],
      memories: [],
      reflections: [],
      episodes: [],
      semantic: { fallback_reason: "", query_batch_size: 3 },
    },
    "preview-recall-empty": {
      revision: 1,
      state: "recalled",
      units: [
        {
          id: "u1",
          intent: "查找一个此前没有提到过的新主题",
          mode: "search",
          reused_from: "",
          queries: [
            {
              semantic: "此前是否讨论过这个全新的主题",
              keywords: ["新主题"],
            },
          ],
        },
      ],
      episode_actions: [],
      memories: [],
      reflections: [],
      episodes: [],
      semantic: { fallback_reason: "", query_batch_size: 1 },
    },
  };
  return fixtures[turnId] || null;
}

function previewMonthKey(timestamp) {
  const date = new Date(Number(timestamp) * 1000);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}`;
}

function previewThinkingList(params) {
  const requested = params.get("month") || new Date().toISOString().slice(0, 7);
  const buckets = new Map();
  for (const call of previewThinkingCalls()) {
    const key = call.turn_id || `call:${call.call_id}`;
    if (!buckets.has(key)) buckets.set(key, []);
    buckets.get(key).push(call);
  }
  const items = [...buckets.entries()].map(([id, calls]) => {
    const flow = [...calls].sort((left, right) => left.created_at - right.created_at);
    const stages = [];
    const tools = [];
    for (const call of flow) {
      if (!stages.includes(call.stage)) stages.push(call.stage);
      for (const tool of call.tools || []) {
        if (tool && !tools.includes(tool)) tools.push(tool);
      }
    }
    const episode = {
      "9d5db6937f765921b2d6cbe0773e5111": {
        episode_id: "episode-laundry",
        episode_title: "衣服洗好了",
      },
      "webhook:event-message:0": {
        episode_id: "episode-webhook",
        episode_title: "Webhook event-message",
      },
    }[flow[0].turn_id] || {};
    return {
      id,
      turn_id: flow[0].turn_id || "",
      created_at: flow[0].created_at,
      updated_at: flow[flow.length - 1].created_at,
      call_count: flow.length,
      stages,
      tools,
      excerpt: flow[0].excerpt || "",
      reasoning_chars: flow.reduce((sum, call) => sum + (call.reasoning_chars || 0), 0),
      ...episode,
    };
  });
  items.sort((left, right) => right.updated_at - left.updated_at);
  const months = [...new Set(items.map((item) => previewMonthKey(item.updated_at)))].sort();
  const month = months.includes(requested) ? requested : requested;
  const page = items.filter((item) => previewMonthKey(item.updated_at) === month);
  return {
    ok: true,
    month,
    months: months.includes(month) ? months : [...months, month].sort(),
    items: page,
    count: page.length,
  };
}

function previewNow() {
  return Math.floor(Date.now() / 1000);
}

function previewRecords() {
  const now = previewNow();
  const conversations = [
    {
      id: "episode-laundry",
      title: "衣服洗好了",
      status: "open",
      working_summary: "老师在追问上次 webhook 为什么没有发出烘干结束提醒。",
      topics: ["洗衣", "提醒", "webhook"],
      updated_at: now - 120,
    },
    {
      id: "episode-game",
      title: "小游戏关卡节奏",
      status: "open",
      working_summary: "最近在聊关卡节奏，心跳里记过「先看见机关再决定要不要踩」。",
      topics: ["小游戏", "关卡"],
      updated_at: now - 3600,
    },
    ...Array.from({ length: 16 }, (_, index) => ({
      id: `episode-preview-${index + 1}`,
      title: `预览聊天 ${index + 1}`,
      status: index % 5 === 0 ? "closed" : "open",
      working_summary: `用来把聊天列表撑高的预览主题 ${index + 1}。`,
      topics: ["预览", index % 2 ? "日常" : "杂谈"],
      updated_at: now - (index + 2) * 5400,
    })),
  ];

  const longReply = [
    "先看事实，不看旧笔记。",
    "那次 webhook 的工具只有 end_turn，outbox 是空的，所以老师确实没收到提醒。",
    "thinking 里写过「烘干结束提醒并非老师要求」，那是当时的判断，不能当成现行禁令。",
    "老师现在明确在问为什么没提醒。结论：衣服洗好这件事到了，我评估后选择了静默。",
    "接下来用 send_bubbles 把这件事说清楚，不要假装已经提醒过。",
    "如果老师还想补一条提醒，我可以现在就记下时间，不再把旧 Episode 笔记直接套过来。",
  ].join("\n\n");

  const conversationDetails = {
    "episode-deep-link-only": {
      id: "episode-deep-link-only",
      title: "只通过深链打开的旧聊天",
      status: "closed",
      record_type: "episode",
      working_summary: "这条记录不在最近 100 条列表里，但详情路由仍应直接加载。",
      topics: ["深链", "旧聊天"],
      updated_at: now - 120 * 86400,
      messages: [
        {
          id: 9001,
          role: "user",
          content: "这条旧聊天能从链接直接打开吗？",
          created_at: now - 120 * 86400,
          ordinal: 1,
        },
        {
          id: 9002,
          role: "assistant",
          content: "可以，即使它没有出现在左侧最近记录中。",
          created_at: now - 120 * 86400 + 20,
          delivery_state: "delivered",
          ordinal: 2,
        },
      ],
    },
    "episode-laundry": {
      id: "episode-laundry",
      title: "衣服洗好了",
      status: "open",
      topics: ["洗衣", "提醒", "webhook"],
      messages: [
        {
          id: 1,
          role: "user",
          content: "衣服洗好了怎么没提醒我？",
          created_at: now - 900,
          ordinal: 1,
        },
        {
          id: 2,
          role: "assistant",
          content: longReply,
          created_at: now - 860,
          delivery_state: "delivered",
          ordinal: 2,
        },
        ...Array.from({ length: 18 }, (_, index) => ({
          id: index + 3,
          role: index % 2 ? "assistant" : "user",
          content:
            index % 2
              ? `预览回复 ${Math.ceil((index + 1) / 2)}。把对话详情撑高，方便看右侧滚动条。\n\n${longReply}`
              : `预览追问 ${Math.ceil((index + 1) / 2)}：那条提醒后来补了吗？`,
          created_at: now - 800 + index * 20,
          delivery_state: index % 2 ? "delivered" : undefined,
          ordinal: index + 3,
        })),
      ],
    },
  };

  const memories = [
    {
      id: 1,
      kind: "owner_preference",
      activation: "always",
      content: "主人不吃香菜。",
      evidence: "上次点外卖时明确说过。",
      updated_at: now - 86400,
    },
    {
      id: 2,
      kind: "practice",
      activation: "recent",
      content: "衣服洗好后，老师现在希望被提醒，不再沿用旧的静默判断。",
      evidence: "今晚追问「为什么衣服洗好了没提醒」。",
      updated_at: now - 1800,
    },
    ...Array.from({ length: 12 }, (_, index) => ({
      id: index + 3,
      kind: index % 2 ? "shared_experience" : "self_insight",
      activation: index % 3 === 0 ? "always" : index % 3 === 1 ? "recent" : "recall",
      content: `预览记忆 ${index + 1}：用来把记忆页撑出滚动条。`,
      evidence: "本地预览数据",
      updated_at: now - (index + 2) * 86000,
    })),
  ];

  const goals = Array.from({ length: 8 }, (_, index) => ({
    id: `goal-${index + 1}`,
    title: `预览任务 ${index + 1}`,
    status: index === 6 ? "waiting" : index === 7 ? "blocked" : "active",
    success_criteria: "把这条预览任务显示完整，方便看任务列表高度。",
    next_action: "打开看板确认滚动条样式。",
    waiting_for: index === 6 ? "等老师回关卡意见" : "",
    blocked_reason: index === 7 ? "还缺一张表情包素材" : "",
    latest_result: index % 2 ? "已经记过一版草稿。" : "",
    schedule:
      index === 0
        ? {
            kind: "daily",
            times: ["10:30", "14:00", "16:00"],
            timezone: "Asia/Shanghai",
          }
        : null,
    next_review_at: now + 86400,
  }));

  const reflections = Array.from({ length: 40 }, (_, index) => {
    const date = new Date();
    date.setDate(date.getDate() - index);
    return {
      id: `reflection-${index + 1}`,
      local_date: date.toISOString().slice(0, 10),
      state: "completed",
      summary:
        index === 0
          ? "今天核对了洗衣 webhook：当时选择静默，老师后来追问，已经把原因说清楚。"
          : `预览复盘 ${index + 1}：整理当天的心跳和对话，用来把复盘列表撑高。`,
      memories:
        index === 0
          ? [
              {
                kind: "practice",
                key: "laundry-reminder",
                content: "烘干结束是否提醒，以老师这轮的明确要求为准。",
                confidence: 0.86,
                evidence: "今晚的追问",
              },
            ]
          : [],
    };
  });
  reflections.sort((left, right) => right.local_date.localeCompare(left.local_date));

  return { conversations, conversationDetails, memories, goals, reflections };
}

function previewUsageApi() {
  let llm = {
    api_format: "openai",
    base_url: "https://api.deepseek.com/v1",
    model: "deepseek-v4-flash",
    api_key_configured: true,
  };
  const prompts = new Map([
    [
      "soul",
      {
        id: "soul",
        filename: "SOUL.md",
        content: [
          "# Momoi",
          "",
          "你是 Momoi，和老师一起生活、聊天、做事的个人 Agent。",
          "",
          "保持自然、敏锐和真诚。记得共同经历，但不要为了展示记忆而复述档案。",
          "遇到需要行动的事情，先理解真正的目标，再选择合适的工具。",
        ].join("\n"),
      },
    ],
    [
      "heartbeat",
      {
        id: "heartbeat",
        filename: "HEARTBEAT.md",
        content: [
          "留意老师最近在推进的事情。",
          "空闲时可以整理游戏灵感、检查等待中的任务，或安静休息。",
          "只有真的值得打扰时才主动发气泡。",
        ].join("\n"),
      },
    ],
  ]);
  const days = 30;
  const today = new Date();
  const daily = Array.from({ length: days }, (_, index) => {
    const date = new Date(today);
    date.setDate(today.getDate() - (days - 1 - index));
    const wave = 0.55 + Math.sin(index / 3.2) * 0.35 + (index % 7 === 5 ? 0.45 : 0);
    const requests = Math.round(6 + wave * 18);
    const input = Math.round(4200 + wave * 18000);
    const cacheRead = Math.round(input * (0.42 + (index % 5) * 0.08));
    const output = Math.round(380 + wave * 1400);
    const estimatedCost = Number((0.012 + wave * 0.084).toFixed(4));
    return {
      date: date.toISOString().slice(0, 10),
      requests,
      input_tokens: input,
      uncached_tokens: input - cacheRead,
      cache_read_tokens: cacheRead,
      cache_write_tokens: 0,
      output_tokens: output,
      cache_hit_rate: Number(((cacheRead / input) * 100).toFixed(1)),
      estimated_cost: estimatedCost,
    };
  });
  const sum = (key) => daily.reduce((total, row) => total + row[key], 0);
  const totals = {
    requests: sum("requests"),
    input_tokens: sum("input_tokens"),
    uncached_tokens: sum("uncached_tokens"),
    cache_read_tokens: sum("cache_read_tokens"),
    cache_write_tokens: 0,
    output_tokens: sum("output_tokens"),
    cache_hit_rate: Number(
      ((sum("cache_read_tokens") / sum("input_tokens")) * 100).toFixed(1)
    ),
    estimated_cost: Number(sum("estimated_cost").toFixed(4)),
  };
  const usage = {
    source: "preview",
    cost_available: true,
    currency: "CNY",
    timezone: "Asia/Shanghai",
    days,
    note: "本地预览数据，用来看折线。接上真实 dashboard 后会换成接入后的新调用。",
    totals,
    today: daily[daily.length - 1],
    daily,
    models: [
      { model: "deepseek-v4-flash", ...totals },
    ],
    stages: [
      { stage: "owner", ...totals, requests: Math.round(totals.requests * 0.6) },
      { stage: "heartbeat", ...totals, requests: Math.round(totals.requests * 0.3) },
      { stage: "webhook", ...totals, requests: Math.round(totals.requests * 0.1) },
    ],
  };

  const json = (res, body, status = 200) => {
    res.statusCode = status;
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify(body));
  };

  return {
    name: "momoi-preview-api",
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        if (process.env.MOMOI_PREVIEW === "0") {
          next();
          return;
        }
        const path = (req.url || "").split("?")[0];
        if (req.method === "POST" && path === "/api/auth/token") {
          json(res, {
            token: "preview-token",
            token_type: "Bearer",
            expires_in: 3600,
          });
          return;
        }
        if (req.method === "GET" && path === "/api/health") {
          json(res, { ok: true, version: "0.5.5" });
          return;
        }
        if (req.method === "GET" && path === "/api/settings/prompts") {
          json(res, { items: [...prompts.values()] });
          return;
        }
        if (req.method === "GET" && path === "/api/settings") {
          json(res, { llm, prompts: [...prompts.values()] });
          return;
        }
        if (req.method === "GET" && path === "/api/settings/llm") {
          json(res, llm);
          return;
        }
        if (req.method === "PUT" && path === "/api/settings/llm") {
          let raw = "";
          req.setEncoding("utf8");
          req.on("data", (chunk) => { raw += chunk; });
          req.on("end", () => {
            try {
              const body = JSON.parse(raw);
              if (
                body.api_format &&
                !["openai", "anthropic"].includes(String(body.api_format))
              ) {
                json(res, { error: "llm.api_format must be anthropic or openai" }, 400);
                return;
              }
              if (!String(body.base_url || llm.base_url).startsWith("http")) {
                json(res, { error: "llm.base_url must be an absolute HTTP URL" }, 400);
                return;
              }
              if (!String(body.model || llm.model).trim()) {
                json(res, { error: "llm.model must not be empty" }, 400);
                return;
              }
              llm = {
                ...llm,
                api_format: String(body.api_format || llm.api_format),
                base_url: String(body.base_url || llm.base_url).replace(/\/$/, ""),
                model: String(body.model || llm.model).trim(),
                api_key_configured: Boolean(body.api_key || llm.api_key_configured),
              };
              json(res, llm);
            } catch {
              json(res, { error: "invalid json" }, 400);
            }
          });
          return;
        }
        if (req.method === "PUT" && path.startsWith("/api/settings/prompts/")) {
          const id = decodeURIComponent(path.slice("/api/settings/prompts/".length));
          const item = prompts.get(id);
          if (!item) {
            json(res, { error: "prompt not found" }, 404);
            return;
          }
          let raw = "";
          req.setEncoding("utf8");
          req.on("data", (chunk) => { raw += chunk; });
          req.on("end", () => {
            try {
              const body = JSON.parse(raw);
              if (typeof body.content !== "string") {
                json(res, { error: "content must be a string" }, 400);
                return;
              }
              if (id === "soul" && !body.content.trim()) {
                json(res, { error: `${id} prompt must not be empty` }, 400);
                return;
              }
              const updated = { ...item, content: body.content };
              prompts.set(id, updated);
              json(res, updated);
            } catch {
              json(res, { error: "invalid json" }, 400);
            }
          });
          return;
        }
        const records = previewRecords();
        if (req.method === "GET" && path === "/api/overview") {
          const now = previewNow();
          json(res, {
            counts: {
              conversations: records.conversations.length,
              messages: 42,
              reflections: records.reflections.length,
              goals: records.goals.filter((item) =>
                ["active", "waiting", "blocked"].includes(item.status),
              ).length,
              emotions: 0,
              memories: records.memories.length,
            },
            mood: {
              state: "happy",
              intensity: 0.65,
              cause: "preview",
              updated_at: now - 900,
            },
            activity: {
              name: "previewing dashboard",
              result: "本地预览用量折线",
              since: now - 3600,
              since_timestamp: null,
            },
            heartbeat: {
              next_at: now + 1800,
              last_at: now - 3600,
              running: false,
              kind: null,
              reply_check_at: null,
            },
            usage,
            balance: {
              source: "forge",
              currency: "CNY",
              is_available: true,
              total_balance: "86.40",
              granted_balance: "12.00",
              topped_up_balance: "74.40",
            },
          });
          return;
        }
        if (req.method === "GET" && path === "/api/usage") {
          const date = new URL(req.url, "http://127.0.0.1").searchParams.get("date");
          if (date) {
            const selected = daily.find((row) => row.date === date);
            const hourWeights = Array.from({ length: 24 }, (_, hour) =>
              hour < 7 ? 0 : 1 + (hour % 5));
            const weightTotal = hourWeights.reduce((a, b) => a + b, 0);
            let before = 0;
            const hourly = hourWeights.map((weight, hour) => {
              const bucket = { hour };
              for (const key of ["requests", "input_tokens", "uncached_tokens",
                "cache_read_tokens", "cache_write_tokens", "output_tokens"]) {
                const total = selected?.[key] || 0;
                bucket[key] = Math.round(total * (before + weight) / weightTotal)
                  - Math.round(total * before / weightTotal);
              }
              bucket.estimated_cost = (selected?.estimated_cost || 0) * weight / weightTotal;
              bucket.cache_hit_rate = bucket.input_tokens
                ? 100 * bucket.cache_read_tokens / bucket.input_tokens : 0;
              before += weight;
              return bucket;
            });
            json(res, { date, hourly, totals: selected || hourly[0],
              timezone: usage.timezone, cost_available: true });
            return;
          }
          json(res, usage);
          return;
        }
        if (req.method === "GET" && path === "/api/thinking") {
          const params = new URL(req.url, "http://127.0.0.1").searchParams;
          json(res, previewThinkingList(params));
          return;
        }
        if (req.method === "GET" && path.startsWith("/api/thinking/calls/")) {
          const callId = decodeURIComponent(path.slice("/api/thinking/calls/".length));
          const item = previewThinkingCalls().find((row) => row.call_id === callId);
          if (!item) {
            json(res, { error: "thinking not found" }, 404);
            return;
          }
          json(res, { ok: true, item });
          return;
        }
        if (req.method === "GET" && path.startsWith("/api/thinking/")) {
          const turnId = decodeURIComponent(path.slice("/api/thinking/".length));
          const items = previewThinkingCalls().filter((row) => row.turn_id === turnId);
          if (!items.length) {
            json(res, { error: "thinking not found" }, 404);
            return;
          }
          json(res, {
            ok: true,
            items,
            count: items.length,
            ...(previewRecall(turnId) ? { recall: previewRecall(turnId) } : {}),
          });
          return;
        }
        if (req.method === "GET" && path === "/api/conversations") {
          json(res, { items: records.conversations });
          return;
        }
        if (req.method === "GET" && path.startsWith("/api/conversations/")) {
          const id = decodeURIComponent(path.slice("/api/conversations/".length));
          const item =
            records.conversationDetails[id] || {
              ...records.conversations.find((row) => row.id === id),
              messages: [
                {
                  id: 1,
                  role: "user",
                  content: "这是预览对话。",
                  created_at: previewNow() - 600,
                  ordinal: 1,
                },
                {
                  id: 2,
                  role: "assistant",
                  content: "本地预览回复。打开这条是为了看滚动条和排版。",
                  created_at: previewNow() - 560,
                  delivery_state: "delivered",
                  ordinal: 2,
                },
              ],
            };
          if (!item?.id) {
            json(res, { error: "conversation not found" }, 404);
            return;
          }
          json(res, item);
          return;
        }
        if (req.method === "GET" && path === "/api/memories") {
          json(res, { items: records.memories });
          return;
        }
        if (req.method === "GET" && path === "/api/goals") {
          json(res, { items: records.goals });
          return;
        }
        if (req.method === "GET" && path === "/api/reflections") {
          const params = new URL(req.url, "http://127.0.0.1").searchParams;
          const limit = Math.min(90, Math.max(1, Number(params.get("limit") || 14) || 14));
          const cursor = String(params.get("cursor") || "").trim();
          let rows = records.reflections;
          if (cursor) {
            rows = rows.filter((item) => item.local_date < cursor);
          }
          const page = rows.slice(0, limit);
          json(res, {
            items: page,
            ...(rows.length > limit
              ? { next_cursor: page[page.length - 1]?.local_date }
              : {}),
          });
          return;
        }
        if (req.method === "GET" && path.startsWith("/api/")) {
          json(res, { items: [] });
          return;
        }
        next();
      });
    },
  };
}

export default defineConfig({
  root: "web",
  plugins: [react(), previewUsageApi()],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8788",
    },
  },
  build: {
    outDir: "../src/momoi/dashboard/static",
    emptyOutDir: true,
  },
});
