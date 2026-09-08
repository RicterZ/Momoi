import {
  Children,
  isValidElement,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";
import Loading from "./Loading.jsx";
import { waitForConfiguration } from "./configurationRuntime.js";
import { testProviderConnection } from "./providerConnectionTest.js";
import { runtimeFieldValue, runtimeFieldChanges } from "./runtimeFields.js";
import "./settings.css";

const modules = [
  {
    id: "model",
    label: "语言模型",
    icon: "spark",
    names: ["llm"],
    tip: "兼容 OpenAI 协议的服务可选择 OpenAI。服务地址和模型名称请以服务商提供的信息为准。",
  },
  { id: "prompts", label: "提示词" },
  {
    id: "channel",
    label: "消息渠道",
    icon: "chat",
    tip: "主渠道负责收发消息，不能停用。微信需要完成扫码登录；QQ 需要连接已运行的 NapCat。",
  },
  {
    id: "voice",
    label: "语音合成",
    icon: "voice",
    names: ["tts"],
    optional: true,
    tip: "让 Momoi 可以发送语音消息（需要启用 Napcat QQ 消息渠道，微信渠道暂不支持）。",
  },
  {
    id: "memory",
    label: "语义记忆",
    icon: "memory",
    names: ["embedding"],
    optional: true,
    tip: "这里仅控制向量编码与语义检索。关闭后，记忆写入和关键词召回仍然可用。",
  },
  {
    id: "balance",
    label: "账户余额",
    icon: "coin",
    names: ["balance"],
    optional: true,
    tip: "关闭后停止余额查询与费用估算，本地请求量与通用 Token 用量统计仍然保留。",
  },
  { id: "runtime", label: "运行设置" },
  { id: "mcp", label: "MCP 工具" },
];
const adapterLabels = {
  openai: "OpenAI",
  deepseek: "DeepSeek",
  anthropic: "Anthropic",
  fish: "Fish Audio",
};
const primaryFields = {
  llm: ["base_url", "api_key", "model", "thinking"],
  tts: ["api_key", "reference_id", "model"],
  embedding: ["endpoint", "api_key", "model", "dimensions"],
  balance: ["base_url", "api_key", "timeout_seconds", "accounting"],
};
// Hide transport tuning from the form without removing saved option values.
const hiddenFields = {
  embedding: ["base_url", "document_batch_size", "timeout_seconds"],
};
const fieldHints = {
  dimensions: "需与所选模型的输出维度一致",
};
const equal = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const keepSecret = (value) =>
  value && typeof value === "object" && value.$secret === "keep";

function Icon({ name, ...props }) {
  const paths = {
    spark: (
      <>
        <path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5Z" />
        <path d="M20 2v4m-2-2h4" />
      </>
    ),
    chat: (
      <>
        <path d="M5 4h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H9l-6 4V6a2 2 0 0 1 2-2Z" />
        <path d="M7 9h10M7 13h6" />
      </>
    ),
    weixin: (
      <g strokeWidth="1.7" strokeLinejoin="round">
        <path d="M16.8 9.5C16.8 5.9 13.5 3 9.4 3S2 5.9 2 9.5c0 2 1 3.7 2.7 4.9L4 17l3.2-1.5 2.2.4" />
        <path fill="var(--surface)" d="M22 14.4c0-3-2.8-5.4-6.2-5.4s-6.2 2.4-6.2 5.4 2.8 5.4 6.2 5.4l1.9-.3 2.8 1.4-.6-2.4a5.2 5.2 0 0 0 2.1-4.1Z" />
        <g fill="currentColor" stroke="none">
          <circle cx="6.5" cy="8" r="1" />
          <circle cx="11.8" cy="8" r="1" />
          <circle cx="13.3" cy="13.7" r=".9" />
          <circle cx="18.2" cy="13.7" r=".9" />
        </g>
      </g>
    ),
    qq: (
      <path
        strokeWidth="1.8"
        d="M12 2C7.5 2 5.6 5.5 5.6 10.5c-1 2.2-2.2 5.5-2.3 7.6-.1.8.2.9.7.4l1.6-1.9c.3 2.1 1.1 3.5 2.3 4.4-1.5.4-2.4.9-2.3 1.2.2.5 4.5.5 6.4.3 1.9.2 6.2.2 6.4-.3.1-.3-.8-.8-2.3-1.2 1.2-.9 2-2.3 2.3-4.4l1.6 1.9c.5.5.8.4.7-.4-.1-2.1-1.3-5.4-2.3-7.6C18.4 5.5 16.5 2 12 2Z"
      />
    ),
    voice: (
      <>
        <rect x="9" y="3" width="6" height="12" rx="3" />
        <path d="M5 11v1a7 7 0 0 0 14 0v-1M12 19v3m-4 0h8" />
      </>
    ),
    memory: (
      <>
        <path d="M4 3h13l3 3v15H4Z" />
        <path d="M8 3v7h8V3M8 21v-6h8v6" />
      </>
    ),
    coin: (
      <>
        <circle cx="12" cy="12" r="9" />
        <path d="M15 8h-4a2 2 0 0 0 0 4h2a2 2 0 0 1 0 4H9m3-10v2m0 8v2" />
      </>
    ),
    check: <path d="m5 12 4 4L19 6" />,
    chevron: <path d="m8 5 7 7-7 7" />,
    refresh: (
      <>
        <path d="M20 8a8 8 0 1 0 0 8M20 3v5h-5" />
      </>
    ),
    lock: (
      <>
        <rect x="5" y="10" width="14" height="11" rx="2" />
        <path d="M8 10V7a4 4 0 0 1 8 0v3m-4 5v2" />
      </>
    ),
    eye: (
      <>
        <path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z" />
        <circle cx="12" cy="12" r="3" />
      </>
    ),
    info: (
      <>
        <circle cx="12" cy="12" r="9" />
        <path d="M12 11v6m0-10v1" />
      </>
    ),
    plus: <path d="M12 5v14M5 12h14" />,
  };
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...props}
    >
      {paths[name] || paths.spark}
    </svg>
  );
}

