import { useRef, useState } from "react";

export default function BackupSettings({ token, header }) {
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [file, setFile] = useState(null);
  const [confirmed, setConfirmed] = useState(false);
  const input = useRef(null);
  async function run(kind) {
    if (busy) return;
    setBusy(kind); setError(""); setMessage("");
    try {
      const response = await fetch(`/api/settings/backup/${kind}`, { method: "POST", headers: { Authorization: `Bearer ${token}`, ...(kind === "restore" ? { "Content-Type": "application/zip" } : {}) }, body: kind === "restore" ? file : undefined });
      if (!response.ok) throw new Error(await response.text());
      if (kind === "export") {
        const url = URL.createObjectURL(await response.blob());
        const link = document.createElement("a"); link.href = url; link.download = `momoi-backup-${new Date().toISOString().slice(0,10)}.zip`; link.click();
        setTimeout(() => URL.revokeObjectURL(url), 60000);
        setMessage("备份已导出，Momoi 正在恢复运行。");
      } else {
        setMessage((await response.json()).message); setFile(null); setConfirmed(false); input.current.value = "";
      }
    } catch (err) { setError(err.message); }
    finally { setBusy(""); }
  }
  return <section className="backup-settings">
    {header}
    <div className="settings-form-body settings-runtime-controls">
      <section className="settings-runtime-group"><div className="settings-runtime-copy"><h3>导出备份</h3><p className="settings-runtime-description">保存提示词、聊天、记忆与素材，导出时短暂暂停。不含思考、调用结果、日志和密钥。</p></div>
        <button className="quiet-button settings-button" disabled={Boolean(busy)} onClick={() => run("export")}>{busy === "export" ? "导出中…" : "导出"}</button>
      </section>
      <section className="settings-runtime-group backup-restore-group"><div className="settings-runtime-copy"><h3>恢复备份</h3><p className="settings-runtime-description">使用同版本 ZIP 替换当前数据，密钥与渠道设置保留。建议先导出备份。</p></div>
        <div className="backup-controls"><div className="backup-actions"><label className={`file-picker${file ? " has-file" : ""}`} title={file?.name}><input className="file-picker-input" aria-label="选择备份 ZIP" ref={input} type="file" accept=".zip,application/zip" disabled={Boolean(busy)} onChange={e => { setFile(e.target.files[0] || null); setConfirmed(false); }} /><span className="file-picker-face"><span className="file-picker-action">上传</span></span></label>
          <button className="quiet-button settings-button" disabled={Boolean(busy) || !file || !confirmed} onClick={() => run("restore")}>{busy === "restore" ? "恢复中…" : "恢复"}</button>
          </div>
          {file && <span className="backup-file-name" title={file.name}>{file.name}</span>}
          <label className="backup-confirm"><input type="checkbox" checked={confirmed} disabled={Boolean(busy) || !file} onChange={e => setConfirmed(e.target.checked)} />确认替换当前数据</label>
        </div>
      </section>
      <div role="status" aria-live="polite">{message && <p>{message}</p>}{error && <p className="form-error">{error}</p>}</div>
    </div>
  </section>;
}
