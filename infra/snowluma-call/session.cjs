"use strict";

// Observe QQ's own session initialization; never create or log in a second session.
function observeWrapper(wrapper, onSession, onError) {
  const observed = new WeakSet();
  const factory = wrapper.NodeIQQNTWrapperSession;
  if (!factory) throw new Error("QQ NT session factory is unavailable");
  let hooked = 0;
  for (const name of ["create", "getNTWrapperSession"]) {
    const original = factory[name];
    if (typeof original !== "function") continue;
    factory[name] = function (...args) {
      const session = Reflect.apply(original, this, args);
      if (session && !observed.has(session)) {
        const init = session.init;
        if (typeof init !== "function") throw new Error("QQ session.init is unavailable");
        session.init = function (config, ...rest) {
          const result = Reflect.apply(init, this, [config, ...rest]);
          // Native init is normally synchronous. Preserve its return value and this.
          Promise.resolve(result).then(() => onSession(session, config)).catch(onError);
          return result;
        };
        if (session.init === init) throw new Error("QQ session.init is not writable");
        observed.add(session);
      }
      return session;
    };
    hooked += 1;
    if (factory[name] === original) throw new Error("QQ session factory is not writable");
  }
  if (!hooked) throw new Error("QQ session factories are unavailable");
}

function sessionContext(session, config) {
  if (!config || typeof config.selfUid !== "string" || !config.selfUid ||
      typeof config.selfUin !== "string" || !/^[1-9][0-9]*$/.test(config.selfUin)) {
    throw new Error("QQ session identity is unavailable");
  }
  const service = session.getAVSDKService?.();
  if (!service || service.isNull?.() ||
      typeof service.addKernelAVSDKListener !== "function" ||
      typeof service.setActionFromAVSDK !== "function") {
    throw new Error("QQ AVSDK service is unavailable");
  }
  const accountPath = session.getAccountPath?.(Number(config.selfUin));
  if (typeof accountPath !== "string" || !accountPath) {
    throw new Error("QQ account path is unavailable");
  }
  return {
    core: { context: { session }, selfInfo: { uid: config.selfUid, uin: config.selfUin },
      dataPath: accountPath },
    logger: { info: console.info, warn: console.warn, error: console.error },
    router: { get() {} },
  };
}

module.exports = { observeWrapper, sessionContext };
