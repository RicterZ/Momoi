// Research only: no QQ login, network control, or call acceptance.
const { app, BrowserWindow, ipcMain } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const report = { pid: process.pid, executable: process.execPath, versions: process.versions, plugin: null, error: null };
function finish(error) {
  if (error) report.error = String(error);
  fs.writeFileSync(process.env.MOMOI_PROBE_REPORT, JSON.stringify(report, null, 2));
  app.exit(0);
}
process.on('uncaughtException', error => finish(error.stack));
app.setPath('userData', process.env.MOMOI_PROBE_PROFILE);
app.commandLine.appendSwitch('register-pepper-plugins', `${process.env.MOMOI_PROBE_AVSDK};application/x-ppapi-avSDK`);
app.commandLine.appendSwitch('disable-gpu');
ipcMain.on('probe-result', (_event, result) => { report.plugin = result; finish(); });
app.whenReady().then(async () => {
  fs.writeFileSync(process.env.MOMOI_PROBE_REPORT + '.started', JSON.stringify(report, null, 2));
  const win = new BrowserWindow({ show: false, webPreferences: { nodeIntegration: true, contextIsolation: false, plugins: true, sandbox: false } });
  app.on('child-process-gone', (_event, detail) => {
    if (detail.type === 'Pepper Plugin') finish(`plugin ${detail.reason}`);
  });
  win.webContents.on('render-process-gone', (_event, detail) => finish(`renderer ${detail.reason}`));
  await win.loadFile(path.join(__dirname, 'host.html'));
  setTimeout(() => finish('renderer probe timed out'), 30000);
}).catch(error => finish(error.stack));
