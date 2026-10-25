"use strict";

const FACTORY_METHODS = ["create", "getNTWrapperSession"];

// Observe QQ's own session initialization; never create or log in a second session.
// QQ 3.2 exports NodeIQQNTWrapperSession.getNTWrapperSession as a non-writable,
// non-configurable native function. A Proxy cannot replace that property, so the
// export itself is swapped for a shim before QQ reads it.
function observeWrapper(wrapper, onSession, onError) {
  const factory = wrapper.NodeIQQNTWrapperSession;
  if (typeof factory !== "function" && (typeof factory !== "object" || factory === null)) {
    throw new Error("QQ NT session factory is unavailable");
  }
  const methods = FACTORY_METHODS.filter((name) => typeof factory[name] === "function");
  if (!methods.length) throw new Error("QQ session factories are unavailable");
  hookPrototype(factory, onSession, onError);
  const views = new WeakMap();

  function hookSession(session) {
    if (!session || (typeof session !== "object" && typeof session !== "function")) return session;
    if (views.has(session)) return views.get(session);
    const init = session.init;
    if (typeof init !== "function") throw new Error("QQ session.init is unavailable");
    const notify = (config, result) => {
      Promise.resolve(result).then(() => onSession(session, config)).catch(onError);
    };
    const hooked = function (config, ...rest) {
      const result = Reflect.apply(init, this === hooked ? session : this, [config, ...rest]);
      notify(config, result);
      return result;
    };
    if (assignInit(session, hooked)) {
      views.set(session, session);
      return session;
    }
    const view = Object.create(Object.getPrototypeOf(session));
    Object.defineProperty(view, "init", {
      value(config, ...rest) {
        const result = Reflect.apply(init, session, [config, ...rest]);
        notify(config, result);
        return result;
      },
      writable: true,
      configurable: true,
    });
    const forwarded = new Proxy(view, {
      get(target, prop, receiver) {
        if (prop === "init" || Object.prototype.hasOwnProperty.call(target, prop)) {
          return Reflect.get(target, prop, receiver);
        }
        const value = Reflect.get(session, prop, session);
        return typeof value === "function" ? value.bind(session) : value;
      },
      set(target, prop, value, receiver) {
        if (prop === "init" || Object.prototype.hasOwnProperty.call(target, prop)) {
          return Reflect.set(target, prop, value, receiver);
        }
        Reflect.set(session, prop, value, session);
        return true;
      },
    });
    views.set(session, forwarded);
    return forwarded;
  }

  function call(name) {
    const method = factory[name];
    return function (...args) {
      const session = Reflect.apply(method, factory, args);
      try {
        return hookSession(session);
      } catch (error) {
        onError(error);
        return session;
      }
    };
  }

  const shim = shimFactory(factory, methods, call);
  Object.defineProperty(shim, "__momoiQQCall", { value: true });
  installFactory(wrapper, shim);
}

const hookedPrototypes = new WeakSet();

function hookPrototype(factory, onSession, onError) {
  try {
    if (typeof factory !== "function" || hookedPrototypes.has(factory) || !factory.prototype) return;
    hookedPrototypes.add(factory);
    const desc = Object.getOwnPropertyDescriptor(factory, "prototype");
    const initDesc = Object.getOwnPropertyDescriptor(factory.prototype, "init");
    if (!desc || (desc.writable === false && desc.configurable === false) || !initDesc || initDesc.writable === false) return;
  } catch (error) {
    console.error("[MomoiQQCall] prototype failed", error && error.message);
  }
}

function captureExport(module, onSession, onError) {
  const target = module.exports && typeof module.exports === "object" ? module.exports : {};
  module.exports = new Proxy(target, {
    set(obj, prop, value, receiver) {
      if (prop === "NodeIQQNTWrapperSession") hookPrototype(value, onSession, onError);
      return Reflect.set(obj, prop, value, receiver);
    },
    defineProperty(obj, prop, desc) {
      if (prop === "NodeIQQNTWrapperSession" && desc && typeof desc.value === "function") {
        hookPrototype(desc.value, onSession, onError);
      }
      return Reflect.defineProperty(obj, prop, desc);
    },
  });
}

function assignInit(session, hooked) {
  try {
    session.init = hooked;
    if (session.init === hooked) return true;
  } catch {
    // Native session methods are read-only on current QQ builds.
  }
  const desc = Object.getOwnPropertyDescriptor(session, "init");
  if (!desc?.configurable) return false;
  Object.defineProperty(session, "init", {
    value: hooked, writable: true, configurable: true, enumerable: desc.enumerable === true,
  });
  return session.init === hooked;
}

function shimFactory(factory, methods, call) {
  const shim = typeof factory === "function"
    ? function (...args) { return Reflect.construct(factory, args, new.target); }
    : {};
  if (typeof factory === "function") {
    Object.setPrototypeOf(shim, Object.getPrototypeOf(factory));
    shim.prototype = factory.prototype;
  }
  const names = new Set([...Object.getOwnPropertyNames(factory), ...methods]);
  for (const name of names) {
    if (name === "prototype" || name === "length" || name === "name" ||
        name === "arguments" || name === "caller") continue;
    if (methods.includes(name)) {
      Object.defineProperty(shim, name, {
        value: call(name), writable: true, configurable: true, enumerable: true,
      });
      continue;
    }
    const desc = Object.getOwnPropertyDescriptor(factory, name);
    if (!desc || desc.get || desc.set) continue;
    Object.defineProperty(shim, name, {
      value: typeof desc.value === "function" ? desc.value.bind(factory) : desc.value,
      writable: true, configurable: true, enumerable: desc.enumerable === true,
    });
  }
  return shim;
}

function installFactory(wrapper, shim) {
  const desc = Object.getOwnPropertyDescriptor(wrapper, "NodeIQQNTWrapperSession");
  try {
    wrapper.NodeIQQNTWrapperSession = shim;
    if (wrapper.NodeIQQNTWrapperSession === shim) return;
  } catch {
    // Retry through defineProperty when the export is configurable.
  }
  if (desc?.configurable) {
    Object.defineProperty(wrapper, "NodeIQQNTWrapperSession", {
      value: shim, writable: true, configurable: true, enumerable: desc.enumerable !== false,
    });
    return;
  }
  throw new Error("QQ session factory is not writable");
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

function nativeSessionFacade(bridge) {
  const service = {
    isNull() { return false; },
    addKernelAVSDKListener(listener) {
      if (listener && typeof bridge.watch === "function") bridge.watch(listener);
      const id = bridge.registerListener();
      if (!Number.isInteger(id)) throw new Error("QQ AVSDK listener was not registered");
      return id;
    },
    removeKernelAVSDKListener(id) {
      if (typeof bridge.removeListener === "function") bridge.removeListener(id);
    },
    setActionFromAVSDK(command, payload) {
      return bridge.setAction(command, typeof payload === "string" ? payload : String(payload ?? ""));
    },
  };
  return {
    getAVSDKService() { return service; },
    getAccountPath() {
      const current = bridge.identity();
      if (!current || typeof current.path !== "string" || !current.path) {
        throw new Error("QQ account path is unavailable");
      }
      return current.path;
    },
  };
}

module.exports = { observeWrapper, captureExport, hookPrototype, sessionContext, nativeSessionFacade };
