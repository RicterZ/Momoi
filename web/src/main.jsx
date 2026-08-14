import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

const pages = {
  overview: ["今天也元气满满！", "MOMOI // HOME"],
  conversations: ["聊天记录", "MOMOI // CHAT LOG"],
  reflections: ["每日复盘", "MOMOI // SAVE DATA"],
  memories: ["记忆", "MOMOI // MEMORY"],
  emotions: ["表情包", "MOMOI // STICKERS"],
  goals: ["任务列表", "MOMOI // QUESTS"],
};

const navItems = [
  ["overview", "01", "主页"],
  ["conversations", "02", "聊天记录"],
  ["reflections", "03", "每日复盘"],
  ["memories", "04", "记忆"],
  ["emotions", "05", "表情包"],
  ["goals", "06", "任务列表"],
];

const activationOrder = ["always", "recent", "recall"];
const activationLabels = {
  always: "持续生效",
  recent: "近期状态",
  recall: "需要时回忆",
};

async function api(path, signal) {
  const response = await fetch(path, {
    headers: { Accept: "application/json" },
    cache: "no-store",
    signal,
  });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}

function formatDate(value, dateOnly = false) {
  if (value === null || value === undefined || value === "") return "—";
  const date =
    typeof value === "number"
      ? new Date(value * 1000)
      : new Date(String(value).replace(" ", "T"));
  if (Number.isNaN(date.valueOf())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "short",
    day: "numeric",
    ...(dateOnly ? {} : { hour: "2-digit", minute: "2-digit" }),
  }).format(date);
}

function memoryKindLabel(kind) {
  return (
    {
      profile: "关于你",
      preference: "你的偏好",
      relationship: "相处方式",
      shared: "共同经历",
      episodic: "具体经历",
      routine: "日常习惯",
      owner_profile: "关于你",
      owner_preference: "你的偏好",
      world_knowledge: "外部信息",
      self_insight: "Momoi 的体会",
      shared_experience: "共同经历",
      practice: "行动习惯",
    }[kind] || "记忆"
  );
}

function useHashRoute() {
  const read = () => {
    const value = window.location.hash.slice(1);
    return pages[value] ? value : "overview";
  };
  const [view, setView] = useState(read);
  useEffect(() => {
    const route = () => setView(read());
    window.addEventListener("hashchange", route);
    return () => window.removeEventListener("hashchange", route);
  }, []);
  return view;
}

function Loading({ children = "正在读取 Momoi 的生活记录…" }) {
  return (
    <div className="loading">
      <span className="loading-mark">M</span>
      <span>{children}</span>
    </div>
  );
}

function Empty({ text = "Momoi 开始运行后，内容会出现在这里。" }) {
  return (
    <div className="empty">
      <span>—</span>
      <h2>这里还没有记录</h2>
      <p>{text}</p>
    </div>
  );
}

function ErrorState({ error }) {
  return (
    <div className="error">
      <strong>数据暂时无法读取</strong>
      <p>{error.message}</p>
    </div>
  );
}

function DataView({ path, refreshKey, children }) {
  const [state, setState] = useState({ loading: true });
  useEffect(() => {
    const controller = new AbortController();
    setState({ loading: true });
    api(path, controller.signal)
      .then((data) => setState({ data }))
      .catch((error) => {
        if (error.name !== "AbortError") setState({ error });
      });
    return () => controller.abort();
  }, [path, refreshKey]);
  if (state.loading) return <Loading />;
  if (state.error) return <ErrorState error={state.error} />;
  return children(state.data);
}

