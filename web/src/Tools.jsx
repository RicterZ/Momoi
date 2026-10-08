import { useEffect, useRef, useState } from "react";
import { Toggle, SelectField, Icon, SaveBar } from "./ConfigurationSettings.jsx";
import { waitForConfiguration } from "./configurationRuntime.js";
import Loading from "./Loading.jsx";
import "./tools.css";

const blankServer = () => ({ description: "", command: "", args: [], env: {} });
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

function Field({ label, hint, children }) {
  return <label className="settings-field"><span className="settings-label">{label}</span>{children}{hint && <small>{hint}</small>}</label>;
}

function KeyValues({ label, value = {}, onChange }) {
  const entries = Object.entries(value);
  return <div className="tool-key-values"><div className="tool-detail-heading"><h4>{label}</h4><button className="quiet-button" type="button" onClick={() => onChange({ ...value, [`KEY_${entries.length + 1}`]: "" })}>＋ 添加</button></div>
    {!entries.length && <p className="tool-note">尚未配置，按需添加。</p>}
    {entries.map(([key, content], index) => <div className="tool-key-row" key={index}>
      <input className="dash-input" aria-label={`${label}名称 ${index + 1}`} value={key} onChange={event => onChange(Object.fromEntries(entries.map(([k, v], i) => [i === index ? event.target.value : k, v])))} />
      <input className="dash-input" aria-label={`${label}值 ${index + 1}`} value={String(content)} onChange={event => onChange({ ...value, [key]: event.target.value })} />
      <button className="quiet-button" type="button" aria-label={`删除${label} ${key}`} onClick={() => onChange(Object.fromEntries(entries.filter(([k]) => k !== key)))}>×</button>
    </div>)}
  </div>;
}

