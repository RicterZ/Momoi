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
      <section className="settings-runtime-group"><div className="settings-runtime-copy"><h3>导出备份</h3><p className="settings-runtime-description">导出提示词、聊天、记忆和素材。期间暂停运行，完成后自动恢复。不含思考、调用结果、日志和密钥。</p></div>
        <button className="quiet-button settings-button" disabled={Boolean(busy)} onClick={() => run("export")}>{busy === "export" ? "正在生成备份…" : "导出 ZIP"}</button>
      </section>
      <section className="settings-runtime-group backup-restore-group"><div className="settings-runtime-copy"><h3>恢复备份</h3><p className="settings-runtime-description">选择相同版本的备份，替换当前聊天、记忆和提示词。模型密钥与渠道设置保留。建议先备份；旧消息不重发，执行中的计划会暂停。</p></div>
        <div className="backup-controls"><label className={`file-picker${file ? " has-file" : ""}`} title={file?.name}><input className="file-picker-input" aria-label="选择备份 ZIP" ref={input} type="file" accept=".zip,application/zip" disabled={Boolean(busy)} onChange={e => { setFile(e.target.files[0] || null); setConfirmed(false); }} /><span className="file-picker-face"><span className="file-picker-action">{file ? file.name : "选择备份 ZIP"}</span></span></label>
          <label className="backup-confirm"><input type="checkbox" checked={confirmed} disabled={Boolean(busy)} onChange={e => setConfirmed(e.target.checked)} />确认替换当前聊天、记忆和提示词</label>
          <button className="quiet-button settings-button" disabled={Boolean(busy) || !file || !confirmed} onClick={() => run("restore")}>{busy === "restore" ? "正在验证并恢复…" : "恢复备份"}</button>
        </div>
      </section>
      <div role="status" aria-live="polite">{message && <p>{message}</p>}{error && <p className="form-error">{error}</p>}</div>
    </div>
  </section>;
}