// Keep native form semantics, but render a keyboard-operable menu instead of the OS select popup.
function SelectField({ label, value, onChange, options, hint, labelAction }) {
  const id = useId();
  const root = useRef(null);
  const trigger = useRef(null);
  const [open, setOpen] = useState(false);
  const [cursor, setCursor] = useState(0);
  const selected = options.find((option) => option.value === value);
  useEffect(() => {
    if (!open) return;
    const outside = (event) => {
      if (!root.current?.contains(event.target)) setOpen(false);
    };
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  }, [open]);
  useEffect(() => {
    if (open)
      document
        .getElementById(`${id}-${cursor}`)
        ?.scrollIntoView({ block: "nearest" });
  }, [cursor, open, id]);
  function choose(index) {
    if (options[index]) onChange(options[index].value);
    setOpen(false);
    trigger.current?.focus();
  }
  function keyboard(event) {
    if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      if (!open) {
        setOpen(true);
        setCursor(
          Math.max(
            0,
            options.findIndex((option) => option.value === value),
          ),
        );
        return;
      }
      setCursor((old) =>
        event.key === "Home"
          ? 0
          : event.key === "End"
            ? options.length - 1
            : (old + (event.key === "ArrowDown" ? 1 : -1) + options.length) %
              options.length,
      );
    } else if (event.key === "Escape") {
      event.preventDefault();
      setOpen(false);
    } else if (event.key === "Tab") setOpen(false);
    else if (open && ["Enter", " "].includes(event.key)) {
      event.preventDefault();
      choose(cursor);
    } else if (event.key.length === 1 && event.key !== " ") {
      const match = options.findIndex((option) =>
        option.label.toLowerCase().startsWith(event.key.toLowerCase()),
      );
      if (match >= 0) {
        event.preventDefault();
        setOpen(true);
        setCursor(match);
      }
    }
  }
  return (
    <div className="settings-field">
      {labelAction ? <div className="settings-field-heading">
        <span id={`${id}-label`} className="settings-label">{label}</span>
        {labelAction}
      </div> : <span id={`${id}-label`} className="settings-label">
        {label}
      </span>}
      <div
        className="settings-select"
        ref={root}
        onBlur={(event) => {
          if (!event.currentTarget.contains(event.relatedTarget))
            setOpen(false);
        }}
      >
        <button
          ref={trigger}
          type="button"
          className="dash-input"
          role="combobox"
          aria-labelledby={`${id}-label`}
          aria-expanded={open}
          aria-controls={open ? `${id}-list` : undefined}
          aria-activedescendant={open ? `${id}-${cursor}` : undefined}
          aria-haspopup="listbox"
          onKeyDown={keyboard}
          onClick={() => {
            setOpen(!open);
            setCursor(
              Math.max(
                0,
                options.findIndex((option) => option.value === value),
              ),
            );
          }}
        >
          <span className={!selected ? "is-placeholder" : ""}>
            {selected?.label || value || "请选择"}
          </span>
          <Icon name="chevron" />
        </button>
        {open && (
          <ul
            id={`${id}-list`}
            role="listbox"
            aria-labelledby={`${id}-label`}
            className="settings-select-menu"
          >
            {options.map((option, index) => (
              <li
                id={`${id}-${index}`}
                key={option.value}
                role="option"
                aria-selected={option.value === value}
                data-highlighted={cursor === index}
                onPointerMove={() => setCursor(index)}
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => choose(index)}
              >
                <span>{option.label}</span>
                {option.value === value && <Icon name="check" />}
              </li>
            ))}
          </ul>
        )}
      </div>
      {hint && <small>{hint}</small>}
    </div>
  );
}

function Toggle({ checked, onChange, children, disabled = false, hideLabel = false }) {
  return (
    <label className="settings-toggle">
      <input
        type="checkbox"
        role="switch"
        aria-label={hideLabel ? children : undefined}
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
        disabled={disabled}
      />
      <span className="settings-toggle-track" aria-hidden="true">
        <span />
      </span>
      {!hideLabel && <span>{children}</span>}
    </label>
  );
}

// Determine row tones from the actual field spans, including full-width JSON fields.
function Fields({ children, as: Container = "fieldset", disabled }) {
  let row = 0;
  let column = 0;
  let mobileRow = 0;
  const items = Children.toArray(children)
    .filter(isValidElement)
    .map((child) => {
      const field =
        child.type === OptionField ||
        child.type === SelectField ||
        Children.toArray(child.props.children).some(
          (item) => isValidElement(item) && item.type === SelectField,
        );
      const wide =
        !field ||
        child.props.className?.includes("settings-field-wide") ||
        child.props.spec?.type === "object";
      if (wide && column) {
        row += 1;
        column = 0;
      }
      const tone = row % 2 ? "pink" : "blue";
      const mobileTone = mobileRow % 2 ? "pink" : "blue";
      if (field) {
        mobileRow += 1;
        if (wide || column === 1) {
          row += 1;
          column = 0;
        } else column = 1;
      }
      return (
        <div
          key={child.key}
          className={`settings-grid-item${wide ? " is-wide" : ""}`}
          data-tone={tone}
          data-mobile-tone={mobileTone}
        >
          {child}
        </div>
      );
    });
  return (
    <Container
      className="settings-fields"
      disabled={Container === "fieldset" ? disabled : undefined}
    >
      {items}
    </Container>
  );
}

function SecretField({ label, value, onChange }) {
  const id = useId();
  const input = useRef(null);
  const [visible, setVisible] = useState(false);
  const environment = value && typeof value === "object" && "env" in value;
  const saved = Boolean(keepSecret(value) || environment);
  useEffect(() => {
    setVisible(false);
  }, [saved]);
  return (
    <div className="settings-field">
      <label className="settings-label" htmlFor={id}>
        {label}
      </label>
      <div
        className={`settings-input-wrap settings-secret-input${saved ? " is-saved" : ""}`}
        onBlur={(event) => {
          if (!event.currentTarget.contains(event.relatedTarget))
            setVisible(false);
        }}
      >
        <input
          ref={input}
          id={id}
          className="dash-input"
          type={visible ? "text" : "password"}
          value={saved ? "••••••••" : (value ?? "")}
          readOnly={saved}
          placeholder="请输入密钥"
          autoComplete="new-password"
          spellCheck={false}
          onChange={(event) => onChange(event.target.value)}
        />
        <div className="settings-secret-actions">
          {saved && (
            <button
              type="button"
              className="settings-text-button"
              aria-label={`替换 ${label}`}
              onClick={() => {
                setVisible(false);
                onChange("");
                input.current?.focus();
              }}
            >
              替换
            </button>
          )}
          <button
            type="button"
            className="settings-reveal"
            aria-label={`${visible ? "隐藏" : "显示"}${label}`}
            aria-pressed={visible}
            onClick={() => setVisible(!visible)}
          >
            <Icon name="eye" />
          </button>
        </div>
      </div>
    </div>
  );
}

function OptionField({ name, spec, value, onChange }) {
  const id = useId();
  const hint = ["base_url", "endpoint"].includes(name)
    ? undefined
    : spec.description || fieldHints[name];
  const label =
    name === "tool_choice" ? "工具选择（Tool Choice）" : spec.label || name;
  if (spec.secret)
    return <SecretField label={label} value={value} onChange={onChange} />;
  if (spec.type === "boolean")
    return (
      <div className="settings-boolean">
        <Toggle checked={value ?? spec.default ?? false} onChange={onChange}>
          {label}
        </Toggle>
      </div>
    );
  if (spec.enum)
    return (
      <SelectField
        label={label}
        value={value ?? spec.default ?? ""}
        onChange={onChange}
        options={spec.enum.map((item) => ({ value: item, label: item }))}
      />
    );
  const object = spec.type === "object";
  let invalid = false;
  if (object && typeof value === "string") {
    try {
      const parsed = JSON.parse(value);
      invalid = !parsed || Array.isArray(parsed) || typeof parsed !== "object";
    } catch {
      invalid = true;
    }
  }
  return (
    <div className={`settings-field${object ? " settings-field-wide" : ""}`}>
      <label className="settings-label" htmlFor={id}>
        {label}
      </label>
      <div className="settings-input-wrap">
        {object ? (
          <textarea
            className="dash-input"
            id={id}
            rows={5}
            value={
              typeof value === "string"
                ? value
                : JSON.stringify(value ?? spec.default ?? {}, null, 2)
            }
            spellCheck={false}
            aria-invalid={invalid}
            aria-describedby={`${id}-hint`}
            onChange={(event) => onChange(event.target.value)}
          />
        ) : (
          <input
            className="dash-input"
            id={id}
            type="text"
            inputMode={
              ["number", "integer"].includes(spec.type) ? "decimal" : undefined
            }
            autoComplete="off"
            spellCheck={false}
            value={value ?? ""}
            placeholder={String(
              spec.default ??
                (name === "base_url" ? "https://api.example.com/v1" : ""),
            )}
            aria-describedby={hint ? `${id}-hint` : undefined}
            onChange={(event) => onChange(event.target.value)}
          />
        )}
      </div>
      {(object || hint) && (
        <small id={`${id}-hint`} className={invalid ? "is-error" : ""}>
          {object
            ? invalid
              ? "请输入有效的 JSON 对象，例如 {}。"
              : "使用 JSON 对象配置额外选项。"
            : hint}
        </small>
      )}
    </div>
  );
}

