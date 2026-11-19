try {
  if (localStorage.getItem("momoi-dashboard-appearance") === "dark") document.documentElement.dataset.theme = "dark";
} catch {}
