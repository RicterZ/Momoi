import { StrictMode, createContext, useContext, useEffect, useId, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

const TOKEN_KEY = "momoi-dashboard-token";
const REFLECTION_PAGE = 14;
const ConfirmContext = createContext(null);

const pages = {
  overview: ["今天也元气满满！", "MOMOI // HOME"],
  conversations: ["聊天记录", "MOMOI // CHAT LOG"],
  reflections: ["每日复盘", "MOMOI // SAVE DATA"],
  memories: ["记忆", "MOMOI // MEMORY"],
  emotions: ["表情包", "MOMOI // STICKERS"],
  goals: ["任务列表", "MOMOI // QUESTS"],
  thinking: ["思考记录", "MOMOI // THINKING"],
};

const navItems = [
  ["overview", "01", "主页"],
  ["conversations", "02", "聊天记录"],
  ["reflections", "03", "每日复盘"],
  ["memories", "04", "记忆"],
  ["emotions", "05", "表情包"],
  ["goals", "06", "任务列表"],
  ["thinking", "07", "思考"],
];

const thinkingStageLabels = {
  owner: "主人对话",
  webhook: "Webhook",
  heartbeat: "心跳",
  heartbeat_plan: "心跳规划",
  reflection: "复盘",
  goal: "目标执行",
  memory_maintenance: "记忆整理",
  episode_anneal: "对话记忆整理",
  episode_consolidate: "对话归并",
  reply_followup: "回复跟进",
};

function thinkingStageLabel(stage) {
  const key = String(stage || "").trim();
  if (!key) return "未标记";
  return thinkingStageLabels[key] || key;
}

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
  const local = localStorage.getItem(TOKEN_KEY) || "";
  if (local) return local;
  // Migrate older session-scoped tokens once.
  const session = sessionStorage.getItem(TOKEN_KEY) || "";
  if (session) {
    localStorage.setItem(TOKEN_KEY, session);
    sessionStorage.removeItem(TOKEN_KEY);
  }
  return session;
}

function writeToken(token) {
  localStorage.setItem(TOKEN_KEY, token);
  sessionStorage.removeItem(TOKEN_KEY);
}

function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
  sessionStorage.removeItem(TOKEN_KEY);
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
    ...(dateOnly
      ? {}
      : { hour: "2-digit", minute: "2-digit", second: "2-digit" }),
  }).format(date);
}

function activityMetaItems(activity, heartbeat) {
  const items = [{ label: "始于", value: formatDate(activity?.since) }];
  if (heartbeat?.running) {
    items.push({
      label: heartbeat.kind === "reply" ? "回复复查" : "心跳",
      value: "进行中",
    });
    return items;
  }
  if (heartbeat?.next_at) {
    items.push({
      label: "下次心跳",
      value: formatDate(heartbeat.next_at),
    });
  }
  if (heartbeat?.reply_check_at) {
    items.push({
      label: "回复复查",
      value: formatDate(heartbeat.reply_check_at),
    });
  }
  if (items.length === 1) {
    items.push({ label: "下次心跳", value: "未排程" });
  }
  return items;
}

function StampMeta({ items }) {
  return (
    <p className="stamp-meta">
      {items.map((item) => (
        <span className="stamp-meta-item" key={item.label}>
          <span className="stamp-meta-label">{item.label}</span>
          <span className="stamp-meta-value">{item.value}</span>
        </span>
      ))}
    </p>
  );
}

const HEART_PIXELS = [
  "..##.##..",
  ".#++#++#.",
  "#++*++++#",
  "#+++++++#",
  ".#+++++#.",
  "..#+++#..",
  "...#+#...",
  "....#....",
];

function heartFills(intensity, count = 5) {
  const total =
    Math.round(Math.max(0, Math.min(1, Number(intensity) || 0)) * count * 2) / 2;
  return Array.from({ length: count }, (_, index) =>
    Math.max(0, Math.min(1, total - index)),
  );
}

function PixelHeart({ fill, clipId }) {
  const pixels = HEART_PIXELS.flatMap((row, y) =>
    [...row].flatMap((cell, x) => (cell === "." ? [] : [{ x, y, cell }])),
  );
  const layer = (className, clip = false) => (
    <g
      className={className}
      clipPath={clip && fill < 1 ? `url(#${clipId})` : undefined}
    >
      {pixels.map(({ x, y, cell }) => (
        <rect
          key={`${className}-${x}-${y}`}
          x={x}
          y={y}
          width="1"
          height="1"
          className={
            cell === "#" ? "is-ink" : cell === "*" ? "is-shine" : "is-fill"
          }
        />
      ))}
    </g>
  );
  return (
    <svg
      className="pixel-heart"
      viewBox={`0 0 ${HEART_PIXELS[0].length} ${HEART_PIXELS.length}`}
      shapeRendering="crispEdges"
      aria-hidden="true"
    >
      {fill > 0 && fill < 1 && (
        <defs>
          <clipPath id={clipId}>
            <rect
              width={HEART_PIXELS[0].length * fill}
              height={HEART_PIXELS.length}
            />
          </clipPath>
        </defs>
      )}
      {layer("pixel-heart-empty")}
      {fill > 0 && layer("pixel-heart-fill", true)}
    </svg>
  );
}

