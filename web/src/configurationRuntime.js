// A successful write is separate from the supervisor completing that revision.
export async function waitForConfiguration(call, revision, {
  signal,
  timeout = 120000,
  interval = 1000,
} = {}) {
  const deadline = Date.now() + timeout;
  let lastError = "";
  while (Date.now() < deadline) {
    signal?.throwIfAborted();
    try {
      const status = await call("/api/settings/runtime", {
        signal: AbortSignal.any([
          ...(signal ? [signal] : []),
          AbortSignal.timeout(Math.min(8000, Math.max(1, deadline - Date.now()))),
        ]),
      });
      lastError = "";
      if (status.saved_revision !== revision) {
        return { state: "error", title: "配置已被更新", message: "另一次修改覆盖了本次配置，请刷新页面后检查。" };
      }
      if (status.state === "error" && status.observed_revision === revision) {
        return { state: "error", title: "服务重启失败", message: status.error || "配置已保存，但服务未能启动。" };
      }
      if (status.applied_revision === revision) {
        if (status.state === "running" && status.runtime_active === true) {
          return { state: "success", title: "保存成功", message: "服务已重启。" };
        }
        if (status.state === "setup") {
          return { state: "success", title: "保存成功", message: "" };
        }
      }
    } catch (error) {
      signal?.throwIfAborted();
      lastError = error.message;
    }
    await new Promise(resolve => setTimeout(resolve, Math.min(interval, Math.max(0, deadline - Date.now()))));
  }
  return {
    state: "timeout",
    title: "尚未确认重启结果",
    message: `配置已保存，检查已超时。可以继续检查或返回设置。${lastError ? `状态读取失败：${lastError}` : ""}`,
  };
}
