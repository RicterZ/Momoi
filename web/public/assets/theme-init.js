// Runs before first paint under the production Content Security Policy.
try {
  const root = document.documentElement;
  for (const [attribute, key] of [
    ["scheme", "momoi-dashboard-appearance"],
    ["style", "momoi-dashboard-style"],
  ]) {
    const value = localStorage.getItem(key);
    if (value && /^[a-z-]+$/.test(value)) root.dataset[attribute] = value;
  }
} catch {
  // CSS supplies the defaults when local storage is unavailable.
}
