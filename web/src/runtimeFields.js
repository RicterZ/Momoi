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
  return JSON.stringify(value) === JSON.stringify(saved) ? undefined : value;
}
