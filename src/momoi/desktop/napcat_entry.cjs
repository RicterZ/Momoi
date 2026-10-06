// Keep the unmodified official bundle read-only; all account data belongs to Momoi.
const path = require('node:path');
const fs = require('node:fs');
const runtime = process.env.MOMOI_NAPCAT_RUNTIME;
const data = process.env.MOMOI_QQ_DATA;
if (!runtime || !data) throw new Error('Missing managed NapCat directories');
fs.mkdirSync(data, { recursive: true });
process.env.NAPCAT_WRAPPER_PATH = path.join(runtime, 'wrapper.node');
process.env.NAPCAT_QQ_PACKAGE_INFO_PATH = path.join(runtime, 'package.json');
process.env.NAPCAT_QQ_VERSION_CONFIG_PATH = path.join(runtime, 'config.json');
process.env.NAPCAT_DISABLE_PIPE = '1';
// NapCat asks this native utility for both the global and per-account data root.
// Apply the override to each dlopen export (upstream does not use require cache).
const dlopen = process.dlopen;
process.dlopen = function (module, filename, ...args) {
  const result = dlopen.call(this, module, filename, ...args);
  if (path.resolve(filename) === path.resolve(process.env.NAPCAT_WRAPPER_PATH)) {
    const util = module.exports.NodeQQNTWrapperUtil;
    if (!util || typeof util.getNTUserDataInfoConfig !== 'function') {
      throw new Error('Unsupported NapCat native data-directory API');
    }
    // Native exports are non-configurable. Shadow on facades instead of mutating them.
    const managedUtil = Object.create(util);
    Object.defineProperty(managedUtil, 'getNTUserDataInfoConfig', { value: () => data });
    const managedExports = Object.create(module.exports);
    Object.defineProperty(managedExports, 'NodeQQNTWrapperUtil', { value: managedUtil });
    module.exports = managedExports;
    if (module.exports.NodeQQNTWrapperUtil.getNTUserDataInfoConfig() !== data) throw new Error('Cannot isolate QQ data');
    fs.writeFileSync(path.join(data, '.native-data-ready'), data, { mode: 0o600 });
  }
  return result;
};