function Disclosure({ title, description, children, className = "" }) {
  return (
    <details className={`settings-disclosure ${className}`}>
      <summary>
        <span>
          <strong>{title}</strong>
          {description && <small>{description}</small>}
        </span>
        <span className="recall-panel-toggle" aria-hidden="true" />
      </summary>
      <div className="settings-disclosure-body">{children}</div>
    </details>
  );
}

function NextButton({ next, busy }) {
  if (!next) return null;
  return (
    <button
      type="button"
      className="quiet-button settings-button settings-next-button"
      disabled={busy}
      onClick={next.onClick}
      aria-label={`下一步：${next.label}`}
    >
      下一步
    </button>
  );
}

function PreviousButton({ previous, busy }) {
  if (!previous) return null;
  return (
    <button
      type="button"
      className="quiet-button settings-button settings-previous-button"
      disabled={busy}
      onClick={previous.onClick}
      aria-label={`上一步：${previous.label}`}
    >
      上一步
    </button>
  );
}

function SettingsDialog({ title, onClose, children, className = "", returnFocusRef }) {
  const dialog = useRef(null);
  const titleId = useId();
  useEffect(() => {
    const element = dialog.current;
    const focused = document.activeElement;
    const overflow = document.body.style.overflow;
    const paddingRight = document.body.style.paddingRight;
    const scrollbarWidth = window.innerWidth - document.documentElement.clientWidth;
    if (scrollbarWidth > 0) document.body.style.paddingRight = `${parseFloat(getComputedStyle(document.body).paddingRight) + scrollbarWidth}px`;
    document.body.style.overflow = "hidden";
    element.showModal();
    return () => {
      element.close();
      document.body.style.overflow = overflow;
      document.body.style.paddingRight = paddingRight;
      const target = returnFocusRef?.current || focused;
      if (target?.isConnected) target.focus();
    };
  }, []);
  return (
    <dialog
      ref={dialog}
      className={`confirm-card settings-apply-dialog ${className}`}
      aria-labelledby={titleId}
      aria-modal="true"
      onCancel={event => { event.preventDefault(); onClose?.(); }}
      onKeyDown={event => {
        if (event.key !== "Tab") return;
        const buttons = [...dialog.current.querySelectorAll("button:not(:disabled), input:not(:disabled)")];
        if (!buttons.length) {
          event.preventDefault();
          dialog.current.focus();
        } else if (event.shiftKey && (document.activeElement === buttons[0] || document.activeElement === dialog.current)) {
          event.preventDefault();
          buttons.at(-1).focus();
        } else if (!event.shiftKey && document.activeElement === buttons.at(-1)) {
          event.preventDefault();
          buttons[0].focus();
        }
      }}
    >
      <h2 id={titleId}>{title}</h2>
      {children}
    </dialog>
  );
}

export function ApplyDialog({ progress, onClose, onRetry }) {
  const messageId = useId();
  const busy = progress.state === "saving" || progress.state === "applying";
  return (
    <SettingsDialog title={progress.title} onClose={busy ? undefined : onClose}>
      <div id={messageId} role="status" aria-live="polite" aria-atomic="true">
        {busy ? <Loading>{progress.message}</Loading> : progress.message ? <p className="confirm-copy">{progress.message}</p> : null}
      </div>
      {!busy && (
        <div className="confirm-actions">
          <button className="quiet-button" type="button" onClick={onClose}>返回设置</button>
          {progress.state === "timeout" && <button className="quiet-button pink" type="button" onClick={onRetry}>继续检查</button>}
        </div>
      )}
    </SettingsDialog>
  );
}

export function SaveBar({ busy, dirty, status, next, previous, hint = "", saveDisabled = false }) {
  return (
    <footer className="settings-save-bar">
      <PreviousButton previous={previous} busy={busy} />
      <div
        className={`settings-save-status${status?.error ? " is-error" : ""}`}
        role="status"
      >
        <span>
          {status?.text || (dirty ? "有修改尚未保存" : "")}
          {hint && <small>{hint}</small>}
        </span>
      </div>
      <div className="settings-save-actions">
        <button
          type="submit"
          className="quiet-button settings-button"
          disabled={!dirty || busy || saveDisabled}
        >
          <Icon
            name={busy ? "refresh" : "memory"}
            className={busy ? "is-spinning" : ""}
          />
          {busy ? "保存中…" : "保存"}
        </button>
        <NextButton next={next} busy={busy} />
      </div>
    </footer>
  );
}

function providerDraft(names, data, optional) {
  const enabled =
    !optional || names.some((name) => data.capabilities[name]?.enabled);
  return {
    enabled,
    values: Object.fromEntries(
      names.map((name) => [
        name,
        {
          adapter:
            data.capabilities[name]?.adapter ||
            (name === "llm" && data.adapters.some((adapter) => adapter.capability === "llm" && adapter.adapter === "openai") ? "openai" : "") ||
            data.adapters.find((adapter) => adapter.capability === name)
              ?.adapter ||
            "",
          enabled: true,
          options: data.capabilities[name]?.options || {},
        },
      ]),
    ),
  };
}

function normalizeProvider(name, value, adapters) {
  const fields =
    adapters.find(
      (adapter) =>
        adapter.capability === name && adapter.adapter === value.adapter,
    )?.fields || {};
  const options = Object.fromEntries(
    Object.entries(value.options).filter(([key]) => Object.hasOwn(fields, key)),
  );
  for (const [key, spec] of Object.entries(fields)) {
    if (options[key] === "" || options[key] === undefined) {
      delete options[key];
      continue;
    }
    if (spec.type === "object" && typeof options[key] === "string") {
      try {
        options[key] = JSON.parse(options[key]);
      } catch {
        throw new Error(`${spec.label || key} 需要有效的 JSON 对象。`);
      }
      if (
        !options[key] ||
        Array.isArray(options[key]) ||
        typeof options[key] !== "object"
      )
        throw new Error(`${spec.label || key} 需要有效的 JSON 对象。`);
    }
    if (["integer", "number"].includes(spec.type)) {
      options[key] = Number(options[key]);
      if (
        !Number.isFinite(options[key]) ||
        (spec.type === "integer" && !Number.isInteger(options[key]))
      )
        throw new Error(
          `${spec.label || key} 需要填写${spec.type === "integer" ? "整数" : "有效数字"}。`,
        );
    }
  }
  return { ...value, options };
}

