import { useMemo } from "react";
import { MemoryTriggerTags } from "./MemoryMetadata.jsx";

export function memoryEntries(snapshot) {
  return [["triggered_memories", "触发词"], ["scoped_memories", "领域"]].flatMap(([source, label]) => {
    const raw = snapshot?.[source]?.trim();
    if (!raw) return [];
    const doc = new DOMParser().parseFromString(`<memories>${raw}</memories>`, "application/xml");
    if (doc.querySelector("parsererror")) return [{ source, label, raw }];
    return Array.from(doc.documentElement.children).filter(node => node.tagName === "memory").map(node => ({
      source, label,
      id: node.getAttribute("id"),
      key: node.getAttribute("key"),
      content: node.querySelector("content")?.textContent ?? node.textContent,
      triggers: Array.from(node.querySelectorAll("trigger"), trigger => trigger.textContent),
      raw: new XMLSerializer().serializeToString(node),
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
                {entry.id && <code>#{entry.id}</code>}
                {entry.key && <span className="trajectory-memory-key">{entry.key}</span>}
              </div>
              {entry.content ? <p className="trajectory-memory-content">{entry.content}</p> : <p>无法解析此快照，请查看原始内容。</p>}
              <MemoryTriggerTags triggers={entry.triggers} />
              <details>
                <summary>原始快照</summary>
                <pre className="flow-json">{entry.raw}</pre>
              </details>
            </li>
          ))}
        </ul>
      ) : <p className="trajectory-memory-empty">{snapshot ? "本次请求未注入触发词或领域记忆。" : "此记录未保存注入记忆快照。"}</p>}
    </section>
  );
}
