import { useRef, useState } from "react";

export default function BackupSettings({ token }) {
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
    <div className="settings-section-header"><span className="panel-label">SAVE DATA // BACKUP</span><h2>备份与恢复</h2><p>保留同一个 Momoi，以及你们积累的共同历史。</p></div>
    <div className="settings-form-body">
      <section className="settings-runtime-group"><div className="settings-runtime-copy"><h3>导出备份</h3><p className="settings-runtime-description">导出前暂停运行，生成并压缩一致的数据库快照。包括 SOUL、自定义提示词、聊天、记忆、任务、表情与渠道媒体素材；不含思考、工具调用结果、日志、浏览器数据或账户密钥。</p></div>
        <button className="quiet-button settings-button" disabled={Boolean(busy)} onClick={() => run("export")}>{busy === "export" ? "正在生成备份…" : "导出 ZIP"}</button>
      </section>
      <section className="settings-runtime-group"><div className="settings-runtime-copy"><h3>恢复备份</h3><p className="settings-runtime-description">使用相同版本导出的 Momoi ZIP。恢复会替换当前聊天、记忆和提示词；模型密钥与消息渠道设置保留。建议先导出当前备份。未发送的旧消息不会重发，正在执行的计划会暂停。</p></div>
        <div className="backup-controls"><label className="settings-field">选择备份 ZIP<input ref={input} type="file" accept=".zip,application/zip" disabled={Boolean(busy)} onChange={e => { setFile(e.target.files[0] || null); setConfirmed(false); }} /></label>
          <label className="backup-confirm"><input type="checkbox" checked={confirmed} disabled={Boolean(busy)} onChange={e => setConfirmed(e.target.checked)} />我确认用这份备份替换当前共同历史与提示词</label>
          <button className="quiet-button settings-button" disabled={Boolean(busy) || !file || !confirmed} onClick={() => run("restore")}>{busy === "restore" ? "正在验证并恢复…" : "恢复备份"}</button>
        </div>
      </section>
      <div role="status" aria-live="polite">{message && <p>{message}</p>}{error && <p className="form-error">{error}</p>}</div>
    </div>
  </section>;
}
