export const memoryTagLabels = {
  personal_background: "个人背景",
  mental_wellbeing: "心理状态", finance: "金融", shopping: "购物", home_living: "居家生活",
  food_drink: "饮食", health: "健康", work_study: "工作学习",
  technology: "技术", travel: "出行", leisure: "休闲",
  daily_life: "日常生活", social: "人际关系", communication: "交流互动",
};

export function memoryScope(item) {
  return item.meta?.scope ?? item.scope ?? "";
}

export function memoryScopeLabel(item) {
  return item.scope_label || memoryScope(item) || "全局";
}

export const UNTAGGED = "__untagged";

export function memoryTagLabel(tag) {
  return tag === UNTAGGED ? "未分类" : memoryTagLabels[tag] || tag;
}

export function memoryPrimaryTag(item) {
  return item.meta?.tags?.[0] || UNTAGGED;
}

export function memoryUsage(item) {
  const counts = item.access?.counts;
  return counts ? (counts.recalled || 0) + (counts.searched || 0) : 0;
}

function compareMemories(sort) {
  const updated = (left, right) => String(right.updated_at ?? "").localeCompare(String(left.updated_at ?? ""), undefined, { numeric: true });
  if (sort === "updated") return updated;
  return (left, right) => memoryUsage(right) - memoryUsage(left) || updated(left, right);
}

export function memoryTagCounts(items) {
  const counts = new Map();
  for (const item of items) {
    const tags = item.meta?.tags?.length ? item.meta.tags : [UNTAGGED];
    for (const tag of new Set(tags)) counts.set(tag, (counts.get(tag) || 0) + 1);
  }
  return [...counts].sort(([a, left], [b, right]) => (a === UNTAGGED) - (b === UNTAGGED) || right - left)
    .map(([tag, count]) => ({ tag, label: memoryTagLabel(tag), count }));
}

export function groupMemories(items, { by = "tag", sort = "usage" } = {}) {
  const groups = new Map();
  for (const item of items) {
    const key = by === "scope" ? memoryScope(item) : memoryPrimaryTag(item);
    if (!groups.has(key)) {
      groups.set(key, { key, label: by === "scope" ? memoryScopeLabel(item) : memoryTagLabel(key), items: [] });
    }
    groups.get(key).items.push(item);
  }
  const last = by === "scope" ? "" : UNTAGGED;
  return [...groups.values()]
    .map((group) => ({ ...group, items: group.items.sort(compareMemories(sort)) }))
    .sort((left, right) => (left.key === last) - (right.key === last)
      || right.items.length - left.items.length || left.label.localeCompare(right.label));
}

export function filterMemories(items, { activation = "all", scope = null, query = "", tags: wanted = [] } = {}) {
  const normalize = (value) => value.normalize("NFKC").toLocaleLowerCase();
  const text = normalize(query.trim());
  return items.filter((item) => {
    if (activation !== "all" && item.activation !== activation) return false;
    if (scope !== null && memoryScope(item) !== scope) return false;
    const tags = item.meta?.tags || [];
    if (wanted.length && !wanted.some((tag) => tag === UNTAGGED ? !tags.length : tags.includes(tag))) return false;
    return !text || normalize([
      item.content, item.key, memoryScope(item), memoryScopeLabel(item),
      ...(item.meta?.triggers || []), ...tags, ...tags.map((tag) => memoryTagLabels[tag] || tag),
    ].filter(Boolean).join("\n")).includes(text);
  });
}
