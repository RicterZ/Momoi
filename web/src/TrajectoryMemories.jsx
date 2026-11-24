import { useMemo } from "react";
import { MemoryTriggerTags } from "./MemoryMetadata.jsx";

const RELATION_LABELS = { follows_up: "后续", followed_by: "被后续", revises: "修订", revised_by: "被修订", context: "背景" };

function episodeEntry(record, label) {
  return {
    source: "triggered_episodes", label, id: record.id, episode: true,
    content: record.title,
    notes: [
      ...(record.summary ? [{ label: "摘要", text: record.summary }] : []),
      ...(record.relations || []).map(pointer => ({
        label: RELATION_LABELS[pointer.relation] || pointer.relation,
        text: pointer.title ? `${pointer.title}（${pointer.id}）` : pointer.id,
      })),
      ...(record.supplements || []).map(child => ({ label: "补充话题", text: child.title ? `${child.title}（${child.id}）` : child.id })),
    ],
    triggers: [],
  };
}

export function memoryEntries(snapshot) {
  const episodes = (Array.isArray(snapshot?.triggered_episodes) ? snapshot.triggered_episodes : [])
    .map(record => episodeEntry(record, record.source === "reply" ? "回复触发话题" : "触发话题"));
  return [["triggered_memories", "触发词"], ["scoped_memories", "领域"]].flatMap(([source, label]) => {
    const raw = snapshot?.[source]?.trim();
    if (!raw) return [];
    const doc = new DOMParser().parseFromString(`<memories>${raw}</memories>`, "application/xml");
    if (doc.querySelector("parsererror")) return [{ source, label }];
    return Array.from(doc.documentElement.children).filter(node => node.tagName === "memory").map(node => ({
      source, label,
      id: node.getAttribute("id"),
      content: node.querySelector("content")?.textContent ?? Array.from(node.childNodes)
        .filter(child => child.nodeType === Node.TEXT_NODE).map(child => child.textContent).join("").trim(),
      notes: Array.from(node.children).filter(child => !["content", "trigger"].includes(child.tagName))
        .map(child => ({ label: { verification: "验证状态", caution: "注意", basis: "依据", pitfall: "易错点" }[child.tagName] || child.tagName, text: child.textContent })),
      triggers: Array.from(node.querySelectorAll("trigger"), trigger => trigger.textContent),
    }));
  }).concat(episodes);
}

export const memoryEntryKey = (entry) => `${entry.source}:${entry.id}`;

export default function TrajectoryMemories({ snapshot, seen }) {
  const all = useMemo(() => memoryEntries(snapshot), [snapshot]);
  const entries = all.filter(entry => !entry.id || !seen?.has(memoryEntryKey(entry)));
  if (all.length && !entries.length) return null;
  return (
    <section className="trajectory-memories" aria-label="注入记忆">
      <h4>记忆 <span className="trajectory-memory-count">{snapshot ? entries.length : "—"}</span></h4>
      {entries.length ? (
        <ul className="trajectory-memory-list">
          {entries.map((entry, index) => (
            <li className="trajectory-memory" key={`${entry.source}-${entry.id || index}-${index}`}>
              <div className="trajectory-memory-head">
                <span className={`trajectory-memory-source is-${entry.source}`}>{entry.label}</span>
                {entry.id && <code aria-label={`${entry.episode ? "话题" : "记忆"}编号 ${entry.id}`}>{entry.episode ? entry.id : `#${entry.id}`}</code>}
              </div>
              {entry.content ? <p className="trajectory-memory-content">{entry.content}</p> : <p>此条内容暂时无法显示。</p>}
              {!!entry.notes?.length && <dl className="trajectory-memory-notes">
                {entry.notes.map((note, noteIndex) => <div key={noteIndex}><dt>{note.label}</dt><dd>{note.text}</dd></div>)}
              </dl>}
              <MemoryTriggerTags triggers={entry.triggers} />
            </li>
          ))}
        </ul>
      ) : <p className="trajectory-memory-empty">{snapshot ? "本次请求未注入触发词、领域记忆或触发话题。" : "此记录未保存注入记忆快照。"}</p>}
    </section>
  );
}
