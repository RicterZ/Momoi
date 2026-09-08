export function connectionTestStatus(result) {
  const elapsed = Number.isFinite(result?.elapsed_ms) && result.elapsed_ms >= 0
    ? ` · ${result.elapsed_ms} ms` : "";
  if (result?.ok === true) return { text: `连接成功${elapsed}`, error: false };
  const message = result?.error?.message;
  const httpStatus = Number.isInteger(result?.error?.http_status)
    ? `（HTTP ${result.error.http_status}）` : "";
  return {
    text: `${typeof message === "string" ? message : "测试返回了无法识别的结果，请重试。"}${httpStatus}${elapsed}`,
    error: true,
  };
}

export async function testProviderConnection(request, capability, document, signal) {
  try {
    const result = await request(`/api/settings/providers/${encodeURIComponent(capability)}/test`, {
      method: "POST",
      body: document,
      signal: AbortSignal.any([signal, AbortSignal.timeout(45000)]),
    });
    return connectionTestStatus(result);
  } catch (error) {
    try {
      const result = JSON.parse(error.message);
      if (result?.error && typeof result.error === "object") return connectionTestStatus(result);
    } catch { /* Non-JSON errors retain the request's message. */ }
    return {
      text: error.name === "TimeoutError" ? "连接测试超时，请稍后重试。" : error.message || "无法完成连接测试，请稍后重试。",
      error: true,
    };
  }
}