function ProviderSection({ module, data, save, saving, testProvider, testing, next, previous }) {
  const configurationId = useId();
  const { names, optional } = module;
  const [draft, setDraft] = useState(() =>
    providerDraft(names, data, optional),
  );
  const [saved, setSaved] = useState(draft);
  const [status, setStatus] = useState(null);
  const [busy, setBusy] = useState(false);
  const [showDisabled, setShowDisabled] = useState(false);
  const [testResult, setTestResult] = useState(null);
  const [testPending, setTestPending] = useState(false);
  const testVersion = useRef(0);
  const testLock = useRef(false);
  const testButtonRef = useRef(null);
  const testCapability = names.find(name => data.adapters.some(adapter =>
    adapter.capability === name && adapter.adapter === draft.values[name].adapter && adapter.test_supported === true,
  ));
  useEffect(() => () => { testVersion.current += 1; }, []);
  const dirty = !equal(draft, saved);
  function change(next) {
    testVersion.current += 1;
    setTestResult(null);
    setDraft(next);
    setStatus(null);
  }
  async function testConnection() {
    if (!testCapability || testing || testLock.current || saving || busy) return;
    const version = ++testVersion.current;
    testLock.current = true;
    setTestPending(true);
    setTestResult(null);
    setStatus(null);
    try {
      const { adapter, options } = normalizeProvider(testCapability, draft.values[testCapability], data.adapters);
      const fields = data.adapters.find(item => item.capability === testCapability && item.adapter === adapter)?.fields || {};
      for (const [key, spec] of Object.entries(fields)) {
        if (spec.secret && keepSecret(options[key])) throw new Error(`请重新填写${spec.label || key}后测试连接。`);
      }
      const result = await testProvider(testCapability, { adapter, options });
      if (version === testVersion.current) setTestResult(result);
    } catch (error) {
      if (version === testVersion.current) setTestResult({ text: error.message, error: true });
    } finally {
      testLock.current = false;
      setTestPending(false);
    }
  }
  async function submit(event) {
    event.preventDefault();
    if (saving || busy || !dirty) return;
    setBusy(true);
    testVersion.current += 1;
    setTestResult(null);
    setStatus(null);
    try {
      const document = Object.fromEntries(
        names.map((name) => [
          name,
          {
            ...normalizeProvider(name, draft.values[name], data.adapters),
            enabled: draft.enabled,
          },
        ]),
      );
      const result = await save("/api/settings/providers", document);
      const next = {
        enabled: draft.enabled,
        values: Object.fromEntries(
          names.map((name) => [
            name,
            {
              ...result.capabilities[name],
              enabled: draft.values[name].enabled,
              options: {
                // Keep other adapters' draft fields locally; only the active
                // adapter's normalized options are sent to the server.
                ...Object.fromEntries(
                  Object.entries(draft.values[name].options).filter(([key]) =>
                    !Object.hasOwn(
                      data.adapters.find((adapter) =>
                        adapter.capability === name && adapter.adapter === draft.values[name].adapter,
                      )?.fields || {},
                      key,
                    ),
                  ),
                ),
                ...result.capabilities[name].options,
              },
            },
          ]),
        ),
      };
      setDraft(next);
      setSaved(next);
      setStatus(result.applyStatus);
    } catch (error) {
      setStatus({ text: error.message, error: true });
    } finally {
      setBusy(false);
    }
  }
  const testButton = testCapability && (
    <button ref={testButtonRef} type="button" className="settings-text-button settings-test-button"
      disabled={testing || saving || busy} onClick={testConnection}>
      <Icon name="refresh" className={testPending ? "is-spinning" : ""} />
      {testPending ? "测试中…" : "测试连接"}
    </button>
  );
  return (
    <>
      {testResult && (
        <SettingsDialog title="连接测试" className="settings-connection-test-dialog" onClose={() => setTestResult(null)} returnFocusRef={testButtonRef}>
          <p className={`confirm-copy${testResult.error ? " is-error" : ""}`} role={testResult.error ? "alert" : "status"}>
            {testResult.text}
          </p>
          <div className="confirm-actions">
            <button type="button" className="quiet-button" onClick={() => setTestResult(null)}>关闭</button>
          </div>
        </SettingsDialog>
      )}
      <form
        noValidate
        onSubmit={submit}
        data-dirty={dirty}
        data-config-dirty={dirty}
      >
        <SectionHeader
          module={module}
          control={
            optional && (
              <Toggle
                checked={draft.enabled}
                disabled={saving}
                onChange={(enabled) => change({ ...draft, enabled })}
              >
                启用功能
              </Toggle>
            )
          }
        />
        <div className="settings-form-body">
          {!draft.enabled && (
            <div className="settings-disabled">
              <div className="settings-disabled-copy">
                <h3>
                  {saved.enabled ? "将在保存后停用" : "功能未启用"}
                </h3>
                <p>{module.tip}</p>
                {testButton}
              </div>
              <button
                type="button"
                className="settings-disabled-toggle"
                aria-expanded={showDisabled}
                aria-controls={configurationId}
                onClick={() => setShowDisabled(!showDisabled)}
              >
                <span>{showDisabled ? "收起已有配置" : "查看已有配置"}</span>
                <span className="recall-panel-toggle" aria-hidden="true" />
              </button>
            </div>
          )}
          <div
            id={configurationId}
            hidden={!draft.enabled && !showDisabled}
          >
            {names.map((name) => {
              const value = draft.values[name];
              const adapters = data.adapters.filter(
                (adapter) => adapter.capability === name,
              );
              const fields = Object.entries(
                adapters.find((adapter) => adapter.adapter === value.adapter)
                  ?.fields || {},
              );
              const basic = fields.filter(([key]) =>
                primaryFields[name]?.includes(key),
              );
              if (name === "balance") {
                basic.sort(([left], [right]) =>
                  primaryFields.balance.indexOf(left) - primaryFields.balance.indexOf(right),
                );
              }
              const advanced = fields.filter(
                ([key]) =>
                  !primaryFields[name]?.includes(key) &&
                  !hiddenFields[name]?.includes(key),
              );
              const update = (next) =>
                change({ ...draft, values: { ...draft.values, [name]: next } });
              const renderField = ([key, spec]) => {
                if (name === "llm" && key === "thinking" && spec.properties?.effort?.enum) {
                  const effort = spec.properties.effort;
                  return <SelectField key={`${value.adapter}-${key}`} label={effort.label || "默认思考强度"}
                    value={value.options.thinking?.effort ?? effort.default ?? ""}
                    options={effort.enum.map(option => ({ value: option, label: option === "" ? "服务商默认" : option }))}
                    onChange={next => update({ ...value, options: { ...value.options,
                      thinking: { ...value.options.thinking, effort: next },
                    } })}
                  />;
                }
                return (
                <OptionField
                  key={`${value.adapter}-${key}`}
                  name={key}
                  spec={spec}
                  value={value.options[key]}
                  onChange={(next) =>
                    update({
                      ...value,
                      options: { ...value.options, [key]: next },
                    })
                  }
                />
                );
              };
              return (
                <div key={name}>
                  <Fields
                    disabled={!draft.enabled || saving}
                  >
                    <div
                      className={
                        basic.some(([name]) => name === "model")
                          ? undefined
                          : "settings-field-wide"
                      }
                    >
                      <SelectField
                        label="服务协议"
                        labelAction={draft.enabled && name === testCapability ? testButton : undefined}
                        value={value.adapter}
                        onChange={(adapter) =>
                          update({ ...value, adapter })
                        }
                        options={adapters.map((adapter) => ({
                          value: adapter.adapter,
                          label:
                            adapterLabels[adapter.adapter] || adapter.adapter,
                        }))}
                      />
                    </div>
                    {basic
                      .filter(([name]) => name === "model")
                      .map(renderField)}
                    {basic
                      .filter(([name]) => name !== "model")
                      .map(renderField)}
                    {advanced.length > 0 && (
                      <Disclosure
                        title="高级参数"
                        description={`${advanced.length} 项 · 超时、输出与其他选项`}
                      >
                        <Fields as="div">{advanced.map(renderField)}</Fields>
                      </Disclosure>
                    )}
                  </Fields>
                </div>
              );
            })}
          </div>
        </div>
        <SaveBar
          busy={saving || busy}
          dirty={dirty}
          status={status}
          saveDisabled={testing}
          next={next}
          previous={previous}
        />
      </form>
    </>
  );
}