function MoodHearts({ intensity }) {
  const uid = useId().replace(/[^a-zA-Z0-9_-]/g, "");
  const fills = heartFills(intensity);
  const level = fills.reduce((sum, value) => sum + value, 0);
  return (
    <div
      className="mood-hearts"
      role="img"
      aria-label={`情绪强度 ${level} / 5`}
    >
      {fills.map((fill, index) => (
        <PixelHeart key={index} fill={fill} clipId={`${uid}-${index}`} />
      ))}
    </div>
  );
}

function pixelCells(grid) {
  return grid.flatMap((row, y) =>
    [...row].flatMap((cell, x) => (cell === "." ? [] : [{ x, y, cell }])),
  );
}

function PixelSprite({ grid, className }) {
  return (
    <svg
      className={className}
      viewBox={`0 0 ${grid[0].length} ${grid.length}`}
      shapeRendering="crispEdges"
      aria-hidden="true"
    >
      {pixelCells(grid).map(({ x, y, cell }) => (
        <rect
          key={`${x}-${y}`}
          x={x}
          y={y}
          width="1"
          height="1"
          className={
            cell === "#" ? "is-ink" : cell === "*" ? "is-shine" : "is-fill"
          }
        />
      ))}
    </svg>
  );
}

const LED_PIXELS = [
  ".###.",
  "#+*+#",
  "#+++#",
  "#+++#",
  ".###.",
];

function PixelMeter({ value, cells = 8 }) {
  const filled = Math.round(Math.max(0, Math.min(1, Number(value) || 0)) * cells);
  return (
    <span className="pixel-meter" aria-hidden="true">
      {Array.from({ length: cells }, (_, index) => (
        <i key={index} className={index < filled ? "is-on" : undefined} />
      ))}
    </span>
  );
}

function formatTokens(value) {
  const n = Number(value) || 0;
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

function formatYuan(value) {
  const n = Number(value) || 0;
  if (n === 0) return "¥0.00";
  if (Math.abs(n) < 0.01) return `¥${n.toFixed(4)}`;
  return `¥${n.toFixed(2)}`;
}

function formatRate(value) {
  return `${Number(value || 0).toFixed(1)}%`;
}

function useNarrowScreen() {
  const [narrow, setNarrow] = useState(() =>
    typeof window !== "undefined" && window.matchMedia("(max-width: 760px)").matches,
  );
  useEffect(() => {
    const media = window.matchMedia("(max-width: 760px)");
    const sync = () => setNarrow(media.matches);
    sync();
    media.addEventListener("change", sync);
    return () => media.removeEventListener("change", sync);
  }, []);
  return narrow;
}

function summarizeDaily(rows) {
  const list = rows || [];
  const requests = list.reduce((sum, row) => sum + (Number(row.requests) || 0), 0);
  const input = list.reduce((sum, row) => sum + (Number(row.input_tokens) || 0), 0);
  const cacheRead = list.reduce((sum, row) => sum + (Number(row.cache_read_tokens) || 0), 0);
  const estimatedCost = list.reduce(
    (sum, row) => sum + (Number(row.estimated_cost) || 0),
    0,
  );
  return {
    requests,
    estimated_cost: estimatedCost,
    cache_hit_rate: input ? (cacheRead / input) * 100 : 0,
  };
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
      practice: "方法论",
      tool_skill: "工具经验",
    }[kind] || "记忆"
  );
}

