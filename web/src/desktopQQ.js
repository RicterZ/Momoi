export function hasDesktopQQ(target = globalThis.window) {
  return !!target?.chrome?.webview?.postMessage;
}

export function desktopQQ(action, values = {}, target = globalThis.window) {
  const bridge = target?.chrome?.webview;
  if (!hasDesktopQQ(target)) return Promise.reject(new Error('内置 QQ 仅在 Windows 桌面版可用。'));
  const id = globalThis.crypto.randomUUID();
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => finish(new Error('QQ 操作超时，请查看配置目录中的 napcat/logs。')), 75000);
    function finish(error, result) {
      clearTimeout(timer);
      bridge.removeEventListener('message', receive);
      if (error) reject(error); else resolve(result);
    }
    function receive(event) {
      const message = event.data;
      if (message?.type !== 'momoi-qq' || message.id !== id) return;
      finish(message.error ? new Error(message.error) : null, message.result);
    }
    bridge.addEventListener('message', receive);
    try { bridge.postMessage({ ...values, type: 'momoi-qq', id, action }); }
    catch (error) { finish(error); }
  });
}