function SectionHeader({ module, control }) {
  return (
    <header className="settings-panel-header">
      <div className="settings-panel-title">
        <h2>{module.label}</h2>
        <span className="panel-label">SETTINGS // {module.id.toUpperCase()}</span>
      </div>
      <div className="settings-panel-control">{control}</div>
    </header>
  );
}

// The MCP PATCH endpoint is currently a no-op; keep editing local until it can save.
function McpSection({ module, request, token, active, busy, previous, next }) {
  const [content, setContent] = useState(null);
  const [draft, setDraft] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const dirty = content !== null && draft !== content;
  useEffect(() => {
    if (!active || content !== null) return;
    const controller = new AbortController();
    setLoading(true);
    setError("");
    request("/api/settings/mcp", { token, responseType: "text", signal: AbortSignal.any([controller.signal, AbortSignal.timeout(30000)]) })
      .then(value => {
        if (controller.signal.aborted) return;
        if (typeof value !== "string") throw new Error("无法读取 mcp.json，请刷新后重试。");
        setDraft(value);
        setContent(value);
        setLoading(false);
      })
      .catch(problem => {
        if (!controller.signal.aborted) { setError(problem.message); setLoading(false); }
      });
    return () => controller.abort();
  }, [active, content, request, token]);
  return (
    <form onSubmit={event => event.preventDefault()} data-dirty={dirty}>
      <SectionHeader module={module} />
      <div className="settings-form-body settings-persona settings-mcp">
        <div className="prompt-card">
          <p className="prompt-description">连接外部 MCP 服务，为 Momoi 提供更多工具。</p>
          <label className="prompt-field">
            <span>mcp.json</span>
            <textarea
              className="dash-input"
              aria-label="mcp.json"
              value={draft}
              placeholder={loading ? "正在读取 mcp.json…" : ""}
              spellCheck={false}
              autoCapitalize="off"
              autoCorrect="off"
              disabled={busy || loading || content === null}
              aria-busy={loading}
              onChange={event => setDraft(event.target.value)}
            />
          </label>
        </div>
      </div>
      <SaveBar busy={busy} dirty={dirty} status={error ? { text: error, error: true } : null} saveDisabled previous={previous} next={next} />
    </form>
  );
}

function TimeField({ label, value, onChange, allowDisabled = false }) {
  const options = Array.from({ length: 48 }, (_, index) => {
    const text = `${String(Math.floor(index / 2)).padStart(2, "0")}:${index % 2 ? "30" : "00"}`;
    return { value: text, label: text };
  });
  if (allowDisabled) options.unshift({ value: "disabled", label: "禁用" });
  return (
    <SelectField label={label} value={value || "03:00"} options={options} onChange={onChange} />
  );
}

function RuntimePropertyFields({ spec, value, onChange }) {
  return (
    <div className="settings-runtime-properties">
      {Object.entries(spec.properties).map(([key, child]) => {
        const update = next => onChange({ ...value, [key]: next });
        if (child.properties) return <RuntimePropertyFields key={key} spec={child} value={value?.[key]} onChange={update} />;
        return <SelectField key={key} label={child.label || key} value={value?.[key] ?? child.default}
          options={child.enum.map(option => ({ value: option, label: option === "" ? "跟随模型" : option }))} onChange={update} />;
      })}
    </div>
  );
}

const runtimeOrder = { heartbeat: 0, episode_annealing: 1, logging: 2, reflection: 3, thinking: 4 };
const runtimeDescriptions = {
  heartbeat: "心跳是 Momoi 的自主时间。她可以探索、创作、延续自己的活动，也可以休息或主动与你分享。",
  logging: "控制运行日志的详细程度，用于查看服务状态与排查问题。",
  reflection: "每天在设定时间回顾对话与活动，记录感受、关系变化和可复用的经验。",
  episode_annealing: "将值得记住的对话按话题整理、生成摘要，并保留原始记录，便于日后回忆与延续。",
  thinking: "为不同运行阶段设置思考强度；默认跟随模型，单独设置后不随模型切换而改变。",
};

function RuntimeSection({ module, data, save, saving, previous, next }) {
  const schemas = data.app_fields || {};
  const initial = () => Object.fromEntries(Object.entries(schemas).map(([name, schema]) => [
    name,
    Object.fromEntries(Object.entries(schema.fields).map(([key, spec]) => [key, runtimeFieldValue(spec, data.app[name]?.[key])])),
  ]));
  const [draft, setDraft] = useState(initial);
  const [saved, setSaved] = useState(initial);
  const [status, setStatus] = useState(null);
  const dirty = !equal(draft, saved);
  async function submit(event) {
    event.preventDefault();
    if (saving || !dirty) return;
    try {
      const document = {};
      for (const [name, schema] of Object.entries(schemas)) {
        const changes = {};
        for (const [key, spec] of Object.entries(schema.fields)) {
          const value = draft[name][key];
          const change = runtimeFieldChanges(spec, value, saved[name][key]);
          if (change === undefined) continue;
          if (spec.pattern && !new RegExp(spec.pattern).test(String(value))) {
            throw new Error(`${spec.label}请使用 HH:MM 格式，例如 03:00。`);
          }
          changes[key] = change;
        }
        if (Object.keys(changes).length) document[name] = changes;
      }
      const result = await save("/api/settings/configuration/app", document, "PATCH");
      const updated = Object.fromEntries(Object.entries(schemas).map(([name, schema]) => [
        name, Object.fromEntries(Object.entries(schema.fields).map(([key, spec]) => [key, runtimeFieldValue(spec, result.app[name]?.[key])])),
      ]));
      setDraft(updated);
      setSaved(updated);
      setStatus(result.applyStatus);
    } catch (error) { setStatus({ text: error.message, error: true }); }
  }
  return (
    <form noValidate onSubmit={submit} data-dirty={dirty} data-config-dirty={dirty}>
      <SectionHeader module={module} />
      <div className="settings-form-body settings-runtime-controls">
        {Object.entries(schemas).sort(([a], [b]) => (runtimeOrder[a] ?? 99) - (runtimeOrder[b] ?? 99)).map(([name, schema]) => (
          <section className={`settings-runtime-group${Object.values(schema.fields).some(spec => spec.properties) ? " settings-runtime-nested" : ""}`} key={name} aria-labelledby={`runtime-${name}`}>
            <div className="settings-runtime-copy">
              <div className="settings-voice-title">
                <h3 id={`runtime-${name}`}>{name === "episode_annealing" ? "话题归档" : schema.label}</h3>
                <span className="panel-label">RUNTIME // {name === "episode_annealing" ? "ARCHIVE" : name.toUpperCase()}</span>
              </div>
              {runtimeDescriptions[name] && <p className="settings-runtime-description" id={`runtime-${name}-description`}>{runtimeDescriptions[name]}</p>}
            </div>
            <fieldset className="settings-runtime-fields" disabled={saving} aria-describedby={runtimeDescriptions[name] ? `runtime-${name}-description` : undefined}>
              {Object.entries(schema.fields).sort(([, a], [, b]) => Number(a.type === "boolean") - Number(b.type === "boolean")).map(([key, spec]) => {
                if (name === "reflection" && key === "enabled") return null;
                if (name === "reflection" && key === "at") return (
                  <TimeField key={key} label={spec.label} allowDisabled
                    value={draft[name].enabled ? draft[name][key] : "disabled"}
                    onChange={value => {
                      setDraft(current => ({ ...current, reflection: {
                        ...current.reflection,
                        enabled: value !== "disabled",
                        at: value === "disabled" ? "03:00" : value,
                      } }));
                      setStatus(null);
                    }}
                  />
                );
                const onChange = value => {
                  setDraft(current => ({ ...current, [name]: { ...current[name], [key]: value } }));
                  setStatus(null);
                };
                if (spec.properties) return <RuntimePropertyFields key={key} spec={spec} value={draft[name][key]} onChange={onChange} />;
                if (spec.type === "boolean") return (
                  <Toggle key={key} checked={draft[name][key]} disabled={saving} onChange={onChange} hideLabel>
                    {name === "episode_annealing" ? "启用归档" : spec.label}
                  </Toggle>
                );
                return spec.format === "time"
                  ? <TimeField key={key} label={spec.label} value={draft[name][key]} onChange={onChange} />
                  : <OptionField key={key} name={key} spec={spec} value={draft[name][key]} onChange={onChange} />;
              })}
            </fieldset>
          </section>
        ))}
        {!Object.keys(schemas).length && <p className="settings-channel-note">当前服务尚未提供运行设置，请更新服务后刷新。</p>}
      </div>
      <SaveBar busy={saving} dirty={dirty} status={status} previous={previous} next={next} />
    </form>
  );
}

