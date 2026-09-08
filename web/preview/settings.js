import appFields from "./runtime-fields.json" with { type: "json" };
import adapters from "./settings-adapters.json" with { type: "json" };

// Isolated, in-memory fixtures for Vite's existing MOMOI_PREVIEW mode.
// No credentials are sent to a provider and no workspace configuration is written.
export function createSettingsPreview(json) {
  let revision = 1;
  let applied = "preview-1";
  let login = { status: "idle" };
  let configuration = {
    revision: "preview-1",
    app: {
      channels: { primary: "weixin", enabled: { weixin: {} } },
      ...Object.fromEntries(Object.entries(appFields).map(([name, schema]) => [name, Object.fromEntries(Object.entries(schema.fields).map(([key, spec]) => [key, spec.default]))])),
    },
    app_fields: appFields,
    adapters,
    capabilities: {
      llm: {
        adapter: "openai",
        enabled: true,
        options: {
          base_url: "https://api.deepseek.com",
          api_key: { $secret: "keep" },
          model: "deepseek-v4-flash",
          temperature: 0.6,
        },
      },
      asr: { adapter: "tencent", enabled: false, options: {} },
      tts: {
        adapter: "fish",
        enabled: true,
        options: {
          api_key: { $secret: "keep" },
          reference_id: "momoi-voice",
          model: "s2.1-pro-free",
        },
      },
      embedding: { adapter: "openai", enabled: false, options: {} },
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
  return (req, res, path) => {
    if (req.method === "GET" && path === "/api/settings/mcp") {
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ mcpServers: {} }, null, 2) + "\n");
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
    if (!providers && !app && !verify) return false;
    let raw = "";
    req.setEncoding("utf8");
    req.on("data", (chunk) => {
      raw += chunk;
    });
    req.on("end", () => {
      try {
        const body = JSON.parse(raw);
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
            app: { ...configuration.app, ...Object.fromEntries(Object.entries(body.document).map(([name, value]) => [name, appFields[name] ? { ...configuration.app[name], ...value } : value])) },
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
}
