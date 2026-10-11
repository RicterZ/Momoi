import { useRef, useState } from "react";

export default function BackupSettings({ token, header, Dialog }) {
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [file, setFile] = useState(null);
  const [confirmed, setConfirmed] = useState(false);
  const input = useRef(null);
  const exportButton = useRef(null);
  const [exported, setExported] = useState(false);
  const [pendingFile, setPendingFile] = useState(null);
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
        setExported(true);
      } else {
        setMessage((await response.json()).message); setFile(null); setConfirmed(false); input.current.value = "";
      }
    } catch (err) { setError(err.message); }
    finally { setBusy(""); }
  }
  return <section className="backup-settings">
    {exported && <Dialog title="备份" onClose={() => setExported(false)} returnFocusRef={exportButton}>
      <p className="confirm-copy">备份已成功导出。</p>
      <div className="confirm-actions"><button className="quiet-button" onClick={() => setExported(false)}>确定</button></div>
    </Dialog>}
    {pendingFile && <Dialog title="确认备份" onClose={() => { setPendingFile(null); input.current.value = ""; }} returnFocusRef={input}>
      <p className="confirm-copy">恢复将替换当前聊天、记忆和提示词。密钥与渠道设置保留。</p>
      <p className="backup-file-name">{pendingFile.name}</p>
      <div className="confirm-actions">
        <button className="quiet-button" onClick={() => { setPendingFile(null); input.current.value = ""; }}>取消</button>
        <button className="quiet-button pink" onClick={() => { setFile(pendingFile); setConfirmed(true); setPendingFile(null); }}>确认</button>
      </div>
    </Dialog>}
    {header}
    <div className="settings-form-body settings-runtime-controls">
      <section className="settings-runtime-group"><div className="settings-runtime-copy"><div className="settings-voice-title"><h3>导出备份</h3><span className="panel-label">BACKUP // EXPORT</span></div><p className="settings-runtime-description">保存提示词、聊天、记忆与素材，导出时短暂暂停。不含思考、调用结果、日志和密钥。</p></div>
        <button ref={exportButton} className="quiet-button settings-button" disabled={Boolean(busy)} onClick={() => run("export")}>{busy === "export" ? "导出中…" : "导出"}</button>
      </section>
      <section className="settings-runtime-group backup-restore-group"><div className="settings-runtime-copy"><div className="settings-voice-title"><h3>恢复备份</h3><span className="panel-label">BACKUP // RESTORE</span></div><p className="settings-runtime-description">恢复 ZIP 备份，旧数据自动升级。密钥与渠道设置保留，建议先备份。</p></div>
        <div className="backup-controls"><div className="backup-actions"><label className={`file-picker${file ? " has-file" : ""}`} title={file?.name}><input className="file-picker-input" aria-label="选择备份 ZIP" ref={input} type="file" accept=".zip,application/zip" disabled={Boolean(busy)} onChange={e => { const selected = e.target.files[0]; if (selected) { setPendingFile(selected); setFile(null); setConfirmed(false); } }} /><span className="file-picker-face"><span className="file-picker-action">上传</span></span></label>
          <button className="quiet-button settings-button" disabled={Boolean(busy) || !file || !confirmed} onClick={() => run("restore")}>{busy === "restore" ? "恢复中…" : "恢复"}</button>
          </div>
          {file && <span className="backup-file-name" title={file.name}>{file.name}</span>}
        </div>
      </section>
      <div role="status" aria-live="polite">{message && <p>{message}</p>}{error && <p className="form-error">{error}</p>}</div>
    </div>
  </section>;
}
