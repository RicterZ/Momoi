"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { observeWrapper, captureExport, sessionContext } = require("../infra/snowluma-call/session.cjs");

const config = { selfUid: "u_test", selfUin: "123456" };
function nativeSession() {
  return {
    init(value) { this.config = value; return 42; },
    getAVSDKService() { return { addKernelAVSDKListener() {}, setActionFromAVSDK() {} }; },
    getAccountPath() { return "/account"; },
  };
}

test("captures QQ-created session after native init, preserving this and return value", async () => {
  const session = nativeSession();
  const factory = {
    create() { assert.equal(this, factory); return session; },
    getNTWrapperSession(name) { assert.equal(this, factory); assert.equal(name, "nt_1"); return session; },
  };
  const wrapper = { NodeIQQNTWrapperSession: factory };
  const captured = [];
  observeWrapper(wrapper, (value, identity) => captured.push([value, identity]), assert.fail);
  assert.notEqual(wrapper.NodeIQQNTWrapperSession, factory);
  assert.equal(wrapper.NodeIQQNTWrapperSession.create(), session);
  wrapper.NodeIQQNTWrapperSession.getNTWrapperSession("nt_1");
  assert.equal(session.init(config), 42);
  await Promise.resolve();
  assert.equal(session.config, config);
  assert.deepEqual(captured, [[session, config]]);
  const ctx = sessionContext(session, config);
  assert.equal(ctx.core.context.session, session);
  assert.deepEqual(ctx.core.selfInfo, { uid: config.selfUid, uin: config.selfUin });
});

test("read-only native factory and session.init are observed without assignment", async () => {
  const session = nativeSession();
  const init = session.init;
  Object.defineProperty(session, "init", { value: init, writable: false, configurable: false });
  function NodeIQQNTWrapperSession() {}
  Object.defineProperty(NodeIQQNTWrapperSession, "getNTWrapperSession", {
    value(id) {
      assert.equal(this, NodeIQQNTWrapperSession);
      assert.equal(id, "nt_1");
      return session;
    },
    writable: false,
    configurable: false,
  });
  const wrapper = { NodeIQQNTWrapperSession };
  const captured = [];
  observeWrapper(wrapper, (value, identity) => captured.push([value, identity]), assert.fail);
  const returned = wrapper.NodeIQQNTWrapperSession.getNTWrapperSession("nt_1");
  assert.notEqual(returned, session);
  assert.equal(returned.init(config), 42);
  assert.equal(session.config, config);
  assert.equal(returned.getAccountPath(), "/account");
  await Promise.resolve();
  assert.deepEqual(captured, [[session, config]]);
  assert.equal(sessionContext(captured[0][0], config).core.context.session, session);
  const frozen = {};
  Object.defineProperty(frozen, "NodeIQQNTWrapperSession", {
    value: { create: () => session }, writable: false, configurable: false,
  });
  assert.throws(() => observeWrapper(frozen, () => {}, assert.fail), /not writable/);
});

test("native export assignment receives the shim, not the read-only factory", async () => {
  function NodeIQQNTWrapperSession() {}
  const session = nativeSession();
  Object.defineProperty(NodeIQQNTWrapperSession, "getNTWrapperSession", {
    value(id) { assert.equal(this, NodeIQQNTWrapperSession); assert.equal(id, "nt_1"); return session; },
    writable: false, configurable: false,
  });
  const module = { exports: {} };
  const captured = [];
  captureExport(module, (value, identity) => captured.push([value, identity]), assert.fail);
  module.exports.NodeIQQNTWrapperSession = NodeIQQNTWrapperSession;
  assert.equal(module.exports.NodeIQQNTWrapperSession.__momoiQQCall, true);
  module.exports.NodeIQQNTWrapperSession.getNTWrapperSession("nt_1");
  assert.equal(session.init(config), 42);
  await Promise.resolve();
  assert.deepEqual(captured, [[session, config]]);
});

test("invalid identity or missing native service fails closed", () => {
  for (const identity of [null, {}, { ...config, selfUin: "0" }, { ...config, selfUid: "" }]) {
    assert.throws(() => sessionContext(nativeSession(), identity), /identity/);
  }
  const session = nativeSession();
  session.getAVSDKService = () => null;
  assert.throws(() => sessionContext(session, config), /AVSDK/);
  session.getAVSDKService = () => ({ isNull: () => true });
  assert.throws(() => sessionContext(session, config), /AVSDK/);
  assert.throws(() => observeWrapper({}, () => {}, assert.fail), /factory/);
  assert.throws(() => observeWrapper({ NodeIQQNTWrapperSession: {} }, () => {}, assert.fail), /factories/);
});

test("native failures are preserved and rejected init never attaches", async () => {
  const session = nativeSession();
  const failure = new Error("native failure");
  session.init = () => { throw failure; };
  const wrapper = { NodeIQQNTWrapperSession: { create: () => session } };
  observeWrapper(wrapper, assert.fail, assert.fail);
  wrapper.NodeIQQNTWrapperSession.create();
  assert.throws(() => session.init(config), failure);
  const asyncSession = nativeSession();
  asyncSession.init = () => Promise.reject(failure);
  const errors = [];
  const asyncWrapper = { NodeIQQNTWrapperSession: { create: () => asyncSession } };
  observeWrapper(asyncWrapper, assert.fail, error => errors.push(error));
  asyncWrapper.NodeIQQNTWrapperSession.create();
  await assert.rejects(asyncSession.init(config), failure);
  await Promise.resolve();
  assert.deepEqual(errors, [failure]);
});

const fs = require("node:fs");
const vm = require("node:vm");
function loadPreload({ enabled = "1", avHost = false, type = "browser" } = {}) {
  const loaded = [], listeners = new Map();
  const original = function (module, filename) {
    assert.equal(this, proc);
    module.exports = { filename };
    return 19;
  };
  const proc = { type, env: { QQ_CALL_ENABLED: enabled,
    MAIBOT_QQ_CALL_AV_HOST: avHost ? "1" : "0" }, dlopen: original };
  const module = { exports: {} };
  vm.runInNewContext(fs.readFileSync(require.resolve("../infra/snowluma-call/loader.cjs"), "utf8"), {
    module, process: proc, console, clearTimeout, setTimeout,
    require(name) {
      if (name === "./session.cjs") return { observeWrapper, captureExport, sessionContext };
      if (name === "electron") return { app: { on: (name, fn) => listeners.set(name, fn) } };
      if (name.endsWith("/av-host/host.cjs")) { loaded.push(name); return {}; }
      return require(name);
    },
  });
  module.exports.start();
  return { proc, original, loaded, listeners };
}

test("preload leaves disabled and renderer processes alone", () => {
  for (const options of [{ enabled: "0" }, { type: "renderer" }]) {
    const { proc, original, loaded, listeners } = loadPreload(options);
    assert.equal(proc.dlopen, original);
    assert.equal(loaded.length, 0);
    assert.equal(listeners.size, 0);
  }
});

test("AV-only process loads host without observing another NT session", () => {
  const { proc, original, loaded } = loadPreload({ avHost: true });
  assert.equal(proc.dlopen, original);
  assert.deepEqual(loaded, ["/opt/qq-call/upstream/bridge/av-host/host.cjs"]);
});

test("QQ preload preserves unrelated native module loading", () => {
  const { proc, listeners } = loadPreload();
  const module = {};
  assert.equal(proc.dlopen(module, "/qq/other.node"), 19);
  assert.deepEqual(module.exports, { filename: "/qq/other.node" });
  assert.ok(listeners.has("before-quit"));
  listeners.get("before-quit")();
});
