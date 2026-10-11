import appFields from "./runtime-fields.json" with { type: "json" };
import adapters from "./settings-adapters.json" with { type: "json" };

// Isolated, in-memory fixtures for Vite's existing MOMOI_PREVIEW mode.
// No credentials are sent to a provider and no workspace configuration is written.
export function createSettingsPreview(json) {
  let revision = 1;
  let applied = "preview-1";
  let login = { status: "idle" };
  let connectionTesting = false;
  let mcpDocument = { mcpServers: {
    "brave-search": { description: "搜索公开网页与本地商家，为 Momoi 提供实时信息与检索结果。", command: "npx", args: ["-y", "@modelcontextprotocol/server-brave-search"], env: { BRAVE_API_KEY: "${BRAVE_API_KEY}" } },
    "fetch": { description: "抓取网页正文并转换为 Markdown，支持按页读取长文内容。", command: "uvx", args: ["mcp-server-fetch"] },
    "context7": { description: "查询开发库的最新文档与代码示例，辅助编程和技术调研。", url: "https://mcp.context7.com/mcp", disabled: true },
  } };
  let skills = [
    { name: "mcp-install", description: "安装、配置或修复 MCP 工具服务。检查运行环境，安装依赖、合并配置并重载连接。", directory: "~/.momoi/skills/mcp-install", resources: [], content: "---\nname: mcp-install\ndescription: 安装、配置或修复 MCP 工具服务。\n---\n\n# 安装 MCP 工具\n\n## 1. 检查运行环境\n\nWindows 桌面版附带 uv、Node 和 Python；Linux Docker 镜像提供同样的运行环境。\n\n## 2. 安装到工作区\n\n把源码、独立环境和服务数据放在：\n\n    tools/mcp/<server-id>/\n\n## 3. 合并服务配置\n\n读取 mcp.json，仅修改目标服务。description 描述工具是什么、能干什么，供 Momoi 发现能力。\n\n## 4. 最后重载并验证\n\n查找并加载 mcp_reload，确认连接与工具发现成功，继续原任务。" },
    { name: "paper-reading", description: "阅读研究论文，梳理问题、方法与实验依据，整理有出处的阅读笔记。", directory: "~/.momoi/skills/paper-reading", resources: ["references/reading-guide.md"], content: "---\nname: paper-reading\ndescription: 阅读研究论文并整理笔记。\n---\n\n# 论文阅读\n\n先确认研究问题，再检查方法、实验与结论之间的证据。" },
    { name: "daily-review", description: "整理一天的进展与待办，回顾重要事项，生成简洁的日常复盘。", directory: "~/.momoi/skills/daily-review", resources: [], content: "---\nname: daily-review\ndescription: 整理一天的进展与待办。\n---\n\n# 日常复盘\n\n记录已完成的事情和下一步安排。" },
  ];
  const mcpStatus = () => Object.entries(mcpDocument.mcpServers).map(([name, config]) => ({ name, connected: !config.disabled, error: null, tools: config.disabled ? [] : (name === "brave-search" ? ["brave_web_search", "brave_local_search"] : ["fetch"]).map(tool => ({ name: `mcp__${name}__${tool}`, description: tool === "brave_web_search" ? "搜索公开网页，获取标题、摘要和原文链接。" : tool === "fetch" ? "提取网页正文，返回 Markdown 内容。" : "查询地点与商家信息。" })) }));
  let configuration = {
    desktop_embedding_managed: process.env.MOMOI_PREVIEW_DESKTOP === "1",
    revision: "preview-1",
    app: {
      channels: { primary: "weixin", enabled: { weixin: {} } },
      ...Object.fromEntries(Object.entries(appFields).map(([name, schema]) => [name, Object.fromEntries(Object.entries(schema.fields).map(([key, spec]) => [key, spec.default]))])),
      budget: { enabled: true, daily_amount: 0.2, monthly_amount: 60 },
    },
    app_fields: appFields,
    adapters,
    capabilities: {
      llm: {
        adapter: "openai",
        enabled: true,
        options: {
          base_url: "https://api.deepseek.com",
          api_key: "preview-model-key",
          model: "deepseek-v4-flash",
          temperature: 0.6,
        },
      },
      tts: {
        adapter: "fish",
        enabled: true,
        options: {
          api_key: { $secret: "keep" },
          reference_id: "momoi-voice",
          model: "s2.1-pro-free",
        },
      },
      embedding: { adapter: "local", enabled: true, options: {} },
      balance: {
        adapter: "deepseek",
        enabled: true,
        options: {
          api_key: { $secret: "keep" },
          base_url: "https://api.deepseek.com",
        },
      },
    },
    environment_overrides: [],
  };
  const apply = () => {
    const next = configuration.revision;
    setTimeout(() => {
      applied = next;
    }, 1400);
  };
  const redact = (capability, value) => {
    const options = { ...value.options };
    const fields =
      adapters.find(
        (adapter) =>
          adapter.capability === capability &&
          adapter.adapter === value.adapter,
      )?.fields || {};
    for (const [key, spec] of Object.entries(fields)) {
      if (
        spec.secret &&
        options[key] &&
        !(typeof options[key] === "object" && "env" in options[key])
      )
        options[key] = { $secret: "keep" };
    }
    return { ...value, options };
  };
  const handle = (req, res, path) => {
    if (path === "/api/tools/mcp" || path === "/api/tools/mcp/reload") { json(res, { ok: true, servers: mcpStatus() }); return true; }
    if (path.startsWith("/api/tools/skills")) {
      const name = decodeURIComponent(path.split("/")[4] || "");
      if (req.method === "GET") { json(res, name ? skills.find(item => item.name === name) : { skills }); return true; }
      if (req.method === "DELETE") { skills = skills.filter(item => item.name !== name); json(res, { ok: true }); return true; }
      if (req.method === "POST") { json(res, { ok: false, message: "预览模式不会安装外部 Skill。" }, 400); return true; }
    }
    if (req.method === "GET" && path === "/api/settings/mcp") {
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify(mcpDocument, null, 2) + "\n");
      return true;
    }
    if (req.method === "GET" && path === "/api/settings/configuration") {
      json(res, configuration);
      return true;
    }
    if (req.method === "GET" && path === "/api/settings/runtime") {
      json(res, {
        state: applied === configuration.revision ? "running" : "applying",
        missing: [],
        observed_revision: configuration.revision,
        runtime_active: applied === configuration.revision,
        saved_revision: configuration.revision,
        applied_revision: applied,
        weixin_login: login,
      });
      return true;
    }
    if (req.method === "POST" && path === "/api/settings/apply") {
      applied = null;
      apply();
      json(res, { accepted: true }, 202);
      return true;
    }
    if (
      path === "/api/settings/channels/weixin/login" &&
      ["POST", "DELETE"].includes(req.method)
    ) {
      login =
        req.method === "DELETE"
          ? { status: "cancelled" }
          : { status: "verification_required" };
      json(res, { accepted: true }, 202);
      return true;
    }
    const providers =
      path === "/api/settings/providers" && req.method === "PUT";
    const app =
      path === "/api/settings/configuration/app" && req.method === "PATCH";
    const verify =
      path === "/api/settings/channels/weixin/verify" && req.method === "POST";
    const test = req.method === "POST" && path.match(/^\/api\/settings\/providers\/([^/]+)\/test$/);
    const mcp = req.method === "PATCH" && path === "/api/settings/mcp";
    if (!providers && !app && !verify && !test && !mcp) return false;
    let raw = "";
    req.setEncoding("utf8");
    req.on("data", (chunk) => {
      raw += chunk;
    });
    req.on("end", () => {
      try {
        const body = JSON.parse(raw);
        if (mcp) {
          if (req.headers["if-match"] !== `"${configuration.revision}"`) {
            json(res, { error: "配置已变更，请刷新后重试。" }, 409);
            return;
          }
          if (!body?.mcpServers || typeof body.mcpServers !== "object" || Array.isArray(body.mcpServers)) {
            json(res, { error: "mcpServers 必须为对象。" }, 400);
            return;
          }
          mcpDocument = body;
          configuration = { ...configuration, revision: `preview-${++revision}` };
          apply();
          json(res, configuration, 202);
          return;
        }
        if (test) {
          const capability = test[1];
          const metadata = adapters.find(item => item.capability === capability && item.adapter === body.adapter);
          if (metadata?.test_supported !== true) {
            json(res, { ok: false, error: { code: "unsupported", message: "当前服务不支持连接测试。" } }, 400);
            return;
          }
          if (connectionTesting) {
            json(res, { ok: false, error: { code: "busy", message: "已有连接测试正在进行，请稍后重试。" } }, 429);
            return;
          }
          connectionTesting = true;
          setTimeout(() => {
            connectionTesting = false;
            json(res, { ok: true, capability, adapter: body.adapter, elapsed_ms: 320,
              details: { model: body.options?.model || "preview-model", ...(capability === "embedding" ? { dimensions: body.options?.dimensions || 512 } : {}) },
            });
          }, 320);
          return;
        }
        if (verify) {
          if (!body.code?.trim()) {
            json(res, { error: "请输入验证码" }, 400);
            return;
          }
          login = { status: "confirmed" };
          json(res, { accepted: true }, 202);
          return;
        }
        if (body.revision !== configuration.revision) {
          json(res, { error: "配置已变更，请重新加载后再试。" }, 409);
          return;
        }
        if (!body.document || typeof body.document !== "object") {
          json(res, { error: "配置文档不能为空" }, 400);
          return;
        }
        if (providers) {
          for (const [capability, value] of Object.entries(body.document)) {
            if (
              !adapters.some(
                (adapter) =>
                  adapter.capability === capability &&
                  adapter.adapter === value.adapter,
              )
            ) {
              json(res, { error: "请选择可用的服务协议" }, 400);
              return;
            }
            if (
              capability === "llm" &&
              (!value.enabled || !value.options?.model?.trim())
            ) {
              json(res, { error: "请填写语言模型名称" }, 400);
              return;
            }
          }
          configuration = {
            ...configuration,
            capabilities: {
              ...configuration.capabilities,
              ...Object.fromEntries(
                Object.entries(body.document).map(([name, value]) => [
                  name,
                  redact(name, value),
                ]),
              ),
            },
          };
        } else
          configuration = {
            ...configuration,
            app: { ...configuration.app, ...Object.fromEntries(Object.entries(body.document).map(([name, value]) => [name,
              name === "thinking" ? { ...configuration.app[name], ...value, stages: { ...configuration.app[name]?.stages, ...value.stages } }
                : appFields[name] ? { ...configuration.app[name], ...value } : value,
            ])) },
          };
        revision += 1;
        configuration.revision = `preview-${revision}`;
        apply();
        json(res, configuration);
      } catch {
        json(res, { error: "无效的 JSON 配置" }, 400);
      }
    });
    return true;
  };
  handle.budgetStatus = () => {
    const budget = configuration.app.budget;
    const daily = budget.daily_amount > 0;
    const blocked = budget.enabled && ((daily && 0.07 >= budget.daily_amount) || (budget.monthly_amount > 0 && 18.72 >= budget.monthly_amount));
    return { enabled: budget.enabled, period: daily ? "daily" : "monthly", amount: daily ? Number(budget.daily_amount) : Number(budget.monthly_amount), spent: daily ? 0.07 : 18.72, available: true, blocked, reason: blocked ? "达到费用预算，调度已暂停。" : "" };
  };
  return handle;
}
