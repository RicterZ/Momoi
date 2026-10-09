// Provider-specific defaults must not carry an old supplier's endpoint forward.
export function switchProviderAdapter(capability, value, adapter, adapters) {
  if (adapter === value.adapter) return value;
  const fieldsFor = (name) => adapters.find(
    (item) => item.capability === capability && item.adapter === name,
  )?.fields || {};
  const previous = fieldsFor(value.adapter);
  const next = fieldsFor(adapter);
  const options = {};
  for (const [key, spec] of Object.entries(next)) {
    const supplied = value.options?.[key];
    const endpointChanged = capability !== "llm" && ["base_url", "endpoint"].includes(key)
      && previous[key]?.default !== spec.default;
    if (endpointChanged || supplied === undefined || (spec.enum && !spec.enum.includes(supplied))) {
      if (Object.hasOwn(spec, "default")) options[key] = structuredClone(spec.default);
    } else {
      options[key] = structuredClone(supplied);
    }
  }
  return { ...value, adapter, options };
}
