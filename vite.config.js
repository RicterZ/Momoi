import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

function previewUsageApi() {
  const days = 30;
  const today = new Date();
  const daily = Array.from({ length: days }, (_, index) => {
    const date = new Date(today);
    date.setDate(today.getDate() - (days - 1 - index));
    const wave = 0.55 + Math.sin(index / 3.2) * 0.35 + (index % 7 === 5 ? 0.45 : 0);
    const requests = Math.round(6 + wave * 18);
    const input = Math.round(4200 + wave * 18000);
    const cacheRead = Math.round(input * (0.42 + (index % 5) * 0.08));
    const output = Math.round(380 + wave * 1400);
    const estimatedCost = Number((0.012 + wave * 0.084).toFixed(4));
    return {
      date: date.toISOString().slice(0, 10),
      requests,
      input_tokens: input,
      uncached_tokens: input - cacheRead,
      cache_read_tokens: cacheRead,
      cache_write_tokens: 0,
      output_tokens: output,
      cache_hit_rate: Number(((cacheRead / input) * 100).toFixed(1)),
      estimated_cost: estimatedCost,
    };
  });
  const sum = (key) => daily.reduce((total, row) => total + row[key], 0);
  const totals = {
    requests: sum("requests"),
    input_tokens: sum("input_tokens"),
    uncached_tokens: sum("uncached_tokens"),
    cache_read_tokens: sum("cache_read_tokens"),
    cache_write_tokens: 0,
    output_tokens: sum("output_tokens"),
    cache_hit_rate: Number(
      ((sum("cache_read_tokens") / sum("input_tokens")) * 100).toFixed(1)
    ),
    estimated_cost: Number(sum("estimated_cost").toFixed(4)),
  };
  const usage = {
    source: "preview",
    currency: "CNY",
    timezone: "Asia/Shanghai",
    days,
    note: "本地预览数据，用来看折线。接上真实 dashboard 后会换成接入后的新调用。",
    totals,
    today: daily[daily.length - 1],
    daily,
    models: [
      { model: "deepseek-v4-flash", ...totals },
    ],
    stages: [
      { stage: "owner", ...totals, requests: Math.round(totals.requests * 0.6) },
      { stage: "heartbeat", ...totals, requests: Math.round(totals.requests * 0.3) },
      { stage: "webhook", ...totals, requests: Math.round(totals.requests * 0.1) },
    ],
  };

  const json = (res, body, status = 200) => {
    res.statusCode = status;
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify(body));
  };

  return {
    name: "momoi-preview-api",
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        if (process.env.MOMOI_PREVIEW === "0") {
          next();
          return;
        }
        const path = (req.url || "").split("?")[0];
        if (req.method === "POST" && path === "/api/auth/token") {
          json(res, {
            token: "preview-token",
            token_type: "Bearer",
            expires_in: 3600,
          });
          return;
        }
        if (req.method === "GET" && path === "/api/health") {
          json(res, { ok: true });
          return;
        }
        if (req.method === "GET" && path === "/api/overview") {
          json(res, {
            counts: {
              conversations: 0,
              messages: 0,
              reflections: 0,
              goals: 0,
              reminders: 0,
              emotions: 0,
              memories: 0,
            },
            mood: { state: "happy", intensity: 0.65, cause: "preview" },
            activity: {
              name: "previewing dashboard",
              result: "本地预览用量折线",
              since: null,
              since_timestamp: null,
            },
            usage,
            balance: {
              source: "forge",
              currency: "CNY",
              is_available: true,
              total_balance: "86.40",
              granted_balance: "12.00",
              topped_up_balance: "74.40",
            },
          });
          return;
        }
        if (req.method === "GET" && path === "/api/usage") {
          json(res, usage);
          return;
        }
        if (req.method === "GET" && path.startsWith("/api/")) {
          json(res, { items: [] });
          return;
        }
        next();
      });
    },
  };
}

export default defineConfig({
  root: "web",
  plugins: [react(), previewUsageApi()],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8788",
    },
  },
  build: {
    outDir: "../src/momoi/dashboard",
    emptyOutDir: true,
  },
});
