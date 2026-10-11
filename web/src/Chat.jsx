import { useEffect, useLayoutEffect, useRef, useState } from "react";
import EmotionContent from "./EmotionContent.jsx";
import "./chat.css";

function mergeMessages(old, fresh) {
  const byId = new Map(old.map(message => [message.id, message]));
  fresh.forEach(message => byId.set(message.id, message));
  return [...byId.values()].sort((a, b) => a.created_at - b.created_at || (String(a.id) < String(b.id) ? -1 : String(a.id) > String(b.id) ? 1 : 0));
}

function ChatAttachment({ message, token }) {
  const [url, setUrl] = useState("");
  const [error, setError] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    let objectUrl;
    setUrl(""); setError(false);
    fetch(`/api/chat/media/${message.media_id}`, { headers: { Authorization: `Bearer ${token}` }, signal: controller.signal })
      .then(response => { if (!response.ok) throw new Error(); return response.blob(); })
      .then(blob => { if (!controller.signal.aborted) { objectUrl = URL.createObjectURL(blob); setUrl(objectUrl); } })
      .catch(() => { if (!controller.signal.aborted) setError(true); });
    return () => { controller.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [message.media_id, token]);
  if (error) return <span>附件暂时不可用</span>;
  if (!url) return <span>加载附件…</span>;
  if (message.kind === "image") return <img className="chat-attachment" src={url} alt="Momoi 发来的图片" />;
  if (["audio", "voice"].includes(message.kind)) return <audio className="chat-attachment" src={url} controls />;
  if (message.kind === "video") return <video className="chat-attachment" src={url} controls />;
  return <a href={url} download="momoi-attachment">下载附件</a>;
}

