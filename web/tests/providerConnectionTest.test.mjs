import assert from "node:assert/strict";
import test from "node:test";
import { connectionTestStatus, testProviderConnection } from "../src/providerConnectionTest.js";

test("sends the current configuration directly without a revision or document wrapper", async () => {
  const document = { adapter: "openai", options: { model: "unsaved", temperature: 0.4, api_key: { env: "MODEL_KEY" } } };
  const result = await testProviderConnection(async (path, options) => {
    assert.equal(path, "/api/settings/providers/llm/test");
    assert.equal(options.method, "POST");
    assert.deepEqual(options.body, document);
    assert.equal(options.signal.aborted, false);
    return { ok: true, elapsed_ms: 126 };
  }, "llm", document, new AbortController().signal);
  assert.deepEqual(result, { text: "连接成功 · 126 ms", error: false });
});

test("HTTP 200 with ok false reports the upstream failure and elapsed time", () => {
  const result = connectionTestStatus({ ok: false, elapsed_ms: 85, error: { message: "认证失败，请检查密钥及权限。", http_status: 401 } });
  assert.equal(result.error, true);
  assert.match(result.text, /认证失败/);
  assert.match(result.text, /HTTP 401/);
  assert.match(result.text, /85 ms/);
});

test("decodes structured HTTP validation and busy errors", async () => {
  for (const code of ["validation", "busy", "unsupported"]) {
    const result = await testProviderConnection(async () => {
      throw new Error(JSON.stringify({ ok: false, error: { code, message: `reason: ${code}` } }));
    }, "embedding", {}, new AbortController().signal);
    assert.deepEqual(result, { text: `reason: ${code}`, error: true });
  }
});

test("does not treat an unexpected response as success", () => {
  for (const response of [null, {}, { ok: "true" }]) assert.equal(connectionTestStatus(response).error, true);
});

test("reports transport failures and timeouts", async () => {
  const timeout = await testProviderConnection(async () => { throw new DOMException("timed out", "TimeoutError"); }, "llm", {}, new AbortController().signal);
  assert.match(timeout.text, /超时/);
  const failure = await testProviderConnection(async () => { throw new Error("Failed to fetch"); }, "llm", {}, new AbortController().signal);
  assert.deepEqual(failure, { text: "Failed to fetch", error: true });
});

test("passes cancellation to the request", async () => {
  const controller = new AbortController();
  await testProviderConnection(async (_path, options) => {
    controller.abort();
    assert.equal(options.signal.aborted, true);
    options.signal.throwIfAborted();
  }, "llm", {}, controller.signal);
});