function Overview({ refreshKey }) {
  return (
    <DataView path="/api/overview" refreshKey={refreshKey}>
      {(data) => {
        const metrics = [
          ["聊天主题", data.counts.conversations],
          ["消息", data.counts.messages],
          ["复盘", data.counts.reflections],
          ["记忆", data.counts.memories],
          ["进行中 Goals", data.counts.goals],
          ["表情包", data.counts.emotions],
        ];
        return (
          <>
            <section className="metrics">
              {metrics.map(([label, value]) => (
                <article className="metric" key={label}>
                  <span>{label}</span>
                  <strong>{value}</strong>
                </article>
              ))}
            </section>
            <section className="overview-grid">
              <article className="panel">
                <span className="panel-label">Current activity</span>
                <h2 className="state-name">{data.activity.name}</h2>
                <p className="state-detail">
                  {data.activity.result || "Momoi 正在按自己的节奏生活。"}
                </p>
                <p className="secondary">始于 {formatDate(data.activity.since)}</p>
              </article>
              <article className="panel">
                <span className="panel-label">Mood</span>
                <h2 className="state-name">{data.mood.state}</h2>
                <p className="state-detail">{data.mood.cause}</p>
                <div className="intensity" aria-label="情绪强度">
                  <span
                    style={{
                      width: `${Math.max(
                        0,
                        Math.min(100, Number(data.mood.intensity || 0) * 100),
                      )}%`,
                    }}
                  />
                </div>
              </article>
            </section>
          </>
        );
      }}
    </DataView>
  );
}

function Conversations({ refreshKey }) {
  const [selected, setSelected] = useState(null);
  const [detail, setDetail] = useState({ loading: false });
  return (
    <DataView path="/api/conversations?limit=100" refreshKey={refreshKey}>
      {(data) => {
        const items = data.items || [];
        if (!items.length) return <Empty />;
        const activeId = selected || items[0].id;
        return (
          <ConversationLayout
            items={items}
            activeId={activeId}
            detail={detail}
            onSelect={setSelected}
            setDetail={setDetail}
          />
        );
      }}
    </DataView>
  );
}

function ConversationLayout({ items, activeId, detail, onSelect, setDetail }) {
  useEffect(() => {
    const controller = new AbortController();
    setDetail({ loading: true });
    api(
      `/api/conversations/${encodeURIComponent(activeId)}?token_budget=100000`,
      controller.signal,
    )
      .then((data) => setDetail({ data }))
      .catch((error) => {
        if (error.name !== "AbortError") setDetail({ error });
      });
    return () => controller.abort();
  }, [activeId, setDetail]);

  return (
    <section className="record-layout">
      <div className="record-list" aria-label="聊天主题">
        {items.map((item) => (
          <button
            className={`record-item ${item.id === activeId ? "active" : ""}`}
            type="button"
            key={item.id}
            onClick={() => onSelect(item.id)}
          >
            <h3>{item.title}</h3>
            <p>
              {item.working_summary ||
                item.summary ||
                item.topics?.join(" · ") ||
                "暂无摘要"}
            </p>
            <time>{formatDate(item.updated_at)}</time>
          </button>
        ))}
      </div>
      <div className="conversation">
        {detail.loading && <Loading>正在读取聊天…</Loading>}
        {detail.error && <ErrorState error={detail.error} />}
        {detail.data && <ConversationDetail item={detail.data} />}
      </div>
    </section>
  );
}

function ConversationDetail({ item }) {
  return (
    <>
      <header className="conversation-head">
        <span className="panel-label">{item.status} conversation</span>
        <h2>{item.title}</h2>
        <div className="tags">
          {item.topics?.map((topic) => (
            <span className="tag" key={topic}>
              {topic}
            </span>
          ))}
        </div>
      </header>
      <div className="messages">
        {item.messages?.length ? (
          item.messages.map((message) => (
            <article className="message" key={message.id}>
              <div className="message-role">
                {message.role === "user" ? "OWNER" : "MOMOI"}
              </div>
              <div className="message-body">
                <p className="message-content">{message.content}</p>
                <div className="message-meta">
                  <time>{formatDate(message.created_at)}</time>
                  {message.role === "assistant" && (
                    <span>{message.delivery_state}</span>
                  )}
                </div>
              </div>
            </article>
          ))
        ) : (
          <Empty text="这个主题还没有可展示的消息。" />
        )}
      </div>
    </>
  );
}