function useHashRoute() {
  const read = () => {
    const raw = window.location.hash.slice(1);
    const [page, ...rest] = raw.split("/");
    return {
      view: pages[page] ? page : "overview",
      param: rest.length ? decodeURIComponent(rest.join("/")) : "",
    };
  };
  const [route, setRoute] = useState(read);
  useEffect(() => {
    const onChange = () => setRoute(read());
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return route;
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
      {(data) => <OverviewBody data={data} />}
    </DataView>
  );
}

function OverviewBody({ data }) {
  const narrow = useNarrowScreen();
  const usage = data.usage || {};
  const groups = [
    {
      kind: "archive",
      label: "Life archive",
      title: "生活档案",
      tone: "pink",
      items: [
        ["聊天主题", data.counts.conversations, "#conversations"],
        ["消息", data.counts.messages, "#conversations"],
        ["记忆", data.counts.memories, "#memories"],
        ["每日复盘", data.counts.reflections, "#reflections"],
      ],
    },
    {
      kind: "agenda",
      label: "Agenda",
      title: "待办事项",
      tone: "blue",
      items: [["进行中 Goals", data.counts.goals, "#goals"]],
    },
    {
      kind: "stickers",
      label: "Expression",
      title: "表情包",
      tone: "blue",
      items: [["可用素材", data.counts.emotions, "#emotions"]],
    },
  ];
  return (
    <>
      <OverviewSection
        label="Usage"
        note={narrow ? "账户余额与近 7 日调用" : "账户余额与近 30 日调用"}
      >
        <UsageChart
          rows={usage.daily}
          totals={usage.totals}
          today={usage.today}
          balance={data.balance}
          days={narrow ? 7 : 30}
        />
      </OverviewSection>
      <OverviewSection label="Now" note="当前活动与心情">
        <div className="overview-grid">
          <article className="panel">
            <span className="panel-label">Current activity</span>
            <h2 className="state-name">{data.activity.name}</h2>
            <p className="state-detail">
              {data.activity.result || "Momoi 正在按自己的节奏生活。"}
            </p>
            <StampMeta items={activityMetaItems(data.activity, data.heartbeat)} />
          </article>
          <article className="panel">
            <span className="panel-label">Mood</span>
            <div className="mood-title">
              <h2 className="state-name">{data.mood.state}</h2>
              <MoodHearts intensity={data.mood.intensity} />
            </div>
            <p className="state-detail">{data.mood.cause}</p>
            <StampMeta
              items={[{ label: "更新于", value: formatDate(data.mood.updated_at) }]}
            />
          </article>
        </div>
      </OverviewSection>
      <OverviewSection label="Records" note="生活记录与待办概览">
        <div className="overview-groups">
          {groups.map((group) => (
            <article
              className={`overview-group ${group.tone} ${group.kind}`}
              key={group.title}
            >
              <div className="overview-group-head">
                <span className="panel-label">{group.label}</span>
              </div>
              <h2>{group.title}</h2>
              <div className="overview-stats">
                {group.items.map(([label, value, href]) => (
                  <a href={href} key={label}>
                    <strong>{value}</strong>
                    <span>{label}</span>
                    <i aria-hidden="true">↗</i>
                  </a>
                ))}
              </div>
            </article>
          ))}
        </div>
      </OverviewSection>
    </>
  );
}

function OverviewSection({ label, note, children }) {
  return (
    <section className="overview-section">
      <div className="overview-section-head">
        <span className="panel-label">{label}</span>
        <p>{note}</p>
      </div>
      {children}
    </section>
  );
}

function shortDate(value) {
  const [, month, day] = String(value).split("-");
  if (!month || !day) return value;
  return `${Number(month)}/${Number(day)}`;
}

function linePath(points) {
  if (!points.length) return "";
  return points
    .map((point, index) => `${index === 0 ? "M" : "L"}${point.x.toFixed(1)} ${point.y.toFixed(1)}`)
    .join(" ");
}

function UsageChart({ rows, totals, today, balance, days = 30 }) {
  const [hover, setHover] = useState(null);
  const compact = days <= 7;
  const daily = (rows || []).slice(-days);
  const shown = compact ? summarizeDaily(daily) : totals;
  const todayStats = today || daily.at(-1) || {};
  const width = compact ? 390 : 720;
  const height = compact ? 220 : 176;
  const pad = compact
    ? { top: 14, right: 14, bottom: 36, left: 14 }
    : { top: 16, right: 18, bottom: 30, left: 18 };
  const innerW = width - pad.left - pad.right;
  const innerH = height - pad.top - pad.bottom;
  const costs = daily.map((row) => Number(row.estimated_cost) || 0);
  const requests = daily.map((row) => Number(row.requests) || 0);
  const maxCost = Math.max(...costs, 0);
  const maxReq = Math.max(...requests, 0);
  const count = Math.max(daily.length, 1);
  const xAt = (index) =>
    pad.left + (count === 1 ? innerW / 2 : (index / (count - 1)) * innerW);
  const yAt = (value, max, ratio) =>
    pad.top + innerH - (max ? (value / max) * innerH * ratio : 0);
  const costPoints = daily.map((row, index) => ({
    x: xAt(index),
    y: yAt(Number(row.estimated_cost) || 0, maxCost, 0.92),
    cost: Number(row.estimated_cost) || 0,
  }));
  const ticks = daily
    .map((row, index) => ({ row, index }))
    .filter(({ index }) => {
      if (count <= 8) return true;
      const step = Math.ceil((count - 1) / 6);
      return index === 0 || index === count - 1 || index % step === 0;
    });
  const active = hover == null ? null : daily[hover];

  return (
    <section className={`usage-chart-card${compact ? " is-compact" : ""}`}>
      <div className="usage-chart-head">
        <div className="usage-legend">
          <span className="usage-legend-item pink">估算金额</span>
          <span className="usage-legend-item blue bar">请求次数</span>
        </div>
      </div>
      <div className="usage-home-stats">
        <div>
          <span>账户余额</span>
          <strong>{formatYuan(balance?.total_balance)}</strong>
        </div>
        <div>
          <span>请求</span>
          <strong>{shown?.requests ?? 0}</strong>
        </div>
        <div>
          <span>今日缓存命中</span>
          <strong>{formatRate(todayStats.cache_hit_rate)}</strong>
          <PixelMeter value={(Number(todayStats.cache_hit_rate) || 0) / 100} />
        </div>
        <div>
          <span>今日估算金额</span>
          <strong>{formatYuan(todayStats.estimated_cost)}</strong>
        </div>
      </div>
      <div className="usage-chart-frame">
        <svg
          viewBox={`0 0 ${width} ${height}`}
          role="img"
          aria-label={`近 ${days} 日用量图`}
          onMouseLeave={() => {
            if (!compact) setHover(null);
          }}
        >
          {[0.25, 0.5, 0.75, 1].map((step) => (
            <line
              key={step}
              className="usage-gridline"
              x1={pad.left}
              x2={width - pad.right}
              y1={pad.top + innerH * (1 - step)}
              y2={pad.top + innerH * (1 - step)}
            />
          ))}
          {daily.map((row, index) => {
            const req = Number(row.requests) || 0;
            if (!req || !maxReq) return null;
            const slot = innerW / count;
            const barW = Math.round(
              compact
                ? Math.min(28, Math.max(slot * 0.62, 12))
                : Math.min(14, Math.max(slot * 0.55, 3)),
            );
            const barH = Math.max(
              2,
              Math.round((req / maxReq) * innerH * (compact ? 0.62 : 0.58)),
            );
            return (
              <rect
                key={`bar-${row.date}`}
                className="usage-req-bar"
                x={Math.round(xAt(index) - barW / 2)}
                y={Math.round(pad.top + innerH - barH)}
                width={barW}
                height={barH}
                shapeRendering="crispEdges"
              />
            );
          })}
          <path className="usage-line pink" d={linePath(costPoints)} />
          {daily.map((row, index) => (
            <g key={row.date}>
              {costPoints[index].cost > 0 && (
                <circle
                  className="usage-dot pink"
                  cx={costPoints[index].x}
                  cy={costPoints[index].y}
                  r={hover === index ? (compact ? 7 : 5.5) : compact ? 5.2 : 3.6}
                />
              )}
              <rect
                className="usage-hit"
                x={xAt(index) - innerW / count / 2}
                y={pad.top}
                width={Math.max(innerW / count, compact ? 28 : 12)}
                height={innerH}
                onMouseEnter={() => setHover(index)}
                onPointerDown={() => setHover(index)}
              />
            </g>
          ))}
          {ticks.map(({ row, index }) => (
            <text
              key={row.date}
              className="usage-tick"
              x={xAt(index)}
              y={height - (compact ? 14 : 10)}
              textAnchor="middle"
            >
              {shortDate(row.date)}
            </text>
          ))}
        </svg>
        {active && (
          <div className="usage-tooltip">
            <span className="panel-label">{active.date}</span>
            <strong>{formatYuan(active.estimated_cost)}</strong>
            <p>
              {active.requests} 次 · 输入 {formatTokens(active.input_tokens)} · 输出{" "}
              {formatTokens(active.output_tokens)}
            </p>
            <p>缓存 {formatRate(active.cache_hit_rate)}</p>
          </div>
        )}
      </div>
    </section>
  );
}

function Conversations({ refreshKey, token, routeParam }) {
  const [selected, setSelected] = useState(routeParam || null);
  const [detail, setDetail] = useState({ loading: false });
  useEffect(() => {
    if (routeParam) setSelected(routeParam);
  }, [routeParam]);
  function select(id) {
    setSelected(id);
    window.location.hash = id
      ? `conversations/${encodeURIComponent(id)}`
      : "conversations";
  }
  return (
    <DataView path="/api/conversations?limit=100" refreshKey={refreshKey} token={token}>
      {(data) => {
        const items = data.items || [];
        if (!items.length) return <Empty />;
        const activeId =
          (selected && items.some((item) => item.id === selected)
            ? selected
            : items[0].id);
        return (
          <ConversationLayout
            items={items}
            activeId={activeId}
            detail={detail}
            token={token}
            onSelect={select}
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

function compareMessages(left, right, newestFirst) {
  const delta = messageTime(left.created_at) - messageTime(right.created_at);
  if (delta !== 0) return newestFirst ? -delta : delta;
  const idDelta = Number(left.id || 0) - Number(right.id || 0);
  if (idDelta !== 0) return newestFirst ? -idDelta : idDelta;
  const ordinalDelta = Number(left.ordinal || 0) - Number(right.ordinal || 0);
  return newestFirst ? -ordinalDelta : ordinalDelta;
}

function SortArrow({ down }) {
  return (
    <svg
      className="sort-toggle-icon"
      viewBox="0 0 16 16"
      width="16"
      height="16"
      aria-hidden="true"
      focusable="false"
    >
      <path
        d={
          down
            ? "M3.2 6.2 L8 11 l4.8-4.8"
            : "M3.2 9.8 L8 5 l4.8 4.8"
        }
        fill="none"
        stroke="currentColor"
        strokeWidth="2.2"
        strokeLinecap="square"
        strokeLinejoin="miter"
      />
    </svg>
  );
}

function ConversationDetail({ item }) {
  const [newestFirst, setNewestFirst] = useState(true);
  const messages = [...(item.messages || [])].sort((left, right) =>
    compareMessages(left, right, newestFirst),
  );

  return (
    <>
      <header className="conversation-head">
        <div className="conversation-head-row">
          <span className="panel-label">{item.status} conversation</span>
          <button
            className={`sort-toggle${newestFirst ? " newest" : " oldest"}`}
            type="button"
            aria-label={newestFirst ? "切换为时间正序" : "切换为时间倒序"}
            title={newestFirst ? "时间倒序 · 点击正序" : "时间正序 · 点击倒序"}
            onClick={() => setNewestFirst((value) => !value)}
          >
            <SortArrow down={newestFirst} />
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
  const [items, setItems] = useState([]);
  const [status, setStatus] = useState({ loading: true });
  const [loadingMore, setLoadingMore] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const cursorRef = useRef(null);
  const busy = useRef(false);
  const moreRef = useRef(null);
  const loadOlderRef = useRef(async () => {});

  useEffect(() => {
    if (!token) {
      setStatus({ error: new Error("unauthorized") });
      return undefined;
    }
    const controller = new AbortController();
    busy.current = false;
    cursorRef.current = null;
    setItems([]);
    setHasMore(false);
    setLoadingMore(false);
    setStatus({ loading: true });
    api(`/api/reflections?limit=${REFLECTION_PAGE}`, {
      signal: controller.signal,
      token,
    })
      .then((data) => {
        cursorRef.current = data.next_cursor ?? null;
        setItems(data.items || []);
        setHasMore(data.next_cursor != null);
        setStatus({});
      })
      .catch((error) => {
        if (error.name !== "AbortError") setStatus({ error });
      });
    return () => controller.abort();
  }, [refreshKey, token]);

  loadOlderRef.current = async () => {
    if (busy.current || cursorRef.current == null || !token) return;
    busy.current = true;
    setLoadingMore(true);
    try {
      const query = new URLSearchParams({
        limit: String(REFLECTION_PAGE),
        cursor: cursorRef.current,
      });
      const data = await api(`/api/reflections?${query}`, { token });
      const incoming = data.items || [];
      cursorRef.current = data.next_cursor ?? null;
      if (incoming.length) {
        setItems((rows) => {
          const seen = new Set(rows.map((item) => item.id));
          return [...rows, ...incoming.filter((item) => !seen.has(item.id))];
        });
      }
      setHasMore(data.next_cursor != null);
    } catch (error) {
      if (error.name !== "AbortError") setStatus({ error });
    } finally {
      busy.current = false;
      setLoadingMore(false);
    }
  };

  useEffect(() => {
    const target = moreRef.current;
    if (!target || !hasMore || status.loading) return undefined;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          loadOlderRef.current();
        }
      },
      { rootMargin: "240px" },
    );
    observer.observe(target);
    return () => observer.disconnect();
  }, [hasMore, items.length, status.loading]);

  if (status.loading) return <Loading>正在读取复盘…</Loading>;
  if (status.error) return <ErrorState error={status.error} />;
  if (!items.length) return <Empty />;

  return (
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
                    {memory.evidence && <small>依据：{memory.evidence}</small>}
                  </div>
                ))}
              </div>
            )}
          </article>
        ))}
      </section>
      {hasMore || loadingMore ? (
        <button
          className="record-list-more"
          type="button"
          ref={moreRef}
          disabled={loadingMore}
          onClick={() => loadOlderRef.current()}
        >
          {loadingMore ? "正在加载…" : "更早的复盘"}
        </button>
      ) : null}
    </>
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
                        <StampMeta
                          items={[
                            {
                              label: "更新于",
                              value: formatDate(item.updated_at),
                            },
                            ...(item.expires_at
                              ? [
                                  {
                                    label: "有效至",
                                    value: formatDate(item.expires_at),
                                  },
                                ]
                              : []),
                          ]}
                        />
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