const channelOptions = [
  { value: "weixin", label: "微信" },
  { value: "napcat", label: "Napcat QQ" },
];
const channelDefaults = (name) =>
  name === "napcat" ? { url: "ws://127.0.0.1:3001", owner_qq: "" } : {};
function ChannelSection({ module, data, save, login, action, saving, actionBusy, next, previous }) {
  const [channels, setChannels] = useState(data.app.channels || { primary: "", enabled: {} });
  const [saved, setSaved] = useState(channels);
  const [status, setStatus] = useState(null);
  const [code, setCode] = useState("");
  const [loginPending, setLoginPending] = useState(false);
  const [loginOpen, setLoginOpen] = useState(false);
  const [loginError, setLoginError] = useState("");
  const cached = useRef({ ...channels.enabled });
  const dirty = !equal(channels, saved);
  const loginActive = ["starting", "waiting", "scanned", "verification_required"].includes(login?.status);
  const busy = saving || actionBusy || loginPending;
  const choices = [...channelOptions, ...Object.keys(channels.enabled).filter(name => !channelOptions.some(option => option.value === name)).map(name => ({ value: name, label: name }))];
  const enabledChoices = choices.filter(option => option.value in channels.enabled);
  function change(value) { setChannels(value); setStatus(null); }
  function toggle(name, enabled) {
    const values = { ...channels.enabled };
    if (enabled) values[name] = cached.current[name] || channelDefaults(name);
    else { cached.current[name] = values[name]; delete values[name]; }
    change({ ...channels, enabled: values, primary: channels.primary in values ? channels.primary : Object.keys(values)[0] || "" });
  }
  function edit(name, values) {
    cached.current[name] = values;
    change({ ...channels, enabled: { ...channels.enabled, [name]: values } });
  }
  async function submit(event) {
    event.preventDefault();
    if (busy || loginActive || !dirty) return;
    try {
      if (!channels.primary || !(channels.primary in channels.enabled)) throw new Error("请至少启用一个消息渠道。");
      if (channels.enabled.napcat) {
        if (!/^\d+$/.test(channels.enabled.napcat.owner_qq || "")) throw new Error("请填写主人 QQ 号码，仅包含数字。");
        let url;
        try { url = new URL(channels.enabled.napcat.url); } catch { throw new Error("请填写有效的 NapCat WebSocket 地址。"); }
        if (!["ws:", "wss:"].includes(url.protocol)) throw new Error("NapCat 地址需要以 ws:// 或 wss:// 开头。");
      }
      const result = await save("/api/settings/configuration/app", { channels }, "PATCH");
      setChannels(result.app.channels);
      setSaved(result.app.channels);
      cached.current = { ...cached.current, ...result.app.channels.enabled };
      setStatus(result.applyStatus);
    } catch (error) { setStatus({ text: error.message, error: true }); }
  }
  async function loginAction(path, body, method) {
    if (busy) return;
    setLoginPending(true);
    setLoginError("");
    try {
      const ok = await action(path, body, method);
      if (!ok) setLoginError("操作失败，请重试。");
      return ok;
    }
    finally { setLoginPending(false); }
  }
  async function startLogin() {
    setCode("");
    setLoginOpen(true);
    if (!loginActive) await loginAction("/api/settings/channels/weixin/login");
  }
  async function closeLogin() {
    if (busy) return;
    if (loginActive && !await loginAction("/api/settings/channels/weixin/login", undefined, "DELETE")) return;
    setLoginOpen(false);
    setCode("");
  }
  return (
    <form noValidate onSubmit={submit} data-dirty={dirty} data-config-dirty={dirty}>
      <SectionHeader module={module} />
      <div className="settings-form-body settings-channels">
        <div className="settings-channel-grid">
          {choices.map(({ value: name, label }) => {
            const enabled = name in channels.enabled;
            const options = channels.enabled[name] || {};
            const titleId = `settings-channel-${name}`;
            return (
              <section className="settings-channel-column" key={name} aria-labelledby={titleId}>
                <header className="settings-channel-heading">
                  <div className="settings-channel-title">
                    <h3 id={titleId}>{label}</h3>
                    <span className="panel-label">CHANNEL // {name === "weixin" ? "WEIXIN" : name === "napcat" ? "QQ" : name.toUpperCase()}</span>
                  </div>
                  <Toggle checked={enabled} disabled={busy || loginActive} onChange={value => toggle(name, value)}>启用{label}</Toggle>
                </header>
                <p className="settings-channel-description">
                  {name === "weixin" ? "启用并保存后，使用微信扫码登录。" : name === "napcat" ? "填写 NapCat 连接信息，保存后自动连接。" : "配置并保存后连接此渠道。"}
                </p>
                <div className="settings-channel-content">
                {!enabled ? (
                  <div className="settings-channel-off">
                    <span className="settings-channel-off-icon" aria-hidden="true"><Icon name={name === "weixin" ? "weixin" : name === "napcat" ? "qq" : "chat"} /></span>
                    <div>
                      <strong>{name === "weixin" ? "在微信里和 Momoi 聊天" : name === "napcat" ? "在 QQ 里和 Momoi 聊天" : `连接${label}`}</strong>
                      <p>{name === "weixin" ? "打开上方开关，保存后扫码登录。" : name === "napcat" ? "打开上方开关，填写 Napcat 连接信息。" : "打开上方开关，接入这个消息渠道。"}</p>
                    </div>
                  </div>
                ) : name === "napcat" ? (
                  <Fields disabled={busy || loginActive}>
                    <OptionField name="url" spec={{ type: "string", label: "NapCat WebSocket 地址" }} value={options.url} onChange={value => edit(name, { ...options, url: value })} />
                    <OptionField name="owner_qq" spec={{ type: "string", label: "主人 QQ" }} value={options.owner_qq} onChange={value => edit(name, { ...options, owner_qq: value })} />
                  </Fields>
                ) : name === "weixin" ? (
                  <div className="settings-channel-login">
                    <button type="button" className="quiet-button settings-button" disabled={busy || dirty || !saved.enabled.weixin} onClick={startLogin}>
                      {loginActive ? "查看登录进度" : "扫码登录"}
                    </button>
                    <p className="settings-channel-note" style={{ visibility: dirty || !saved.enabled.weixin ? "visible" : "hidden" }}>请先点击底部“保存”，再扫码登录。</p>
                  </div>
                ) : <p className="settings-channel-note">保留已有连接配置。</p>}
                </div>
              </section>
            );
          })}
        </div>
        <fieldset className="settings-channel-primary" disabled={busy || loginActive || enabledChoices.length < 2}>
            <SelectField label="主消息渠道" value={channels.primary} options={enabledChoices.length ? enabledChoices : [{ value: "", label: "请先启用一个渠道" }]} onChange={primary => { if (!busy && !loginActive) change({ ...channels, primary }); }} hint="启用多个渠道后，可切换主消息渠道。" />
          </fieldset>
      </div>
      {loginOpen && (
        <SettingsDialog title="微信扫码登录" onClose={closeLogin} className="settings-weixin-dialog">
          <div className="settings-weixin-content">
            {loginPending || !login?.status || login.status === "starting" ? (
              <Loading>正在处理登录…</Loading>
            ) : (
              <>
                {login?.qr_image && ["waiting", "scanned"].includes(login.status) && <img className="settings-qr" src={login.qr_image} alt="使用微信扫描此二维码登录" />}
                <p className={`confirm-copy${login.status === "error" ? " is-error" : ""}`} role="status">
                  {{ waiting: "请使用微信扫描二维码。", scanned: "已扫码，请在微信中确认。", confirmed: "微信登录成功。", expired: "二维码已过期，请重新扫码。", cancelled: "登录已取消。", verification_required: "请填写微信中显示的验证码。", error: `登录失败：${login.error || "请重试"}` }[login.status]}
                </p>
                {login.status === "verification_required" && (
                  <div className="settings-verification">
                    <label className="settings-field">
                      <span className="settings-label">微信验证码</span>
                      <input className="dash-input" inputMode="numeric" autoComplete="one-time-code" maxLength={12} value={code} onChange={event => setCode(event.target.value)} onKeyDown={event => { if(event.key === "Enter") { event.preventDefault(); if(/^\d{1,12}$/.test(code)) loginAction("/api/settings/channels/weixin/verify", { code }); } }} />
                    </label>
                    <button type="button" className="quiet-button settings-button" disabled={!/^\d{1,12}$/.test(code)} onClick={async () => { if(await loginAction("/api/settings/channels/weixin/verify", { code })) setCode(""); }}>提交验证码</button>
                  </div>
                )}
              </>
            )}
            {loginError && <p className="confirm-copy is-error" role="alert">{loginError}</p>}
          </div>
          <div className="confirm-actions">
            <button type="button" className="quiet-button" disabled={busy} onClick={closeLogin}>关闭</button>
            {!loginActive && (loginError || ["expired", "error", "cancelled", "idle"].includes(login?.status)) && <button type="button" className="quiet-button" disabled={busy} onClick={startLogin}>重新获取二维码</button>}
          </div>
        </SettingsDialog>
      )}
      <SaveBar busy={busy || loginActive} dirty={dirty} status={status} next={next} previous={previous} />
    </form>
  );
}