function Reflections({ refreshKey }) {
  return (
    <DataView path="/api/reflections?limit=180" refreshKey={refreshKey}>
      {({ items }) =>
        items.length ? (
          <>
            <section className="section-tools">
              <p>按日期保留 Momoi 对每天经历的整理与学习。</p>
            </section>
            <section className="card-list">
              {items.map((item) => (
                <article className="reflection-card" key={item.id}>
                  <div className="card-head">
                    <h2>{item.local_date}</h2>
                    <span className="status">{item.state}</span>
                  </div>
                  <p className="summary">
                    {item.summary ||
                      (item.error ? `等待重试：${item.error}` : "尚未生成复盘。")}
                  </p>
                  {!!item.memories?.length && (
                    <div className="memory-list">
                      {item.memories.map((memory) => (
                        <div className="memory" key={`${memory.kind}:${memory.key}`}>
                          <div className="memory-head">
                            <span className="memory-kind">
                              {memoryKindLabel(memory.kind)}
                            </span>
                            {Number.isFinite(Number(memory.confidence)) && (
                              <span className="memory-confidence">
                                可信度 {Math.round(Number(memory.confidence) * 100)}%
                              </span>
                            )}
                          </div>
                          <p>{memory.content}</p>
                          {memory.evidence && (
                            <small>依据：{memory.evidence}</small>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                </article>
              ))}
            </section>
          </>
        ) : (
          <Empty />
        )
      }
    </DataView>
  );
}

function Memories({ refreshKey }) {
  const [activation, setActivation] = useState("all");
  return (
    <DataView path="/api/memories?limit=400" refreshKey={refreshKey}>
      {({ items }) => {
        const visible =
          activation === "all"
            ? items
            : items.filter((item) => item.activation === activation);
        const groups = activationOrder
          .map((name) => [
            name,
            visible.filter((item) => item.activation === name),
          ])
          .filter(([, group]) => group.length);
        return (
          <>
            <section className="section-tools">
              <p>{visible.length} 条有效记忆</p>
              <div className="filter">
                {[["all", "全部"], ...Object.entries(activationLabels)].map(
                  ([value, label]) => (
                    <button
                      type="button"
                      key={value}
                      className={activation === value ? "active" : ""}
                      onClick={() => setActivation(value)}
                    >
                      {label}
                    </button>
                  ),
                )}
              </div>
            </section>
            {groups.length ? (
              groups.map(([name, group]) => (
                <section className="memory-section" key={name}>
                  <div className="section-tools">
                    <p>
                      {activationLabels[name]} · {group.length}
                    </p>
                  </div>
                  <section className="card-list">
                    {group.map((item) => (
                      <article className="reflection-card" key={item.id}>
                        <div className="card-head">
                          <h2>{memoryKindLabel(item.kind)}</h2>
                          <span className="status">
                            {activationLabels[item.activation] || item.activation}
                          </span>
                        </div>
                        <p className="summary">{item.content}</p>
                        {item.evidence && (
                          <p className="secondary">依据：{item.evidence}</p>
                        )}
                        <p className="secondary">
                          更新于 {formatDate(item.updated_at)}
                        </p>
                      </article>
                    ))}
                  </section>
                </section>
              ))
            ) : (
              <Empty />
            )}
          </>
        );
      }}
    </DataView>
  );
}

function scheduleText(schedule, nextReview) {
  if (schedule?.kind === "daily") return `每天 ${schedule.at}`;
  if (schedule?.kind === "interval") return `每 ${schedule.every_seconds} 秒`;
  return nextReview ? formatDate(nextReview) : "无计划时间";
}

function Goals({ refreshKey }) {
  const [includeClosed, setIncludeClosed] = useState(false);
  return (
    <DataView path="/api/goals?all=true" refreshKey={refreshKey}>
      {({ items }) => {
        const visible = includeClosed
          ? items
          : items.filter((item) =>
              ["active", "waiting", "blocked"].includes(item.status),
            );
        return (
          <>
            <section className="section-tools">
              <p>{visible.length} 个目标</p>
              <div className="filter">
                <button
                  type="button"
                  className={includeClosed ? "" : "active"}
                  onClick={() => setIncludeClosed(false)}
                >
                  进行中
                </button>
                <button
                  type="button"
                  className={includeClosed ? "active" : ""}
                  onClick={() => setIncludeClosed(true)}
                >
                  全部
                </button>
              </div>
            </section>
            {visible.length ? (
              <section className="goal-grid">
                {visible.map((item) => (
                  <article className="goal-card" key={item.id}>
                    <span className="status">{item.status}</span>
                    <h2>{item.title}</h2>
                    <p className="criteria">{item.success_criteria}</p>
                    <div className="goal-details">
                      <div>
                        <span className="meta-label">Next action</span>
                        <p>{item.next_action || "—"}</p>
                      </div>
                      {item.latest_result && (
                        <div>
                          <span className="meta-label">Latest result</span>
                          <p>{item.latest_result}</p>
                        </div>
                      )}
                      <div>
                        <span className="meta-label">Schedule</span>
                        <p>{scheduleText(item.schedule, item.next_review_at)}</p>
                      </div>
                    </div>
                  </article>
                ))}
              </section>
            ) : (
              <Empty />
            )}
          </>
        );
      }}
    </DataView>
  );
}

function Emotions({ refreshKey }) {
  return (
    <DataView path="/api/emotions" refreshKey={refreshKey}>
      {({ items }) =>
        items.length ? (
          <>
            <section className="section-tools">
              <p>{items.length} 个可用表情</p>
            </section>
            <section className="emotion-grid">
              {items.map((item) => (
                <article className="emotion-card" key={item.id}>
                  <div className="emotion-frame">
                    <img
                      src={item.asset_url}
                      alt={item.description}
                      loading="lazy"
                    />
                  </div>
                  <div className="emotion-copy">
                    <h2>{item.slug}</h2>
                    <p>{item.description}</p>
                  </div>
                </article>
              ))}
            </section>
          </>
        ) : (
          <Empty />
        )
      }
    </DataView>
  );
}

const viewComponents = {
  overview: Overview,
  conversations: Conversations,
  reflections: Reflections,
  memories: Memories,
  emotions: Emotions,
  goals: Goals,
};

function App() {
  const view = useHashRoute();
  const [refreshKey, setRefreshKey] = useState(0);
  const [pageTitle, eyebrow] = pages[view];
  const View = viewComponents[view];
  return (
    <div className="shell">
      <aside className="sidebar">
        <a className="brand" href="#overview" aria-label="Momoi 首页">
          <span className="brand-mark">M</span>
          <span>
            <strong>Momoi</strong>
            <small>GAME DEV DEPT.</small>
          </span>
        </a>
        <nav aria-label="主导航">
          {navItems.map(([target, index, label]) => (
            <a
              href={`#${target}`}
              className={target === view ? "active" : ""}
              key={target}
            >
              <span>{index}</span>
              <strong>{label}</strong>
            </a>
          ))}
        </nav>
        <div className="sidebar-foot">
          <span className="status-dot" />
          <span>SYSTEM ONLINE</span>
        </div>
      </aside>
      <main>
        <header className="topbar">
          <div>
            <p className="eyebrow">{eyebrow}</p>
            <h1>{pageTitle}</h1>
          </div>
          <button
            className="quiet-button"
            type="button"
            onClick={() => setRefreshKey((value) => value + 1)}
          >
            ↻ 刷新
          </button>
        </header>
        <div id="content">
          <View refreshKey={refreshKey} />
        </div>
      </main>
    </div>
  );
}

createRoot(document.getElementById("root")).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
