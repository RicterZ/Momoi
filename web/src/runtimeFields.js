export function runtimeFieldValue(spec, value) {
  if (spec.properties) return Object.fromEntries(Object.entries(spec.properties).map(([key, child]) => [
    key, runtimeFieldValue(child, value?.[key]),
  ]));
  return value ?? spec.default;
}

export function runtimeFieldChanges(spec, value, saved) {
  if (spec.properties) {
    const changes = {};
    for (const [key, child] of Object.entries(spec.properties)) {
      const change = runtimeFieldChanges(child, value?.[key], saved?.[key]);
      if (change !== undefined) changes[key] = change;
    }
    return Object.keys(changes).length ? changes : undefined;
  }
  if (["number", "integer"].includes(spec.type)) {
    const number = typeof value === "string" && value.trim() ? Number(value) : value;
    if (typeof number !== "number" || !Number.isFinite(number) || (spec.type === "integer" && !Number.isInteger(number))) {
      throw new Error(`${spec.label || "数值"}请输入有效${spec.type === "integer" ? "整数" : "数字"}。`);
    }
    value = number;
  }
  return JSON.stringify(value) === JSON.stringify(saved) ? undefined : value;
}