export default function ConfigurationSettings({
  token,
  request,
  promptContent,
}) {
  const [data, setData] = useState(null);
  const [runtime, setRuntime] = useState(null);
  const [error, setError] = useState("");
  const [pollError, setPollError] = useState("");
  const [loading, setLoading] = useState(true);
  const [generation, setGeneration] = useState(0);
  const [activeSection, setActiveSection] = useState("model");
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const providerTestLock = useRef(false);
  const providerTestController = useRef(null);
  const [applyProgress, setApplyProgress] = useState(null);
  const applyController = useRef(null);
  const applyRevision = useRef(null);
  const [actionBusy, setActionBusy] = useState(false);
  const root = useRef(null);
  const saveLock = useRef(false);
  const revision = useRef(null);
  const loadController = useRef(null);
  const call = async (path, options = {}) => {
    try {
      return await request(path, { ...options, token });
    } catch (problem) {
      let message = problem.message;
      try {
        const body = JSON.parse(message);
        message =
          typeof body.error === "string"
            ? body.error
            : typeof body.detail === "string"
              ? body.detail
              : message;
      } catch {
        /* Keep non-JSON errors readable. */
      }
      throw new Error(message);
    }
  };
  async function load() {
    loadController.current?.abort();
    const controller = new AbortController();
    loadController.current = controller;
    setLoading(true);

    try {
      const result = await call("/api/settings/configuration", {
        signal: controller.signal,
      });
      if (controller.signal.aborted) return;
      if (!result?.capabilities || !result?.adapters || !result?.app)
        throw new Error(
          "配置数据不完整，请确认已连接支持配置管理的 Momoi 服务。",
        );
      revision.current = result.revision;
      setData(result);
      setGeneration((value) => value + 1);
      setError("");
    } catch (problem) {
      if (!controller.signal.aborted) setError(problem.message);
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }
  useEffect(() => {
    load();
    return () => loadController.current?.abort();
  }, [token]);
  useEffect(() => {
    let active = true;
    let timer;
    const controller = new AbortController();
    async function poll() {
      let delay = 2000;
      try {
        const value = await call("/api/settings/runtime", {
          signal: AbortSignal.any([controller.signal, AbortSignal.timeout(8000)]),
        });
        if (["starting", "waiting", "scanned", "verification_required"].includes(value?.weixin_login?.status)) delay = 1000;
        if (active) {
          setRuntime(value);
          setPollError("");
        }
      } catch (problem) {
        if (active) setPollError(`运行状态暂时无法更新：${problem.message}`);
      } finally {
        if (active) timer = setTimeout(poll, delay);
      }
    }
    poll();
    return () => {
      active = false;
      clearTimeout(timer);
      controller.abort();
    };
  }, [token]);
  useEffect(() => {
    const beforeUnload = (event) => {
      if (saveLock.current || root.current?.querySelector('[data-dirty="true"]')) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", beforeUnload);
    return () => window.removeEventListener("beforeunload", beforeUnload);
  }, []);
  useEffect(() => () => applyController.current?.abort(), []);
  useEffect(() => () => providerTestController.current?.abort(), []);
  async function testProvider(capability, document) {
    if (providerTestLock.current) throw new Error("已有连接测试正在进行，请稍后重试。");
    providerTestLock.current = true;
    const controller = new AbortController();
    providerTestController.current = controller;
    setTesting(true);
    try {
      return await testProviderConnection(
        (path, options) => request(path, { ...options, token }),
        capability, document, controller.signal,
      );
    } finally {
      providerTestLock.current = false;
      setTesting(false);
    }
  }
  async function monitorApply(target) {
    applyController.current?.abort();
    const controller = new AbortController();
    applyController.current = controller;
    setApplyProgress({ state: "applying", title: "保存成功", message: "正在等待配置生效，如需重启请稍候…" });
    const outcome = await waitForConfiguration(call, target, { signal: controller.signal });
    setApplyProgress(outcome);
    return outcome;
  }
  async function retryMonitor() {
    saveLock.current = true;
    setSaving(true);
    try { await monitorApply(applyRevision.current); }
    catch (problem) {
      if (!applyController.current?.signal.aborted) {
        setApplyProgress({ state: "timeout", title: "尚未确认重启结果", message: problem.message });
      }
    } finally { saveLock.current = false; setSaving(false); }
  }
  async function save(path, document, method = "PUT") {
    if (saveLock.current) throw new Error("另一项配置正在保存，请稍后再试。");
    saveLock.current = true;
    applyController.current = new AbortController();
    setSaving(true);
    setApplyProgress({ state: "saving", title: "正在保存配置", message: "正在提交修改…" });
    try {
      const result = await call(path, {
        method,
        body: { revision: revision.current, document },
        signal: AbortSignal.any([applyController.current.signal, AbortSignal.timeout(30000)]),
      });
      revision.current = result.revision;
      setData(result);
      applyRevision.current = result.revision;
      const outcome = await monitorApply(result.revision);
      return { ...result, applyStatus: { text: outcome.title, error: outcome.state === "error" || outcome.state === "timeout" } };
    } catch (problem) {
      if (!applyController.current?.signal.aborted) {
        setApplyProgress({ state: "error", title: "未能完成保存", message: `${problem.message}。请刷新检查配置后再试。` });
      }
      throw problem;
    } finally {
      saveLock.current = false;
      setSaving(false);
    }
  }
  async function action(path, body, method = "POST") {
    setActionBusy(true);
    try {
      await call(path, { method, body, signal: AbortSignal.timeout(30000) });
      const value = await call("/api/settings/runtime", { signal: AbortSignal.timeout(8000) });
      setRuntime(value);
      setPollError("");
      setError("");
      return true;
    } catch (problem) {
      setError(problem.message);
      return false;
    } finally {
      setActionBusy(false);
    }
  }
  const issue = error || pollError || runtime?.error || data?.validation_error;
  const runtimeContent = issue ? (
    <section className="settings-runtime" aria-label="配置错误">
      <p className="settings-runtime-message is-error" role="alert">
        <Icon name="info" />
        {issue}
      </p>
    </section>
  ) : null;
  return (
    <div className="settings-studio" ref={root}>
      {applyProgress && <ApplyDialog progress={applyProgress} onClose={() => setApplyProgress(null)} onRetry={retryMonitor} />}
      {data ? (
        <div className="settings-workspace">
          <aside className="settings-module-rail">
            <div className="dash-tabs" role="tablist" aria-label="配置分类">
              {modules.map((module) => (
                <button
                  key={module.id}
                  type="button"
                  role="tab"
                  id={`settings-tab-${module.id}`}
                  aria-selected={activeSection === module.id}
                  className={activeSection === module.id ? "active" : ""}
                  tabIndex={activeSection === module.id ? 0 : -1}
                  onKeyDown={(event) => {
                    if (
                      !["ArrowLeft", "ArrowRight", "Home", "End"].includes(
                        event.key,
                      )
                    )
                      return;
                    event.preventDefault();
                    const index = modules.indexOf(module);
                    const next =
                      event.key === "Home"
                        ? 0
                        : event.key === "End"
                          ? modules.length - 1
                          : (index +
                              (event.key === "ArrowRight" ? 1 : -1) +
                              modules.length) %
                            modules.length;
                    setActiveSection(modules[next].id);

                    document
                      .getElementById(`settings-tab-${modules[next].id}`)
                      ?.focus();
                  }}
                  aria-controls={`settings-panel-${module.id}`}
                  onClick={() => {
                    setActiveSection(module.id);
                  }}
                >
                  <span>{module.label}</span>
                </button>
              ))}
            </div>
          </aside>
          <div className="settings-panels">
            {modules.map((module, index) => {
              const preceding = modules[index - 1];
              const previous = preceding ? {
                label: preceding.label,
                onClick: () => {
                  setActiveSection(preceding.id);
                  document.getElementById(`settings-tab-${preceding.id}`)?.focus();
                },
              } : null;
              const following = modules[index + 1];
              const next = following
                ? {
                    label: following.label,
                    onClick: () => {
                      setActiveSection(following.id);
                      document
                        .getElementById(`settings-tab-${following.id}`)
                        ?.focus();
                    },
                  }
                : null;
              return (
                <section
                  className="settings-panel"
                  id={`settings-panel-${module.id}`}
                  role="tabpanel"
                  aria-labelledby={`settings-tab-${module.id}`}
                  key={module.id}
                  hidden={activeSection !== module.id}
                >
                  {module.id === "prompts" ? (
                    <>
                      <SectionHeader module={module} />
                      {promptContent({ next, previous, busy: saving || actionBusy })}
                    </>
                  ) : module.id === "mcp" ? (
                    <McpSection module={module} request={request} token={token} active={activeSection === module.id} busy={saving || loading || actionBusy} previous={previous} next={next} />
                  ) : module.id === "runtime" ? (
                    <RuntimeSection key={generation} module={module} data={data} save={save} saving={saving || loading || actionBusy} previous={previous} next={next} />
                  ) : module.id === "channel" ? (
                    <ChannelSection
                      key={generation}
                      module={module}
                      next={next}
                      previous={previous}
                      data={data}
                      save={save}
                      login={runtime?.weixin_login}
                      action={action}
                      saving={saving || loading}
                      actionBusy={actionBusy}
                    />
                  ) : (
                    <ProviderSection
                      key={generation}
                      module={module}
                      next={next}
                      previous={previous}
                      data={data}
                      save={save}
                      testProvider={testProvider}
                      testing={testing}
                      saving={saving || loading}
                    />
                  )}
                  {module.id !== "prompts" && runtimeContent}
                </section>
              );
            })}
          </div>
        </div>
      ) : loading ? (
        <div role="status">
          <Loading>正在读取配置…</Loading>
        </div>
      ) : (
        <section className="settings-panel settings-load-error" aria-label="配置载入失败">
          <SectionHeader module={{ id: "configuration", label: "配置读取" }} />
          <div className="settings-load-error-body" role="status">
            <span className="settings-channel-off-icon" aria-hidden="true"><Icon name="info" /></span>
            <div>
              <h3>暂时无法载入配置</h3>
              <p>请检查服务状态，再点击右上角刷新。</p>
            </div>
          </div>
          {issue && (
            <p className="settings-load-error-detail is-error" role="alert">
              {issue}
            </p>
          )}
        </section>
      )}
    </div>
  );
}
