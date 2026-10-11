import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import "../src/styles.css";
import "./chat.css";

const navigation = ["主页", "聊天", "话题", "思考", "复盘", "记忆", "表情", "任务", "监控", "工具", "设置"];
const sample = [
  { who: "owner", text: "小桃，昨天那个游戏开头我又改了一版。", time: "14:32" },
  { who: "momoi", text: "老师！是那个勇者出门就迷路的开头吗？", time: "14:32" },
  { who: "momoi", text: "快给我看看！\n这次可不能又让 NPC 把答案全说出来了，我还想让玩家自己猜呢。", time: "14:32" },
  { who: "owner", text: "这次把解释删了，先让玩家去找丢失的地图。", time: "14:33" },
  { who: "momoi", text: "哦哦，这样才有开始冒险的感觉嘛！\n地图找回来之后才发现画反了——嘿嘿，已经看到玩家的表情了。", time: "14:33" },
];

function ChatPreview() {
  const initial = new URLSearchParams(location.search).get("state") || "conversation";
  const [state, setState] = useState(initial);
  const [draft, setDraft] = useState("");
  const [messages, setMessages] = useState(initial === "empty" ? [] : sample);
  const paused = state === "budget";
  function send(e) {
    e.preventDefault();
    if (!draft.trim() || paused) return;
    setMessages([...messages, { who: "owner", text: draft.trim(), time: "刚刚" }]);
    setDraft(""); setState("typing");
  }
  return <div className="shell is-record chat-preview-shell">
    <aside className="sidebar">
      <a className="brand" href="/"><span className="brand-mark">M</span><span><strong>Momoi</strong><small>GAME DEV DEPT.</small></span></a>
      <p className="sidebar-label">NAV // CHANNELS</p>
      <nav aria-label="主导航">{navigation.map((label,i) => <a key={label} href="#" className={i === 1 ? "active" : ""}><span>{String(i+1).padStart(2,"0")}</span><strong>{label}</strong></a>)}</nav>
      <div className="sidebar-foot"><span className="chat-pixel-dot" /><span>SYSTEM ONLINE</span></div>
    </aside>
    <main className="is-record chat-main">
      <header className="topbar"><div><p className="eyebrow">MOMOI // CHAT</p><h1>聊天</h1></div>
        <label className="chat-preview-controls">预览状态<select aria-label="预览状态" value={state} onChange={e => { setState(e.target.value); setMessages(e.target.value === "empty" ? [] : sample); }}><option value="conversation">日常对话</option><option value="empty">初次聊天</option><option value="typing">正在回复</option><option value="budget">预算暂停</option></select></label>
      </header>
      <section className="chat-room" aria-label="与 Momoi 聊天">
        <header className="chat-room-head"><div className="chat-avatar" aria-hidden="true">M</div><div><strong>Momoi</strong><p>在这里，也接着你们的日常。</p></div><span className={`chat-presence${paused ? " paused" : ""}`}><span className="chat-pixel-dot" />{paused ? "暂停中" : "在线"}</span></header>
        <div className="chat-scroll" role="log" aria-label="聊天消息">
          {!messages.length ? <div className="chat-welcome"><span className="chat-welcome-mark">M</span><span className="panel-label">HELLO!</span><h2>来聊两句？</h2><p>从一句问候开始，或者接着之前的话题。</p></div> : <><div className="chat-date"><span>今天 · 10 月 11 日</span></div>{messages.map((message,i) => <div key={i} className={`chat-line ${message.who}`}><div className="chat-message"><p>{message.text}</p></div><span className="chat-time">{message.time}{message.who === "owner" ? " · 已发送" : ""}</span></div>)}</>}
          {state === "typing" && <div className="chat-line momoi"><div className="chat-message chat-typing" role="status" aria-label="Momoi 正在回复"><i/><i/><i/></div><span className="chat-time">Momoi 正在回复…</span></div>}
        </div>
        {paused && <div className="chat-budget-note" role="status"><span>今日费用已达预算，Momoi 暂停运行。</span><a href="/#settings/budget">调整预算 ↗</a></div>}
        <form className="chat-composer" onSubmit={send}><label className="sr-only" htmlFor="chat-draft">想和 Momoi 说什么</label><textarea id="chat-draft" value={draft} onChange={e => setDraft(e.target.value)} placeholder={paused ? "预算恢复后，可以继续聊天" : "想和 Momoi 说些什么？"} disabled={paused} rows={2} onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); send(e); } }}/><button className="quiet-button pink chat-send" disabled={paused || !draft.trim()} type="submit" aria-label="发送消息">发送<svg className="chat-send-icon" width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M12.5 3.5v5h-9m3-3-3 3 3 3" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"/></svg></button></form>
      </section>
      <p className="chat-preview-note">视觉预览 · 示例对话，未连接模型或真实聊天记录</p>
    </main>
  </div>;
}

createRoot(document.getElementById("root")).render(<ChatPreview />);
