export const memoryTagLabels = {
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

export function filterMemories(items, { activation = "all", scope = null, query = "" } = {}) {
  const normalize = (value) => value.normalize("NFKC").toLocaleLowerCase();
  const text = normalize(query.trim());
  return items.filter((item) => {
    if (activation !== "all" && item.activation !== activation) return false;
    if (scope !== null && memoryScope(item) !== scope) return false;
    const tags = item.meta?.tags || [];
    return !text || normalize([
      item.content, item.key, memoryScope(item), memoryScopeLabel(item),
      ...(item.meta?.triggers || []), ...tags, ...tags.map((tag) => memoryTagLabels[tag] || tag),
    ].filter(Boolean).join("\n")).includes(text);
  });
}