export default function Chat({ token, request, refreshKey }) {
  const [messages, setMessages] = useState([]);
  const [runtime, setRuntime] = useState(null);
  const [typing, setTyping] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [connectionError, setConnectionError] = useState("");
  const [hasMore, setHasMore] = useState(false);
  const [olderBusy, setOlderBusy] = useState(false);
  const [reload, setReload] = useState(0);
  const scroll = useRef(null);
  const stick = useRef(true);
  const submission = useRef(null);
  const olderOffset = useRef(null);
  const sending = useRef(false);
  const historyLoaded = useRef(false);
  const paused = !runtime?.runtime_active || runtime?.budget?.blocked;

  useEffect(() => {
    const controller = new AbortController();
    let timer;
    async function poll() {
      try {
        const data = await request("/api/chat/messages", { token, signal: controller.signal });
        if (controller.signal.aborted) return;
        setMessages(previous => mergeMessages(previous, data.messages || []));
        setRuntime(data.runtime); setTyping(data.typing);
        if (!historyLoaded.current) { setHasMore(data.has_more); historyLoaded.current = true; }
        setConnectionError("");
      } catch (error) {
        if (!controller.signal.aborted) setConnectionError(error.message);
      } finally {
        if (!controller.signal.aborted) timer = setTimeout(poll, 500);
      }
    }
    poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [token, request, refreshKey, reload]);

  useLayoutEffect(() => {
    if (!scroll.current) return;
    if (olderOffset.current !== null) {
      scroll.current.scrollTop += scroll.current.scrollHeight - olderOffset.current;
      olderOffset.current = null;
    } else if (stick.current) scroll.current.scrollTop = scroll.current.scrollHeight;
  }, [messages, typing]);

  useEffect(() => {
    const box = scroll.current;
    const observer = new ResizeObserver(() => {
      if (stick.current) box.scrollTop = box.scrollHeight;
    });
    observer.observe(box);
    for (const child of box.children) observer.observe(child);
    return () => observer.disconnect();
  }, [messages, typing]);

  async function loadOlder() {
    if (olderBusy || !messages.length) return;
    setOlderBusy(true);
    try {
      const data = await request(`/api/chat/messages?before=${messages[0].created_at}&before_id=${encodeURIComponent(messages[0].id)}`, { token });
      olderOffset.current = scroll.current?.scrollHeight ?? null;
      setMessages(previous => mergeMessages(previous, data.messages || []));
      setHasMore(data.has_more);
    } catch (error) { setError(error.message); }
    finally { setOlderBusy(false); }
  }

  async function send(event) {
    event.preventDefault();
    const text = draft.trim();
    if (!text || paused || sending.current) return;
    sending.current = true; setBusy(true); setError("");
    if (submission.current?.text !== text) submission.current = { text, id: crypto.randomUUID() };
    try {
      await request("/api/chat/messages", { token, method: "POST", body: submission.current });
      setDraft(""); submission.current = null; stick.current = true;
      setReload(value => value + 1);
    } catch (error) { setError(error.message); }
    finally { sending.current = false; setBusy(false); }
  }

  return <section className="chat-room" aria-label="与 Momoi 聊天">
    <header className="chat-room-head"><div className="chat-avatar" aria-hidden="true">M</div><div><strong>Momoi</strong><p>在这里，也接着你们的日常。</p></div><span className={`chat-presence${paused ? " paused" : ""}`}><span className="chat-pixel-dot" />{runtime ? (paused ? "暂停中" : "在线") : "连接中"}</span></header>
    <div className="chat-scroll" ref={scroll} role="log" aria-label="聊天消息" onScroll={event => { const box = event.currentTarget; stick.current = box.scrollHeight - box.scrollTop - box.clientHeight < 80; }}>
      {hasMore && <button className="chat-history-button" type="button" disabled={olderBusy} onClick={loadOlder}>{olderBusy ? "加载中…" : "查看更早的消息"}</button>}
      {!messages.length && !connectionError ? <div className="chat-welcome"><span className="chat-welcome-mark">M</span><span className="panel-label">HELLO!</span><h2>来聊两句？</h2><p>从一句问候开始，或者接着之前的话题。</p></div> : messages.map((message, index) => {
        const date = new Date(message.created_at * 1000);
        const showDate = !index || date.toLocaleDateString() !== new Date(messages[index - 1].created_at * 1000).toLocaleDateString();
        const next = messages[index + 1];
        const showTime = !message.turn_id || next?.role !== message.role || next?.turn_id !== message.turn_id || Math.floor(next.created_at / 60) !== Math.floor(message.created_at / 60);
        const deliveryLabel = message.role === "user" ? "已发送" : ({ queued: "待发送", cancelled: "已取消", failed: "发送失败", uncertain: "发送状态待确认" })[message.delivery_state];
        return <div key={message.id}>{showDate && <div className="chat-date"><span>{date.toLocaleDateString("zh-CN")}</span></div>}<div className={`chat-line ${message.role === "user" ? "owner" : "momoi"}`}><div className="chat-message">{message.media_id ? <ChatAttachment message={message} token={token} /> : <EmotionContent text={message.content} className="chat-text" />}</div>{(showTime || deliveryLabel) && <span className="chat-time">{showTime && date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}{deliveryLabel && `${showTime ? " · " : ""}${deliveryLabel}`}</span>}</div></div>;
      })}
      {typing && <div className="chat-line momoi"><div className="chat-message chat-typing" role="status" aria-label="Momoi 正在回复"><i/><i/><i/></div><span className="chat-time">Momoi 正在回复…</span></div>}
    </div>
    {(connectionError || error) && <div className="chat-budget-note" role="alert">{error || connectionError}</div>}
    {runtime && paused && <div className="chat-budget-note" role="status"><span>{runtime.budget?.blocked ? runtime.budget.reason : "Momoi 暂停运行，请在设置中检查运行状态。"}</span><a href={runtime.budget?.blocked ? "#settings/budget" : "#settings"}>前往设置 ↗</a></div>}
    <form className="chat-composer" onSubmit={send}><label className="sr-only" htmlFor="chat-draft">想和 Momoi 说什么</label><textarea id="chat-draft" value={draft} onChange={event => setDraft(event.target.value)} placeholder={paused ? "恢复运行后，可以继续聊天" : "想和 Momoi 说些什么？"} disabled={paused || busy} maxLength={10000} rows={2} onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); send(event); } }}/><button className="quiet-button pink chat-send" disabled={paused || busy || !draft.trim()} type="submit" aria-label="发送消息">{busy ? "发送中" : "发送"}<svg className="chat-send-icon" width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M12.5 3.5v5h-9m3-3-3 3 3 3" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"/></svg></button></form>
  </section>;
}
