"use strict";

const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { nativeSessionFacade, sessionContext } = require("./session.cjs");

function start() {
  const { app } = require("electron");
  if (process.type !== "browser" || process.env.QQ_CALL_ENABLED !== "1") return;
  if (process.env.MAIBOT_QQ_CALL_AV_HOST === "1") {
    require("/opt/qq-call/upstream/bridge/av-host/host.cjs");
    return;
  }
  let plugin, timer, pollTimer, generation = 0, work = Promise.resolve();
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
    const result = Reflect.apply(dlopen, this, [module, filename, ...args]);
    if (path.basename(String(filename)) === "wrapper.node") {
      try {
        const native = require("./session_hook.node");
        if (!native.install()) throw new Error("native NT session hook did not install");
        let announced = false;
        pollTimer = setInterval(() => {
          try {
            if (!native.ready()) return;
            const identity = native.identity();
            if (!identity?.uid || !identity.uin) {
              if (!announced) {
                announced = true;
                console.error("[MomoiQQCall] native session waiting", JSON.stringify(native.metrics()));
              }
              return;
            }
            clearInterval(pollTimer);
            console.error("[MomoiQQCall] native session", JSON.stringify(native.metrics()));
            attach(nativeSessionFacade(native), { selfUid: identity.uid, selfUin: identity.uin });
          } catch (error) { report(error); }
        }, 1000);
        pollTimer.unref?.();
      } catch (error) { report(error); }
    }
    return result;
  };
  app.on("before-quit", () => {
    generation += 1;
    clearInterval(pollTimer);
    clearTimeout(timer);
    void work.then(() => plugin?.plugin_cleanup()).catch(report);
  });
}

module.exports = { start };