function dailyScheduleText(schedule) {
  const times = Array.isArray(schedule?.times) ? schedule.times : [];
  const timeText = times.length ? times.join("、") : "未设置时间";
  const timezoneText = schedule?.timezone ? ` · ${schedule.timezone}` : "";
  return `每天 ${timeText}${timezoneText}`;
}

function scheduleText(schedule, nextReview) {
  if (schedule?.kind === "daily") return dailyScheduleText(schedule);
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

function currentMonth() {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
}

function olderMonths(available, month) {
  return (available || []).filter((value) => value < month);
}

function thinkingFlowTitle(item) {
  const stages = (item.stages || [item.stage]).filter((stage) => stage !== undefined);
  const labels = stages.map((stage) => thinkingStageLabel(stage));
  return labels.length ? labels.join(" → ") : "未标记";
}

function thinkingStageCode(stage) {
  return (
    {
      owner: "MOMOI",
      webhook: "HOOK",
      heartbeat: "BEAT",
      heartbeat_plan: "HPLAN",
      reflection: "NOTE",
      goal: "GOAL",
      memory_maintenance: "TIDY",
      episode_anneal: "MEM",
      episode_consolidate: "MERGE",
      reply_followup: "FOLLOW",
    }[String(stage || "").trim()] || "MOMOI"
  );
}

function Thinking({ refreshKey, token }) {
  const [items, setItems] = useState([]);
  const [selectedId, setSelectedId] = useState("");
  const [detail, setDetail] = useState({});
  const [status, setStatus] = useState({ loading: true });
  const [loadingMore, setLoadingMore] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const pager = useRef({ available: [], month: "", cursor: null });
  const busy = useRef(false);
  const allowAuto = useRef(true);

  useEffect(() => {
    if (!token) {
      setStatus({ error: new Error("unauthorized") });
      return undefined;
    }
    const controller = new AbortController();
    busy.current = false;
    allowAuto.current = true;
    pager.current = { available: [], month: "", cursor: null };
    setItems([]);
    setSelectedId("");
    setDetail({});
    setHasMore(false);
    setLoadingMore(false);
    setStatus({ loading: true });

    (async () => {
      try {
        let rows = [];
        let month = currentMonth();
        let available = [];
        while (true) {
          const data = await api(
            `/api/thinking?month=${encodeURIComponent(month)}&limit=64`,
            { signal: controller.signal, token },
          );
          available = data.months?.length ? data.months : [data.month || month];
          const incoming = data.items || [];
          rows = mergeThinkingItems(rows, incoming);
          pager.current = {
            available,
            month: data.month || month,
            cursor: data.next_cursor ?? null,
          };
          if (rows.length || !olderMonths(available, pager.current.month).length) {
            break;
          }
          month = olderMonths(available, pager.current.month).at(-1);
        }
        if (controller.signal.aborted) return;
        setItems(rows);
        setHasMore(thinkingHasMore(pager.current));
        setStatus({});
      } catch (error) {
        if (error.name !== "AbortError") setStatus({ error });
      }
    })();

    return () => controller.abort();
  }, [refreshKey, token]);

  async function loadOlder() {
    if (busy.current || !thinkingHasMore(pager.current) || !token) return;
    busy.current = true;
    setLoadingMore(true);
    try {
      let added = 0;
      while (thinkingHasMore(pager.current)) {
        const nextMonth =
          pager.current.cursor != null
            ? pager.current.month
            : olderMonths(pager.current.available, pager.current.month).at(-1);
        if (!nextMonth) break;
        const query = new URLSearchParams({ month: nextMonth, limit: "64" });
        if (pager.current.cursor != null && nextMonth === pager.current.month) {
          query.set("cursor", String(pager.current.cursor));
        }
        const data = await api(`/api/thinking?${query}`, { token });
        const incoming = data.items || [];
        pager.current = {
          available: data.months?.length ? data.months : pager.current.available,
          month: data.month || nextMonth,
          cursor: data.next_cursor ?? null,
        };
        if (incoming.length) {
          added += incoming.length;
          setItems((rows) => mergeThinkingItems(rows, incoming));
          break;
        }
      }
      if (!added && !thinkingHasMore(pager.current)) setHasMore(false);
      else setHasMore(thinkingHasMore(pager.current));
    } catch (error) {
      if (error.name !== "AbortError") setStatus({ error });
    } finally {
      busy.current = false;
      setLoadingMore(false);
    }
  }

  if (status.loading) return <Loading>正在读取思考…</Loading>;
  if (status.error) return <ErrorState error={status.error} />;
  if (!items.length) return <Empty text="还没有思考记录。" />;
  const active = items.find((item) => item.id === selectedId) || items[0];
  return (
    <ThinkingLayout
      items={items}
      active={active}
      detail={detail}
      token={token}
      hasMore={hasMore}
      loadingMore={loadingMore}
      allowAuto={allowAuto}
      onLoadOlder={loadOlder}
      onSelect={setSelectedId}
      setDetail={setDetail}
    />
  );
}

function mergeThinkingItems(rows, incoming) {
  const seen = new Set(rows.map((item) => item.id));
  const next = incoming.filter((item) => item?.id && !seen.has(item.id));
  return next.length ? [...rows, ...next] : rows;
}

function thinkingHasMore(state) {
  return state.cursor != null || olderMonths(state.available, state.month).length > 0;
}

function ThinkingLayout({
  items,
  active,
  detail,
  token,
  hasMore,
  loadingMore,
  allowAuto,
  onLoadOlder,
  onSelect,
  setDetail,
}) {
  const turnId = String(active?.turn_id || "").trim();
  const activeId = String(active?.id || "");
  const listRef = useRef(null);
  const moreRef = useRef(null);
  const loadOlderRef = useRef(onLoadOlder);
  loadOlderRef.current = onLoadOlder;
  useEffect(() => {
    if (!token || !activeId) {
      setDetail({});
      return undefined;
    }
    const controller = new AbortController();
    setDetail({ loading: true });
    const path = turnId
      ? `/api/thinking/${encodeURIComponent(turnId)}`
      : `/api/thinking/calls/${encodeURIComponent(activeId.replace(/^call:/, ""))}`;
    api(path, { signal: controller.signal, token })
      .then((data) => setDetail({ data }))
      .catch((error) => {
        if (error.name !== "AbortError") setDetail({ error });
      });
    return () => controller.abort();
  }, [activeId, setDetail, token, turnId]);

  useEffect(() => {
    const root = listRef.current;
    const target = moreRef.current;
    if (!root || !target || !hasMore) return undefined;
    const observer = new IntersectionObserver(
      (entries) => {
        if (!entries.some((entry) => entry.isIntersecting)) return;
        if (!allowAuto.current) return;
        allowAuto.current = false;
        loadOlderRef.current();
      },
      { root, rootMargin: "80px" },
    );
    observer.observe(target);
    return () => observer.disconnect();
  }, [allowAuto, hasMore, items.length]);

  return (
    <section className="record-layout">
      <div
        className="record-list"
        aria-label="思考 Turn"
        ref={listRef}
        onScroll={() => {
          allowAuto.current = true;
        }}
      >
        {items.map((item) => (
          <button
            className={`record-item ${item.id === activeId ? "active" : ""}`}
            type="button"
            key={item.id}
            onClick={() => onSelect(item.id)}
          >
            <h3>{thinkingFlowTitle(item)}</h3>
            <p>{item.excerpt || "这次没有可见推理。"}</p>
            <time>
              {formatDate(item.updated_at || item.created_at)}
              {item.call_count > 1 ? ` · ${item.call_count} 次调用` : ""}
            </time>
          </button>
        ))}
        {hasMore || loadingMore ? (
          <button
            className="record-list-more"
            type="button"
            ref={moreRef}
            disabled={loadingMore}
            onClick={onLoadOlder}
          >
            {loadingMore ? "正在加载…" : "更早的思考"}
          </button>
        ) : null}
      </div>
      <div className="conversation">
        {detail.loading && <Loading>正在读取思考…</Loading>}
        {detail.error && <ErrorState error={detail.error} />}
        {detail.data && (
          <ThinkingDetail
            item={active}
            calls={detail.data.items || (detail.data.item ? [detail.data.item] : [])}
            recall={detail.data.recall}
          />
        )}
      </div>
    </section>
  );
}

function RecallDetail({ recall }) {
  if (!recall) return null;
  const units = recall.units || [];
  const evidence = [
    ...(recall.memories || []).map((item) => ({
      key: `memory:${item.kind}:${item.key}`,
      label: item.kind || "memory",
      title: item.key,
      content: item.content,
    })),
    ...(recall.reflections || []).map((item) => ({
      key: `reflection:${item.kind}:${item.key}`,
      label: item.kind || "reflection",
      title: item.key,
      content: item.content,
    })),
    ...(recall.episodes || []).map((item) => ({
      key: `episode:${item.id}`,
      label: item.relation || "episode",
      title: item.title || item.id,
      content: item.summary,
    })),
  ];
  return (
    <section className="recall-panel">
      <div className="card-head">
        <div>
          <span className="panel-label">CONTEXT // RECALL</span>
          <h3>上下文召回</h3>
        </div>
        <span className="status">{recall.state || "unknown"}</span>
      </div>
      {units.map((unit) => (
        <article className="recall-unit" key={unit.id}>
          <div className="memory-head">
            <span className="memory-kind">{unit.mode}</span>
            <strong>{unit.intent || unit.id}</strong>
          </div>
          {unit.reused_from ? (
            <p className="secondary">复用 Turn：{unit.reused_from}</p>
          ) : null}
          {(unit.queries || []).map((query, index) => (
            <div className="recall-query" key={`${unit.id}:${index}`}>
              <p>{query.semantic}</p>
              {!!query.keywords?.length && (
                <div className="tags">
                  {query.keywords.map((keyword) => (
                    <span className="tag" key={keyword}>{keyword}</span>
                  ))}
                </div>
              )}
            </div>
          ))}
        </article>
      ))}
      {!!evidence.length && (
        <div className="recall-evidence">
          {evidence.map((item) => (
            <article className="memory" key={item.key}>
              <div className="memory-head">
                <span className="memory-kind">{item.label}</span>
                <strong>{item.title}</strong>
              </div>
              {item.content ? <p className="secondary">{item.content}</p> : null}
            </article>
          ))}
        </div>
      )}
      {recall.status ? (
        <pre className="recall-status">{recall.status}</pre>
      ) : null}
    </section>
  );
}

function ThinkingDetail({ item, calls, recall }) {
  const flow = [...calls].sort((left, right) => {
    const time = Number(left.created_at || 0) - Number(right.created_at || 0);
    return time !== 0 ? time : Number(left.round || 0) - Number(right.round || 0);
  });
  if (!flow.length) return <Empty text="这个 Turn 还没有思考记录。" />;
  const episodeId = item?.episode_id;
  const episodeTitle = item?.episode_title || "查看聊天记录";
  return (
    <>
      <header className="conversation-head">
        <h2>{thinkingFlowTitle(item || { stages: flow.map((call) => call.stage) })}</h2>
        {episodeId ? (
          <a
            className="tag thinking-conversation"
            href={`#conversations/${encodeURIComponent(episodeId)}`}
          >
            {episodeTitle}
          </a>
        ) : null}
      </header>
      <RecallDetail recall={recall} />
      <div className="messages">
        {flow.map((call) => (
          <article className="message" key={call.call_id}>
            <div className="message-role momoi">
              {thinkingStageCode(call.stage)}
            </div>
            <div className="message-body">
              <p className="message-content thinking-body">
                {call.reasoning || call.excerpt || "这次调用没有可见推理。"}
              </p>
              <div className="message-meta">
                <time>{formatDate(call.created_at)}</time>
                <span>
                  {thinkingStageLabel(call.stage)}
                  {call.tools?.length ? ` · ${call.tools.join(" / ")}` : ""}
                </span>
              </div>
            </div>
          </article>
        ))}
      </div>
    </>
  );
}

const viewComponents = {
  overview: Overview,
  conversations: Conversations,
  reflections: Reflections,
  memories: Memories,
  emotions: Emotions,
  goals: Goals,
  thinking: Thinking,
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
            writeToken(accessToken);
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
  const { view, param } = useHashRoute();
  const [refreshKey, setRefreshKey] = useState(0);
  const [token, setToken] = useState(readToken);
  const [tokenDraft, setTokenDraft] = useState("");
  const [version, setVersion] = useState("");
  const locked = !token;
  const [pageTitle, eyebrow] = pages[view];
  const View = viewComponents[view];
  const isRecord = view === "conversations" || view === "thinking";

  useEffect(() => {
    if (!token) return undefined;
    const controller = new AbortController();
    fetch("/api/health", {
      headers: {
        Accept: "application/json",
        Authorization: `Bearer ${token}`,
      },
      cache: "no-store",
      signal: controller.signal,
    })
      .then(async (response) => {
        // Only drop a saved JWT when the server rejects it; network blips keep it.
        if (response.status === 401) {
          clearToken();
          setToken("");
          return;
        }
        if (!response.ok) return;
        const payload = await response.json();
        if (typeof payload.version === "string" && payload.version.trim()) {
          setVersion(payload.version.trim());
        }
      })
      .catch((error) => {
        if (error.name === "AbortError") return;
      });
    return () => controller.abort();
  }, [token]);

  return (
    <>
      <div
        className={`shell${locked ? " is-locked" : ""}${isRecord ? " is-record" : ""}`}
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
            <span className="status-led" aria-hidden="true">
              <PixelSprite className="pixel-led" grid={LED_PIXELS} />
            </span>
            <span>SYSTEM ONLINE</span>
            {version ? <span className="sidebar-version">{version}</span> : null}
          </div>
        </aside>
        <main className={isRecord ? "is-record" : undefined}>
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
              routeParam={param}
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
