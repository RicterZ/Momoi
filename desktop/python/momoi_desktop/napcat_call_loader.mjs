// Transform only the pinned NapCat module in memory; keep imports and data paths intact.
let settings;
export function initialize(data) { settings = data; }
export async function load(url, context, nextLoad) {
  const result = await nextLoad(url, context);
  if (url !== settings.target) return result;
  const source = typeof result.source === 'string' ? result.source : Buffer.from(result.source).toString('utf8');
  const marker = 'a && (V.core.dbPassphrase = a), await V.InitNapCat();';
  if (source.split(marker).length !== 2) throw new Error('Unsupported pinned NapCat voice bootstrap; reinstall Momoi.');
  const injection = `${marker}\nif (!globalThis[Symbol.for("momoi.qq-call.initialized")]) { globalThis[Symbol.for("momoi.qq-call.initialized")] = true; await (await import(${JSON.stringify(settings.plugin)})).plugin_init({core:V.core,logger:{info:(...args)=>e.log(...args),warn:(...args)=>e.logWarn(...args),error:(...args)=>e.logError(...args)},router:{get(){}}}); }`;
  return { ...result, source: source.replace(marker, injection) };
}
