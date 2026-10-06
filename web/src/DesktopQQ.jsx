import { useEffect, useState } from 'react';
import { desktopQQ, hasDesktopQQ } from './desktopQQ.js';

export default function DesktopQQ({ botQQ, ownerQQ, connected, disabled, onConnection }) {
  const [status, setStatus] = useState(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  const desktop = hasDesktopQQ();
  useEffect(() => {
    if (!desktop) return;
    let active = true;
    async function refresh() {
      try { const result = await desktopQQ('status'); if (active) setStatus(result); }
      catch (failure) { if (active) setError(failure.message); }
    }
    refresh();
    const timer = setInterval(refresh, 5000);
    return () => { active = false; clearInterval(timer); };
  }, [desktop]);
  if (!desktop) return null;
  async function operate(action) {
    if (pending || disabled) return;
    setPending(true); setError('');
    try {
      if (action === 'start') {
        if (!/^[0-9]{5,20}$/.test(botQQ || '')) throw new Error('请填写机器人 QQ 号码（5–20 位数字）。');
        if (!/^[0-9]+$/.test(ownerQQ || '')) throw new Error('请填写主人 QQ 号码。');
        const result = await desktopQQ('start', { bot_qq: botQQ });
        setStatus({ available: true, ...result });
        await onConnection(result);
        await desktopQQ('login');
      } else {
        setStatus(await desktopQQ(action));
      }
    } catch (failure) { setError(failure.message); }
    finally { setPending(false); }
  }
  return <div className="settings-channel-login">
    <p className="settings-channel-note">内置 QQ：{pending ? '正在处理…' : status?.running ? '客户端运行中' : status?.available === false ? '组件缺失，请安装新版桌面版' : '未启动'}。启动后在独立窗口扫码。QQ 消息连接：{connected ? "已连接" : "未连接"}。</p>
    <div className="settings-qq-actions">
    <button type="button" className="quiet-button settings-button" disabled={disabled || pending || status?.available === false} onClick={() => operate('start')}>启动并连接</button>
    <button type="button" className="quiet-button settings-button" disabled={disabled || pending || !status?.ready} onClick={() => operate('login')}>QQ 登录面板</button>
    <button type="button" className="quiet-button settings-button" disabled={disabled || pending || !status?.running} onClick={() => operate('stop')}>停止客户端</button>
    </div>
    {error && <p role="alert" className="settings-channel-note">{error}</p>}
  </div>;
}
