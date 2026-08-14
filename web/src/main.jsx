import { StrictMode, createContext, useContext, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

const TOKEN_KEY = "momoi-dashboard-token";
const ConfirmContext = createContext(null);

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

const goalStatuses = [
  ["active", "进行中"],
  ["waiting", "等待中"],
  ["blocked", "受阻"],
];

function readToken() {
  return sessionStorage.getItem(TOKEN_KEY) || "";
}

async function api(path, { signal, method = "GET", body, token, formData } = {}) {
  const headers = { Accept: "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  let payload = body;
  if (formData) {
    payload = formData;
  } else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  const response = await fetch(path, {
    method,
    headers,
    body: payload,
    cache: "no-store",
    signal,
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `${response.status} ${response.statusText}`);
  }
  if (response.status === 204) return null;
  const contentType = response.headers.get("content-type") || "";
  if (!contentType.includes("application/json")) return null;
  return response.json();
}

async function login(secret) {
  const data = await api("/api/auth/token", {
    method: "POST",
    body: { token: secret },
  });
  const token = String(data?.token || "").trim();
  if (!token) throw new Error("missing token");
  return token;
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

function useConfirm() {
  const confirm = useContext(ConfirmContext);
  if (!confirm) throw new Error("ConfirmProvider missing");
  return confirm;
}

function ConfirmProvider({ children }) {
  const [request, setRequest] = useState(null);
  const resolver = useRef(null);

  function confirm({
    title = "确认操作",
    message = "",
    confirmLabel = "确定",
    cancelLabel = "取消",
  } = {}) {
    return new Promise((resolve) => {
      resolver.current = resolve;
      setRequest({ title, message, confirmLabel, cancelLabel });
    });
  }

  function settle(value) {
    const resolve = resolver.current;
    resolver.current = null;
    setRequest(null);
    resolve?.(value);
  }

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      {request && (
        <div
          className="confirm-gate"
          role="dialog"
          aria-modal="true"
          aria-labelledby="confirm-title"
        >
          <div className="confirm-card">
            <p className="eyebrow">MOMOI // CONFIRM</p>
            <h2 id="confirm-title">{request.title}</h2>
            {request.message && <p className="confirm-copy">{request.message}</p>}
            <div className="confirm-actions">
              <button
                className="quiet-button"
                type="button"
                onClick={() => settle(false)}
              >
                {request.cancelLabel}
              </button>
              <button
                className="quiet-button pink"
                type="button"
                autoFocus
                onClick={() => settle(true)}
              >
                {request.confirmLabel}
              </button>
            </div>
          </div>
        </div>
      )}
    </ConfirmContext.Provider>
  );
}

function DataView({ path, refreshKey, token, children }) {
  const [state, setState] = useState({ loading: true });
  useEffect(() => {
    if (!token) {
      setState({ error: new Error("unauthorized") });
      return undefined;
    }
    const controller = new AbortController();
    setState({ loading: true });
    api(path, { signal: controller.signal, token })
      .then((data) => setState({ data }))
      .catch((error) => {
        if (error.name !== "AbortError") setState({ error });
      });
    return () => controller.abort();
  }, [path, refreshKey, token]);
  if (state.loading) return <Loading />;
  if (state.error) return <ErrorState error={state.error} />;
  return children(state.data);
}

function Overview({ refreshKey, token }) {
  return (
    <DataView path="/api/overview" refreshKey={refreshKey} token={token}>
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

function Conversations({ refreshKey, token }) {
  const [selected, setSelected] = useState(null);
  const [detail, setDetail] = useState({ loading: false });
  return (
    <DataView path="/api/conversations?limit=100" refreshKey={refreshKey} token={token}>
      {(data) => {
        const items = data.items || [];
        if (!items.length) return <Empty />;
        const activeId = selected || items[0].id;
        return (
          <ConversationLayout
            items={items}
            activeId={activeId}
            detail={detail}
            token={token}
            onSelect={setSelected}
            setDetail={setDetail}
          />
        );
      }}
    </DataView>
  );
}

function ConversationLayout({ items, activeId, detail, token, onSelect, setDetail }) {
  useEffect(() => {
    if (!token) {
      setDetail({ error: new Error("unauthorized") });
      return undefined;
    }
    const controller = new AbortController();
    setDetail({ loading: true });
    api(`/api/conversations/${encodeURIComponent(activeId)}?token_budget=100000`, {
      signal: controller.signal,
      token,
    })
      .then((data) => setDetail({ data }))
      .catch((error) => {
        if (error.name !== "AbortError") setDetail({ error });
      });
    return () => controller.abort();
  }, [activeId, setDetail, token]);

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
        {detail.data && (
          <ConversationDetail key={activeId} item={detail.data} />
        )}
      </div>
    </section>
  );
}

function messageTime(value) {
  if (value === null || value === undefined || value === "") return 0;
  if (typeof value === "number") return value < 1e12 ? value * 1000 : value;
  const parsed = new Date(String(value).replace(" ", "T")).valueOf();
  return Number.isNaN(parsed) ? 0 : parsed;
}

function ConversationDetail({ item }) {
  const [newestFirst, setNewestFirst] = useState(true);
  const messages = [...(item.messages || [])].sort((left, right) => {
    const delta = messageTime(left.created_at) - messageTime(right.created_at);
    return newestFirst ? -delta : delta;
  });

  return (
    <>
      <header className="conversation-head">
        <div className="conversation-head-row">
          <span className="panel-label">{item.status} conversation</span>
          <button
            className="sort-toggle"
            type="button"
            aria-label={newestFirst ? "切换为时间正序" : "切换为时间倒序"}
            title={newestFirst ? "时间倒序 · 点击正序" : "时间正序 · 点击倒序"}
            onClick={() => setNewestFirst((value) => !value)}
          >
            <span aria-hidden="true">{newestFirst ? "↓" : "↑"}</span>
          </button>
        </div>
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
        {messages.length ? (
          messages.map((message) => (
            <article className="message" key={message.id}>
              <div className={`message-role ${message.role === "user" ? "owner" : "momoi"}`}>
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

function Reflections({ refreshKey, token }) {
  return (
    <DataView path="/api/reflections?limit=180" refreshKey={refreshKey} token={token}>
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

function Memories({ refreshKey, token, onMutated }) {
  const confirm = useConfirm();
  const [activation, setActivation] = useState("all");
  const [editingId, setEditingId] = useState(null);
  const [draft, setDraft] = useState("");
  const [busyId, setBusyId] = useState(null);
  const [error, setError] = useState("");

  async function save(item) {
    setBusyId(item.id);
    setError("");
    try {
      await api(`/api/memories/${item.id}`, {
        method: "PATCH",
        token,
        body: { content: draft },
      });
      setEditingId(null);
      onMutated();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusyId(null);
    }
  }

  async function remove(item) {
    const ok = await confirm({
      title: "删除这条记忆？",
      message: "删掉之后 Momoi 不会再使用它，也找不回来了。",
      confirmLabel: "删除记忆",
      cancelLabel: "先留着",
    });
    if (!ok) return;
    setBusyId(item.id);
    setError("");
    try {
      await api(`/api/memories/${item.id}`, { method: "DELETE", token });
      onMutated();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusyId(null);
    }
  }

  return (
    <DataView path="/api/memories?limit=400" refreshKey={refreshKey} token={token}>
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
              <div className="dash-tabs" role="tablist" aria-label="记忆筛选">
                {[["all", "全部"], ...Object.entries(activationLabels)].map(
                  ([value, label]) => (
                    <button
                      type="button"
                      role="tab"
                      key={value}
                      aria-selected={activation === value}
                      className={activation === value ? "active" : ""}
                      onClick={() => setActivation(value)}
                    >
                      <span>{label}</span>
                    </button>
                  ),
                )}
              </div>
            </section>
            {error && <p className="form-error">{error}</p>}
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
                        {editingId === item.id ? (
                          <textarea
                            className="edit-area"
                            value={draft}
                            onChange={(event) => setDraft(event.target.value)}
                            rows={4}
                          />
                        ) : (
                          <p className="summary">{item.content}</p>
                        )}
                        {item.evidence && (
                          <p className="secondary">依据：{item.evidence}</p>
                        )}
                        <p className="secondary">
                          更新于 {formatDate(item.updated_at)}
                          {item.expires_at
                            ? ` · 有效至 ${formatDate(item.expires_at)}`
                            : ""}
                        </p>
                        <div className="card-actions">
                          {editingId === item.id ? (
                            <>
                              <button
                                type="button"
                                className="quiet-button"
                                disabled={busyId === item.id}
                                onClick={() => save(item)}
                              >
                                保存
                              </button>
                              <button
                                type="button"
                                className="quiet-button pink"
                                onClick={() => setEditingId(null)}
                              >
                                取消
                              </button>
                            </>
                          ) : (
                            <>
                              <button
                                type="button"
                                className="quiet-button"
                                onClick={() => {
                                  setEditingId(item.id);
                                  setDraft(item.content);
                                  setError("");
                                }}
                              >
                                编辑
                              </button>
                              <button
                                type="button"
                                className="quiet-button pink"
                                disabled={busyId === item.id}
                                onClick={() => remove(item)}
                              >
                                删除
                              </button>
                            </>
                          )}
                        </div>
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

function FilePicker({ id, file, onChange, required = false }) {
  return (
    <label
      className={`file-picker${file ? " has-file" : ""}`}
      htmlFor={id}
      title={file ? file.name : undefined}
    >
      <input
        id={id}
        className="file-picker-input"
        type="file"
        accept="image/*,.gif,.webp"
        required={required}
        onChange={(event) => onChange(event.target.files?.[0] || null)}
      />
      <span className="file-picker-face">
        <span className="file-picker-action">
          {file ? file.name : "选择图片"}
        </span>
      </span>
    </label>
  );
}

function Goals({ refreshKey, token, onMutated }) {
  const confirm = useConfirm();
  const [includeClosed, setIncludeClosed] = useState(false);
  const [editingId, setEditingId] = useState(null);
  const [draft, setDraft] = useState(null);
  const [busyId, setBusyId] = useState(null);
  const [error, setError] = useState("");

  function startEdit(item) {
    setEditingId(item.id);
    setDraft({
      title: item.title || "",
      success_criteria: item.success_criteria || "",
      next_action: item.next_action || "",
      status: item.status,
      waiting_for: item.waiting_for || "",
      blocked_reason: item.blocked_reason || "",
    });
    setError("");
  }

  async function save(item) {
    setBusyId(item.id);
    setError("");
    try {
      await api(`/api/goals/${encodeURIComponent(item.id)}`, {
        method: "PATCH",
        token,
        body: draft,
      });
      setEditingId(null);
      onMutated();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusyId(null);
    }
  }

  async function remove(item) {
    const ok = await confirm({
      title: "取消这个任务？",
      message: "任务会标成已取消，之后还能在「含已结束」里看到。",
      confirmLabel: "取消任务",
      cancelLabel: "再想想",
    });
    if (!ok) return;
    setBusyId(item.id);
    setError("");
    try {
      await api(`/api/goals/${encodeURIComponent(item.id)}`, {
        method: "DELETE",
        token,
        body: { reason: "Cancelled from dashboard" },
      });
      onMutated();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusyId(null);
    }
  }

  return (
    <DataView path="/api/goals?all=true" refreshKey={refreshKey} token={token}>
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
              <div className="dash-tabs" role="tablist" aria-label="任务筛选">
                <button
                  type="button"
                  role="tab"
                  aria-selected={!includeClosed}
                  className={includeClosed ? "" : "active"}
                  onClick={() => setIncludeClosed(false)}
                >
                  <span>进行中</span>
                </button>
                <button
                  type="button"
                  role="tab"
                  aria-selected={includeClosed}
                  className={includeClosed ? "active" : ""}
                  onClick={() => setIncludeClosed(true)}
                >
                  <span>全部</span>
                </button>
              </div>
            </section>
            {error && <p className="form-error">{error}</p>}
            {visible.length ? (
              <section className="goal-grid">
                {visible.map((item) => (
                  <article className="goal-card" key={item.id}>
                    {editingId === item.id && draft ? (
                      <div className="edit-form">
                        <label>
                          状态
                          <select
                            value={draft.status}
                            onChange={(event) =>
                              setDraft({ ...draft, status: event.target.value })
                            }
                          >
                            {goalStatuses.map(([value, label]) => (
                              <option value={value} key={value}>
                                {label}
                              </option>
                            ))}
                          </select>
                        </label>
                        <label>
                          标题
                          <input
                            value={draft.title}
                            onChange={(event) =>
                              setDraft({ ...draft, title: event.target.value })
                            }
                          />
                        </label>
                        <label>
                          成功标准
                          <textarea
                            rows={3}
                            value={draft.success_criteria}
                            onChange={(event) =>
                              setDraft({
                                ...draft,
                                success_criteria: event.target.value,
                              })
                            }
                          />
                        </label>
                        <label>
                          下一步
                          <textarea
                            rows={2}
                            value={draft.next_action}
                            onChange={(event) =>
                              setDraft({ ...draft, next_action: event.target.value })
                            }
                          />
                        </label>
                        {draft.status === "waiting" && (
                          <label>
                            等待
                            <input
                              value={draft.waiting_for}
                              onChange={(event) =>
                                setDraft({
                                  ...draft,
                                  waiting_for: event.target.value,
                                })
                              }
                            />
                          </label>
                        )}
                        {draft.status === "blocked" && (
                          <label>
                            受阻原因
                            <input
                              value={draft.blocked_reason}
                              onChange={(event) =>
                                setDraft({
                                  ...draft,
                                  blocked_reason: event.target.value,
                                })
                              }
                            />
                          </label>
                        )}
                        <div className="card-actions">
                          <button
                            type="button"
                            className="quiet-button"
                            disabled={busyId === item.id}
                            onClick={() => save(item)}
                          >
                            保存
                          </button>
                          <button
                            type="button"
                            className="quiet-button pink"
                            onClick={() => setEditingId(null)}
                          >
                            取消
                          </button>
                        </div>
                      </div>
                    ) : (
                      <>
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
                        {["active", "waiting", "blocked"].includes(item.status) && (
                          <div className="card-actions">
                            <button
                              type="button"
                              className="quiet-button"
                              onClick={() => startEdit(item)}
                            >
                              编辑
                            </button>
                            <button
                              type="button"
                              className="quiet-button pink"
                              disabled={busyId === item.id}
                              onClick={() => remove(item)}
                            >
                              取消任务
                            </button>
                          </div>
                        )}
                      </>
                    )}
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

function Emotions({ refreshKey, token, onMutated }) {
  const confirm = useConfirm();
  const [slug, setSlug] = useState("");
  const [description, setDescription] = useState("");
  const [file, setFile] = useState(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [editing, setEditing] = useState(null);

  async function createEmotion(event) {
    event.preventDefault();
    setBusy("create");
    setError("");
    try {
      const formData = new FormData();
      formData.set("slug", slug.trim());
      formData.set("description", description.trim());
      if (file) formData.set("file", file);
      await api("/api/emotions", { method: "POST", token, formData });
      setSlug("");
      setDescription("");
      setFile(null);
      event.target.reset?.();
      onMutated();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy("");
    }
  }

  async function saveEmotion(item) {
    setBusy(item.slug);
    setError("");
    try {
      if (editing.file) {
        const formData = new FormData();
        formData.set("description", editing.description.trim());
        formData.set("file", editing.file);
        await api(`/api/emotions/${encodeURIComponent(item.slug)}`, {
          method: "PATCH",
          token,
          formData,
        });
      } else {
        await api(`/api/emotions/${encodeURIComponent(item.slug)}`, {
          method: "PATCH",
          token,
          body: { description: editing.description.trim() },
        });
      }
      setEditing(null);
      onMutated();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy("");
    }
  }

  async function remove(item) {
    const ok = await confirm({
      title: `删除表情 ${item.slug}？`,
      message: "贴纸会从表情库里拿掉，已经发出去的聊天不会跟着消失。",
      confirmLabel: "删除表情",
      cancelLabel: "先留着",
    });
    if (!ok) return;
    setBusy(item.slug);
    setError("");
    try {
      await api(`/api/emotions/${encodeURIComponent(item.slug)}`, {
        method: "DELETE",
        token,
      });
      onMutated();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy("");
    }
  }

  return (
    <DataView path="/api/emotions" refreshKey={refreshKey} token={token}>
      {({ items }) => (
        <>
          <form className="create-form" onSubmit={createEmotion}>
            <input
              id="emotion-slug"
              value={slug}
              onChange={(event) => setSlug(event.target.value)}
              placeholder="Slug"
              aria-label="Slug"
              required
            />
            <input
              id="emotion-desc"
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              placeholder="描述"
              aria-label="描述"
              required
            />
            <FilePicker
              id="emotion-file"
              file={file}
              required
              onChange={setFile}
            />
            <button
              className="quiet-button pink"
              type="submit"
              disabled={busy === "create"}
            >
              添加表情
            </button>
          </form>
          {error && <p className="form-error">{error}</p>}
          <section className="section-tools">
            <p>{items.length} 个可用表情</p>
          </section>
          {items.length ? (
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
                    {editing?.slug === item.slug ? (
                      <div className="edit-form compact">
                        <input
                          value={editing.description}
                          onChange={(event) =>
                            setEditing({
                              ...editing,
                              description: event.target.value,
                            })
                          }
                        />
                        <FilePicker
                          id={`emotion-edit-${item.slug}`}
                          file={editing.file}
                          onChange={(next) =>
                            setEditing({ ...editing, file: next })
                          }
                        />
                        <div className="card-actions">
                          <button
                            type="button"
                            className="quiet-button"
                            disabled={busy === item.slug}
                            onClick={() => saveEmotion(item)}
                          >
                            保存
                          </button>
                          <button
                            type="button"
                            className="quiet-button pink"
                            onClick={() => setEditing(null)}
                          >
                            取消
                          </button>
                        </div>
                      </div>
                    ) : (
                      <>
                        <h2>{item.slug}</h2>
                        <p>{item.description}</p>
                        <div className="card-actions">
                          <button
                            type="button"
                            className="quiet-button"
                            onClick={() =>
                              setEditing({
                                slug: item.slug,
                                description: item.description,
                                file: null,
                              })
                            }
                          >
                            编辑
                          </button>
                          <button
                            type="button"
                            className="quiet-button pink"
                            disabled={busy === item.slug}
                            onClick={() => remove(item)}
                          >
                            删除
                          </button>
                        </div>
                      </>
                    )}
                  </div>
                </article>
              ))}
            </section>
          ) : (
            <Empty text="还没有表情包，可以用上面的表单添加。" />
          )}
        </>
      )}
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

function TokenGate({ value, onChange, onUnlock }) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <div className="token-gate" role="dialog" aria-modal="true" aria-labelledby="token-gate-title">
      <form
        className="token-card"
        onSubmit={async (event) => {
          event.preventDefault();
          const next = value.trim();
          if (!next || busy) return;
          setBusy(true);
          setError("");
          try {
            const accessToken = await login(next);
            sessionStorage.setItem(TOKEN_KEY, accessToken);
            onUnlock(accessToken);
            onChange("");
          } catch {
            setError("通行证不对，再试一次。");
          } finally {
            setBusy(false);
          }
        }}
      >
        <div className="token-card-brand">
          <span className="brand-mark">M</span>
          <div>
            <p className="eyebrow">MOMOI // ACCESS</p>
            <h2 id="token-gate-title">老师，出示通行证！</h2>
          </div>
        </div>
        <p className="token-card-copy">
          嘿嘿，先把通行证交一下嘛。对上了才能进开发部的后台哦。
        </p>
        <label className="token-card-field">
          <span>通行证</span>
          <input
            type="password"
            autoFocus
            autoComplete="current-password"
            value={value}
            placeholder="输入通行证"
            onChange={(event) => onChange(event.target.value)}
          />
        </label>
        <p className={`token-card-error${error ? " is-visible" : ""}`} aria-live="polite">
          {error || "\u00a0"}
        </p>
        <button
          className="quiet-button token-card-submit"
          type="submit"
          disabled={!value.trim() || busy}
        >
          冲进后台！
        </button>
      </form>
    </div>
  );
}

function App() {
  const view = useHashRoute();
  const [refreshKey, setRefreshKey] = useState(0);
  const [token, setToken] = useState(readToken);
  const [tokenDraft, setTokenDraft] = useState("");
  const locked = !token;
  const [pageTitle, eyebrow] = pages[view];
  const View = viewComponents[view];

  useEffect(() => {
    if (!token) return undefined;
    const controller = new AbortController();
    api("/api/health", { token, signal: controller.signal }).catch((error) => {
      if (error.name === "AbortError") return;
      sessionStorage.removeItem(TOKEN_KEY);
      setToken("");
    });
    return () => controller.abort();
  }, [token]);

  return (
    <>
      <div
        className={`shell${locked ? " is-locked" : ""}`}
        aria-hidden={locked || undefined}
        inert={locked || undefined}
      >
        <aside className="sidebar">
          <a className="brand" href="#overview" aria-label="Momoi 首页">
            <span className="brand-mark">M</span>
            <span>
              <strong>Momoi</strong>
              <small>GAME DEV DEPT.</small>
            </span>
          </a>
          <p className="sidebar-label">NAV // CHANNELS</p>
          <nav aria-label="主导航">
            {navItems.map(([target, index, label]) => (
              <a
                href={`#${target}`}
                className={target === view ? "active" : ""}
                key={target}
                tabIndex={locked ? -1 : undefined}
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
              tabIndex={locked ? -1 : undefined}
              onClick={() => setRefreshKey((value) => value + 1)}
            >
              ↻ 刷新
            </button>
          </header>
          <div id="content">
            <View
              refreshKey={refreshKey}
              token={token}
              onMutated={() => setRefreshKey((value) => value + 1)}
            />
          </div>
        </main>
      </div>
      {locked && (
        <TokenGate
          value={tokenDraft}
          onChange={setTokenDraft}
          onUnlock={setToken}
        />
      )}
    </>
  );
}

createRoot(document.getElementById("root")).render(
  <StrictMode>
    <ConfirmProvider>
      <App />
    </ConfirmProvider>
  </StrictMode>,
);