export default function Tools({ token, request, confirm, refreshKey }) {
  const [tab, setTab] = useState("mcp");
  const [data, setData] = useState(null);
  const [document, setDocument] = useState({ mcpServers: {} });
  const [runtime, setRuntime] = useState([]);
  const [skills, setSkills] = useState([]);
  const [selected, setSelected] = useState(null);
  const [execSelected, setExecSelected] = useState(true);
  const [editing, setEditing] = useState(null);
  const [saved, setSaved] = useState(null);
  const [skill, setSkill] = useState(null);
  const [install, setInstall] = useState(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [query, setQuery] = useState("");
  const lock = useRef(false);
  const workspace = useRef(null);
  const dirty = editing !== null && !same(editing, saved);
  const call = (path, options = {}) => request(path, { token, ...options });
  async function refresh(signal) {
    const [configuration, mcp, status, listing] = await Promise.all([
      call("/api/settings/configuration", { signal }), call("/api/settings/mcp", { signal }),
      call("/api/tools/mcp", { signal }), call("/api/tools/skills", { signal }),
    ]);
    setData(configuration); setDocument(mcp); setRuntime(status.servers); setSkills(listing.skills);
  }
  useEffect(() => {
    const controller = new AbortController();
    refresh(controller.signal).catch(problem => { if (!controller.signal.aborted) setError(problem.message); });
    return () => controller.abort();
  }, [token, refreshKey]);
  useEffect(() => {
    if (!data || !workspace.current) return;
    const element = workspace.current;
    const fit = () => {
      const top = element.getBoundingClientRect().top + window.scrollY;
      element.style.setProperty("--tool-height", `${Math.max(160, window.innerHeight - top - 24)}px`);
    };
    const observer = new ResizeObserver(fit);
    observer.observe(element.parentElement);
    window.addEventListener("resize", fit);
    fit();
    return () => { observer.disconnect(); window.removeEventListener("resize", fit); };
  }, [data, error, notice]);
  async function mutate(action) {
    if (lock.current) return;
    lock.current = true; setBusy(true); setError(""); setNotice("");
    try { await action(); } catch (problem) { setError(problem.message); }
    finally { lock.current = false; setBusy(false); }
  }
  async function leave() {
    return !dirty || await confirm({ title: "还有修改没有保存", message: "切换会丢弃当前服务的修改。", confirmLabel: "丢弃修改" });
  }
  async function openServer(name, value) {
    if (busy || !await leave()) return;
    setExecSelected(false); setSelected(name); const draft = { name, config: structuredClone(value) };
    setEditing(draft); setSaved(structuredClone(draft)); setSkill(null); setInstall(null); setError("");
  }
  async function saveDocument(next) {
    const result = await call("/api/settings/mcp", { method: "PATCH", body: next, headers: { "If-Match": `"${data.revision}"` } });
    setData(result); setDocument(next);
    const outcome = await waitForConfiguration(call, result.revision);
    if (["error", "timeout"].includes(outcome.state)) throw new Error(outcome.message || outcome.title);
    await refresh();
  }
  async function saveServer(event) {
    event.preventDefault();
    await mutate(async () => {
      const name = editing.name.trim();
      if (!name) throw new Error("请填写服务名称。");
      if (!editing.config.description?.trim()) throw new Error("请描述工具是什么、能做什么。");
      if (name !== selected && Object.hasOwn(document.mcpServers, name)) throw new Error("同名服务已存在。");
      const servers = { ...document.mcpServers };
      if (selected !== null) delete servers[selected];
      servers[name] = editing.config;
      await saveDocument({ ...document, mcpServers: servers });
      const draft = { name, config: editing.config }; setSelected(name); setEditing(draft); setSaved(structuredClone(draft));
      setNotice("服务配置已保存。");
    });
  }
  const change = patch => setEditing(current => ({ ...current, config: { ...current.config, ...patch } }));
  async function openSkill(name) {
    if (!await leave()) return;
    await mutate(async () => { setExecSelected(false); setEditing(null); setSaved(null); setInstall(null); setSelected(name); setSkill(await call(`/api/tools/skills/${encodeURIComponent(name)}`)); });
  }
  if (!data) return error ? <p role="alert">{error}</p> : <Loading />;
  const servers = Object.entries(document.mcpServers || {});
  const connected = runtime.filter(server => server.connected).length;
  const entries = tab === "mcp" ? servers.map(([name, config]) => ({ name, description: config.description, config })) : skills;
  const filtered = entries.filter(item => `${item.name} ${item.description}`.toLowerCase().includes(query.toLowerCase()));
  const showExec = tab === "mcp" && "命令执行 安装依赖 执行系统命令".includes(query.trim().toLowerCase());
  const config = editing?.config;
  const status = runtime.find(server => server.name === selected);
  return <div className="settings-studio toolbox" data-dirty={dirty}>

    <div className="tool-navigation"><div className="dash-tabs" role="tablist" aria-label="工具分类">{[["mcp", "MCP 工具"], ["skills", "Skill"]].map(([id, label]) => <button key={id} role="tab" aria-selected={tab === id} className={tab === id ? "active" : ""} disabled={busy} onClick={async () => { if (await leave()) { setTab(id); setExecSelected(id === "mcp"); setQuery(""); setEditing(null); setSaved(null); setSelected(null); setSkill(null); setInstall(null); setError(""); } }}><span>{label}</span></button>)}</div><div className="tool-counts"><span><b>{servers.length}</b> MCP 服务</span><span><b>{skills.length}</b> Skill</span></div></div>
    {error && <p className="tool-feedback is-error" role="alert">{error}</p>}{notice && <p className="tool-feedback" role="status">{notice}</p>}

    <div ref={workspace} className="record-layout tool-workspace"><section className="tool-library" aria-label={tab === "mcp" ? "MCP 服务列表" : "Skill 列表"}>
      <div className="tool-library-toolbar"><div className="tool-library-head"><p>{tab === "mcp" ? `${connected} 个已连接 · ${servers.length} 个服务` : `${skills.length} 份工作流`}</p>{tab === "mcp" && <button className="quiet-button tool-reload" disabled={busy} onClick={() => mutate(async () => { const result = await call("/api/tools/mcp/reload", { method: "POST", body: {} }); await refresh(); if (!result.ok) throw new Error(result.message || "部分服务连接失败，请检查服务状态。"); setNotice("MCP 连接已重载。"); })}>↻ 重载</button>}</div>

      <div className="tool-library-search"><input className="dash-input tool-filter" aria-label="筛选工具" placeholder={tab === "mcp" ? "搜索服务名称或能力…" : "搜索 Skill 名称或描述…"} value={query} onChange={event => setQuery(event.target.value)} /></div>
      </div><div className="tool-library-body"><div className="tool-list">{showExec && <button className={`tool-list-item ${execSelected ? "active" : ""}`} disabled={busy} onClick={async () => { if (!await leave()) return; setExecSelected(true); setSelected(null); setEditing(null); setSaved(null); setSkill(null); setInstall(null); setError(""); }}><div className="tool-item-heading"><span className="tool-item-icon"><Icon name="spark" /></span><strong>命令执行</strong><small className={`tool-item-status ${data.app.tools?.exec_enabled ? "connected" : ""}`}>{data.app.tools?.exec_enabled ? "已开启" : "已关闭"}</small></div><p>安装依赖与执行系统命令。</p></button>}{filtered.map(item => { const live = runtime.find(server => server.name === item.name); return <button key={item.name} className={`tool-list-item ${selected === item.name ? "active" : ""}`} disabled={busy} onClick={() => tab === "mcp" ? openServer(item.name, item.config) : openSkill(item.name)}><div className="tool-item-heading"><span className={`tool-item-icon ${tab === "skills" ? "blue" : ""}`}><Icon name={tab === "mcp" ? "spark" : "memory"} /></span><strong>{item.name}</strong><small className={`tool-item-status ${tab === "mcp" && live?.connected && !item.config.disabled ? "connected" : ""}`} title={live?.error}>{tab === "mcp" ? item.config.disabled ? "已停用" : live?.connected ? `${live.tools.length} 个工具` : live?.error ? "连接失败" : "未连接" : `${item.resources.length} 个资源`}</small></div><p title={item.description}>{item.description || "尚未填写能力描述"}</p></button>; })}</div>
      {!filtered.length && !showExec && <div className="tool-empty"><Icon name="spark" /><h3>{query ? "没有找到匹配项" : tab === "mcp" ? "连接第一项外部能力" : "添加一份工作流"}</h3><p>{tab === "mcp" ? "添加本地服务或远程 MCP 接口。" : "从标准 Skill 目录或 Git 仓库安装。"}</p></div>}

      </div><footer className="tool-library-footer"><button className="quiet-button pink" disabled={busy} onClick={async () => { if (tab === "mcp") await openServer(null, blankServer()); else if (await leave()) { setEditing(null); setSkill(null); setSelected(null); setInstall({ source: "", subdirectory: "", ref: "" }); } }}><Icon name="plus" />{tab === "mcp" ? "添加" : "安装"}</button></footer>
    </section>
    <section className="tool-detail" aria-label="工具详情">
      {execSelected && tab === "mcp" ? <><header className="tool-detail-header"><div><span className="panel-label">BUILTIN // COMMAND</span><h2>命令执行</h2></div><Toggle checked={Boolean(data.app.tools?.exec_enabled)} disabled={busy} onChange={async value => { if (!await leave()) return; await mutate(async () => { setEditing(null); setSaved(null); const result = await call("/api/settings/configuration/app", { method: "PATCH", body: { revision: data.revision, document: { tools: { exec_enabled: value } } } }); setData(result); const outcome = await waitForConfiguration(call, result.revision); if (["error", "timeout"].includes(outcome.state)) throw new Error(outcome.message || outcome.title); await refresh(); }); }}>启用命令执行</Toggle></header><div className="tool-fields"><p className="tool-skill-description">允许 Momoi 安装依赖与执行系统命令，可用于安装 MCP 工具。</p><p className="tool-note">默认关闭。开启后拥有服务进程权限，无沙箱隔离。</p></div></> : editing ? <form className="tool-mcp-form" onSubmit={saveServer}><header className="tool-detail-header"><div><span className="panel-label">MCP //</span><h2>{selected === null ? "添加 MCP 服务" : selected}</h2></div><div className="tool-header-actions"><Toggle checked={!config.disabled} disabled={busy} onChange={value => change({ disabled: !value })}>启用</Toggle></div></header><fieldset disabled={busy} className="tool-fields">
        <div className="tool-field-grid"><Field label="服务名称"><input className="dash-input" value={editing.name || ""} required onChange={event => setEditing({ ...editing, name: event.target.value })} placeholder="例如 brave-search" /></Field><SelectField label="连接方式" value={config.command !== undefined ? "local" : "remote"} options={[{ value: "local", label: "本地进程 · STDIO" }, { value: "remote", label: "远程接口 · HTTP" }]} onChange={value => { const next = { ...config }; delete next.command; delete next.url; delete next.baseUrl; if (value === "local") next.command = ""; else next.url = ""; setEditing({ ...editing, config: next }); }} /></div>
        <Field label="能力描述" hint="说明提供什么工具、能做什么。Momoi 会通过这段描述发现能力。"><textarea className="dash-input" rows={3} required maxLength={500} value={config.description || ""} onChange={event => change({ description: event.target.value })} placeholder="例如：搜索公开网页与本地商家，获取实时信息。" /></Field>
        {config.command !== undefined ? <><Field label="启动命令"><input className="dash-input" required value={config.command} onChange={event => change({ command: event.target.value })} placeholder="uvx、node 或可执行文件的绝对路径" /></Field><Field label="启动参数" hint="每行一个参数，保留参数内的空格。"><textarea className="dash-input tool-mono" rows={3} value={(config.args || []).join("\n")} onChange={event => change({ args: event.target.value ? event.target.value.split("\n") : [] })} placeholder="-y&#10;@modelcontextprotocol/server-brave-search" /></Field><Field label="工作目录"><input className="dash-input" value={config.cwd || ""} onChange={event => { const next = { ...config }; if (event.target.value) next.cwd = event.target.value; else delete next.cwd; setEditing({ ...editing, config: next }); }} placeholder="可选，建议使用服务安装目录" /></Field><KeyValues label="环境变量" value={config.env} onChange={env => change({ env })} /></> : <><Field label="服务地址"><input className="dash-input" type="url" required value={config.url || config.baseUrl || ""} onChange={event => change({ url: event.target.value })} placeholder="https://example.com/mcp" /></Field><KeyValues label="请求 Header" value={config.headers} onChange={headers => change({ headers })} /></>}
        <details className="tool-advanced"><summary>高级工具设置</summary><Field label="启用的工具" hint="每行一个工具名。* 表示全部；留空则不注册任何工具。"><textarea className="dash-input" rows={2} value={(config.enabled_tools || config.enabledTools || ["*"]).join("\n")} onChange={event => change({ enabled_tools: event.target.value ? event.target.value.split("\n") : [] })} /></Field><Field label="只读工具" hint="仅填写确认没有写入或外部副作用的工具名，每行一个。"><textarea className="dash-input" rows={2} value={(config.readOnlyTools || []).join("\n")} onChange={event => change({ readOnlyTools: event.target.value ? event.target.value.split("\n") : [] })} /></Field></details>
        {status && <div className="tool-discovered"><div className="tool-detail-heading"><h4>可用工具</h4><span className="panel-label">{status.tools.length} TOOLS</span></div>{status.error && <p className="is-error">{status.error}</p>}{status.tools.map(tool => <div key={tool.name}><strong>{tool.name}</strong><p>{tool.description}</p></div>)}{!status.tools.length && <p className="tool-note">连接成功后，工具会显示在这里。</p>}</div>}
      </fieldset><footer className="tool-mcp-actions">{selected !== null && <button className="quiet-button pink" type="button" disabled={busy} onClick={async () => { if (!await confirm({ title: `删除 ${selected}？`, message: "移除服务配置并关闭连接，安装目录与依赖保留。", confirmLabel: "删除" })) return; await mutate(async () => { const next = { ...document.mcpServers }; delete next[selected]; await saveDocument({ ...document, mcpServers: next }); setEditing(null); setSaved(null); setSelected(null); }); }}>删除</button>}<SaveBar busy={busy} dirty={dirty} hint="保存后自动应用 MCP 配置。" /></footer></form>
      : install ? <form onSubmit={event => { event.preventDefault(); mutate(async () => { const result = await call("/api/tools/skills", { method: "POST", body: { source: install.source, subdirectory: install.subdirectory || ".", ...(install.ref ? { ref: install.ref } : {}) } }); await refresh(); setInstall(null); setSelected(result.name); setSkill(await call(`/api/tools/skills/${encodeURIComponent(result.name)}`)); setNotice("Skill 已安装。"); }); }}><header className="tool-detail-header"><div><span className="panel-label">SKILL //</span><h2>安装 Skill</h2></div></header><fieldset className="tool-fields" disabled={busy}><p className="tool-note">导入标准 SKILL.md 目录及其脚本、引用与资源，不会执行脚本。</p><Field label="安装来源" hint="本地目录或 HTTPS Git 仓库地址。"><input className="dash-input" required value={install.source} onChange={event => setInstall({ ...install, source: event.target.value })} placeholder="https://github.com/anthropics/skills.git" /></Field><Field label="仓库内目录"><input className="dash-input" value={install.subdirectory} onChange={event => setInstall({ ...install, subdirectory: event.target.value })} placeholder="例如 skills/pdf，可留空" /></Field><Field label="分支或标签"><input className="dash-input" value={install.ref} onChange={event => setInstall({ ...install, ref: event.target.value })} placeholder="默认分支" /></Field><button className="quiet-button pink" disabled={busy}>{busy ? "正在安装…" : "安装到工作区"}</button></fieldset></form>
      : skill ? <div className="tool-skill-view"><header className="tool-detail-header"><div><span className="panel-label">SKILL //</span><h2>{skill.name}</h2></div></header><div className="tool-fields tool-skill-body"><p className="tool-skill-description">{skill.description}</p><div className="tool-location"><span className="panel-label">WORKSPACE</span><code>{skill.directory}</code></div><div className="tool-detail-heading"><h4>工作流指引</h4><span className="panel-label">SKILL.md</span></div><pre className="tool-skill-content">{skill.content}</pre>{skill.resources.length > 0 && <div className="tool-resources"><h4>附带资源</h4>{skill.resources.map(resource => <code key={resource}>{resource}</code>)}</div>}</div><footer className="tool-skill-actions"><button className="quiet-button pink" disabled={busy} onClick={async () => { if (!await confirm({ title: `卸载 ${skill.name}？`, message: "删除这份 Skill 和附带资源。它安装的 MCP 服务、依赖与文件保留。", confirmLabel: "卸载" })) return; await mutate(async () => { await call(`/api/tools/skills/${encodeURIComponent(skill.name)}`, { method: "DELETE" }); await refresh(); setSkill(null); setSelected(null); }); }}>卸载</button></footer></div>
      : <div className="tool-detail-empty"><span className="tool-empty-mark"><Icon name={tab === "mcp" ? "spark" : "memory"} /></span>{tab === "mcp" && <span className="panel-label">CONNECT // CAPABILITIES</span>}<h2>{tab === "mcp" ? "让 Momoi 多一项本领" : "把经验变成工作流"}</h2><p>{tab === "mcp" ? "选择一个服务查看能力与连接配置，或添加新的 MCP 工具。" : "选择一份 Skill 查看指引。Momoi 会搜索内容，在需要时加载它。"}</p><button className="quiet-button pink" disabled={busy} onClick={() => tab === "mcp" ? openServer(null, blankServer()) : setInstall({ source: "", subdirectory: "", ref: "" })}>{tab === "mcp" ? "＋ 添加 MCP 服务" : "＋ 安装 Skill"}</button></div>}
    </section></div>
  </div>;
}
