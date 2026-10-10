import { memoryScope, memoryScopeLabel } from "./memoryInventory.js";

export default function MemoryMetadata({ item }) {
  const reflection = item.activation === "reflection" || item.resource === "reflection-candidates";
  const triggers = item.meta?.triggers || item.triggers || [];
  const scope = memoryScope(item);
  if (!reflection && item.activation !== "recall" && !scope) return null;
  const triggerActive = item.activation === "recall" && !scope;
  return (
    <div className="memory-metadata">
      {scope && <p className="memory-scope">作用于 {memoryScopeLabel(item)}</p>}
      {(reflection || item.activation === "recall") && <>
        <div className="memory-triggers">
          <span>触发词：</span>
          {triggers.length ? (
            <ul className="memory-chips" aria-label="记忆触发词">
              {triggers.map((word) => <li className="memory-trigger" key={word}>{word}</li>)}
            </ul>
          ) : <span className="secondary">未设置</span>}
        </div>
        {!!triggers.length && <p className="memory-trigger-hint">{reflection
          ? "待人工准入后参与自动召回。"
          : triggerActive
          ? "用户消息包含这些词时，可自动召回这条记忆。"
          : "触发词已保留，仅在全局召回记忆中参与自动触发。"}</p>}
      </>}
    </div>
  );
}
