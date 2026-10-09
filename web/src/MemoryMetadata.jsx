import { memoryScope, memoryScopeLabel, memoryTagLabels } from "./memoryInventory.js";

export default function MemoryMetadata({ item }) {
  if (item.activation === "reflection") return null;
  const triggers = item.meta?.triggers || [];
  const tags = item.meta?.tags || [];
  const scope = memoryScope(item);
  const triggerActive = item.activation === "recall" && !scope;
  return (
    <div className="memory-metadata">
      <p className="memory-scope">
        作用域：{memoryScopeLabel(item)}
        {scope && item.scope_label && item.scope_label !== scope && <code>{scope}</code>}
      </p>
      {!!tags.length && (
        <div className="memory-chips" aria-label="主题标签">
          {tags.map((tag) => <span className="memory-tag" key={tag} title={tag}>{memoryTagLabels[tag] || tag}</span>)}
        </div>
      )}
      <div className="memory-triggers">
        <span>触发词：</span>
        {triggers.length ? (
          <ul className="memory-chips" aria-label="记忆触发词">
            {triggers.map((word) => <li className="memory-trigger" key={word}>{word}</li>)}
          </ul>
        ) : <span className="secondary">未设置</span>}
      </div>
      {!!triggers.length && <p className="memory-trigger-hint">{triggerActive
        ? "用户消息包含这些词时，可自动召回这条记忆。"
        : "触发词已保留，仅在全局召回记忆中参与自动触发。"}</p>}
    </div>
  );
}
