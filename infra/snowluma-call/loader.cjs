"use strict";

const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { observeWrapper, captureExport, sessionContext } = require("./session.cjs");

function start() {
  console.error("[MomoiQQCall] loader", process.type, process.env.QQ_CALL_ENABLED || "");
  const { app } = require("electron");
  if (process.type !== "browser" || process.env.QQ_CALL_ENABLED !== "1") return;
  if (process.env.MAIBOT_QQ_CALL_AV_HOST === "1") {
    require("/opt/qq-call/upstream/bridge/av-host/host.cjs");
    return;
  }
  let plugin, timer, generation = 0, work = Promise.resolve();
  const report = (error) => console.error("[MomoiQQCall] session bridge unavailable",
    error && (error.stack || error.message || String(error)) || "check the pinned QQ version");
  const attach = (session, config) => {
    const current = ++generation;
    clearTimeout(timer);
    work = work.then(async () => {
      if (plugin) await plugin.plugin_cleanup();
      plugin = await import(pathToFileURL("/opt/qq-call/upstream/bridge/napcat-plugin/index.mjs"));
      let attempts = 0;
      const ready = async () => {
        if (current !== generation) return;
        try {
          const ctx = sessionContext(session, config);
          await plugin.plugin_init(ctx);
        } catch (error) {
          await plugin.plugin_cleanup();
          if (attempts === 0) report(error);
          if (++attempts < 60) timer = setTimeout(() => {
            work = work.then(ready).catch(report);
          }, 1000);
          else report(error);
        }
      };
      await ready();
    }).catch(report);
  };
  const dlopen = process.dlopen;
  process.dlopen = function (module, filename, ...args) {
    const wrapper = path.basename(String(filename)) === "wrapper.node";
    if (wrapper) captureExport(module, attach, report);
    const result = Reflect.apply(dlopen, this, [module, filename, ...args]);
    if (wrapper) {
      try {
        const factory = module.exports && module.exports.NodeIQQNTWrapperSession;
        if (factory && !factory.__momoiQQCall) observeWrapper(module.exports, attach, report);
      } catch (error) { report(error); }
    }
    return result;
  };
  app.on("before-quit", () => {
    generation += 1;
    clearTimeout(timer);
    void work.then(() => plugin?.plugin_cleanup()).catch(report);
  });
}

module.exports = { start };
