const STORAGE_KEY = "momoi-dashboard-appearance";

export const themes = [
  { id: "light", label: "浅色" },
  { id: "dark", label: "深色" },
];

export function readTheme() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (themes.some(({ id }) => id === saved)) return saved;
  } catch {
    // The dashboard can still switch themes when storage is unavailable.
  }
  return themes[0].id;
}

export function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  const accent = getComputedStyle(document.documentElement).getPropertyValue("--pink").trim();
  if (accent) document.querySelector('meta[name="theme-color"]').content = accent;
}

export function saveTheme(theme) {
  applyTheme(theme);
  try {
    localStorage.setItem(STORAGE_KEY, theme);
  } catch {
    // Keep the selected theme for this page even if persistence fails.
  }
}
