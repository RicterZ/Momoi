import { useMemo } from "react";
import { MemoryTriggerTags } from "./MemoryMetadata.jsx";

export function memoryEntries(snapshot) {
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
  });
}

export default function TrajectoryMemories({ snapshot }) {
  const entries = useMemo(() => memoryEntries(snapshot), [snapshot]);
  return (
    <section className="trajectory-memories" aria-label="注入记忆">
      <h4>记忆 <span className="trajectory-memory-count">{snapshot ? entries.length : "—"}</span></h4>
      {entries.length ? (
        <ul className="trajectory-memory-list">
          {entries.map((entry, index) => (
            <li className="trajectory-memory" key={`${entry.source}-${entry.id || index}-${index}`}>
              <div className="trajectory-memory-head">
                <span className={`trajectory-memory-source is-${entry.source}`}>{entry.label}</span>
                {entry.id && <code aria-label={`记忆编号 ${entry.id}`}>#{entry.id}</code>}
              </div>
              {entry.content ? <p className="trajectory-memory-content">{entry.content}</p> : <p>此条记忆内容暂时无法显示。</p>}
              {!!entry.notes?.length && <dl className="trajectory-memory-notes">
                {entry.notes.map((note, noteIndex) => <div key={noteIndex}><dt>{note.label}</dt><dd>{note.text}</dd></div>)}
              </dl>}
              <MemoryTriggerTags triggers={entry.triggers} />
            </li>
          ))}
        </ul>
      ) : <p className="trajectory-memory-empty">{snapshot ? "本次请求未注入触发词或领域记忆。" : "此记录未保存注入记忆快照。"}</p>}
    </section>
  );
}
