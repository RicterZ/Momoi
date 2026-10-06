import test from "node:test";
import assert from "node:assert/strict";
import { hasModelApiKey, hasMessageChannel } from "../src/setupGuide.js";

const model = api_key => ({ capabilities: { llm: { options: { api_key } } } });
test("setup is triggered by an absent or blank model key, not redacted credentials", () => {
  for (const key of [undefined, null, "", "  ", {}]) assert.equal(hasModelApiKey(model(key)), false);
  for (const key of ["user-key", { $secret: "keep" }, { env: "MODEL_API_KEY" }]) assert.equal(hasModelApiKey(model(key)), true);
  assert.equal(hasModelApiKey({ capabilities: { tts: { options: { api_key: "voice-only" } } } }), false);
});
test("the primary channel must exist and QQ needs an owner and transport", () => {
  const channel = (primary, enabled) => ({ app: { channels: { primary, enabled } } });
  assert.equal(hasMessageChannel(channel("", {})), false);
  assert.equal(hasMessageChannel(channel("napcat", { napcat: { url: "ws://localhost:3001" } })), false);
  assert.equal(hasMessageChannel(channel("napcat", { napcat: { url: "ws://localhost:3001", owner_qq: "123456" } })), true);
  assert.equal(hasMessageChannel(channel("weixin", { weixin: {} })), true);
});
