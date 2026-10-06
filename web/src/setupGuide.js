// Configuration snapshots redact stored secrets with {$secret: "keep"}.
export function hasModelApiKey(data) {
  const key = data?.capabilities?.llm?.options?.api_key;
  return (typeof key === "string" && key.trim().length > 0)
    || Boolean(key && typeof key === "object" && (key.$secret === "keep" || key.env));
}

export function hasMessageChannel(data) {
  const channels = data?.app?.channels;
  const channel = channels?.enabled?.[channels.primary];
  if (!channel) return false;
  if (channels.primary === "napcat") return Boolean(String(channel.owner_qq || "").trim() && String(channel.url || "").trim());
  return true;
}
