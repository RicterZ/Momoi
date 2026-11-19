export const schemes = [
  { id: "light", label: "浅色" },
  { id: "dark", label: "深色" },
];

export const styles = [
  { id: "pixel", label: "像素" },
  { id: "flat", label: "紧凑扁平" },
];

const preferences = {
  scheme: { key: "momoi-dashboard-appearance", options: schemes },
  style: { key: "momoi-dashboard-style", options: styles },
};

export function readAppearance(axis) {
  const { key, options } = preferences[axis];
  try {
    const saved = localStorage.getItem(key);
    if (options.some(({ id }) => id === saved)) return saved;
  } catch {
    // CSS and the application both have defaults.
  }
  return options[0].id;
}

export function applyAppearance(axis, value) {
  const root = document.documentElement;
  root.dataset[axis] = value;
  if (axis === "scheme") {
    const color = getComputedStyle(root).getPropertyValue("--browser-chrome").trim();
    if (color) document.querySelector('meta[name="theme-color"]').content = color;
  }
}

export function saveAppearance(axis, value) {
  applyAppearance(axis, value);
  try {
    localStorage.setItem(preferences[axis].key, value);
  } catch {
    // The selection still applies for the current page.
  }
}
