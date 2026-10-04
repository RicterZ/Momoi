# Windows 桌面版与签名更新

桌面版使用 .NET 10 WPF + WebView2，托盘提供打开面板、配置目录、检查更新和退出。
关闭窗口隐藏到托盘；再次运行激活原实例。主体和 BGE 使用私有 Python 3.12，
FastEmbed CPU 编码保持 `BAAI/bge-small-zh-v1.5`、512 维与原有 calibration profile。

## 分发架构

安装目录：

```text
Momoi.exe                       # 自包含 .NET 外壳，无需另装 .NET
runtime/python/                 # 私有 Python 和锁定依赖，不放业务代码
runtime/runtime.json            # 运行组件兼容标识
models/bge-small-zh-v1.5/        # 固定 revision 的离线模型
releases/bundled/app/            # 首次安装附带的代码、前端、资源
releases/bundled/release.json
licenses/
```

用户数据统一放在 `<安装目录>\data`，托盘“配置目录”打开此处，升级与卸载均保留：

```text
config.json, providers.yaml, data/, prompts/, logs/, webview/
releases/<release_id>/           # 已校验的不可变代码版本
active-release.json             # 当前版本原子指针
update-backups/                 # 更新失败恢复所需配置和 SQLite 快照
```

业务代码、提示词、SQL 与构建后的 Dashboard 都进入代码 ZIP；Python、依赖、
模型和外壳不进入 ZIP。正常代码更新不重装组件。ZIP 可在后台运行时下载，
应用时短暂重启 Python；不原地替换已载入模块。依赖、Python ABI 或模型 revision
变化会改变 runtime_id，此类更新要求新版安装包。

## 内置 COS 地址与签名

外壳只从 `desktop/Momoi.Update/UpdateSource.cs` 的 `LatestManifestUrl` 获取
`latest.json`。没有配置文件、环境变量或命令行地址覆盖。
**仓库尚未提供实际 COS 地址，当前常量为空；发布安装包前必须填入真实 HTTPS 地址。**
可以从托盘手动检查；每次打开程序也会在后台检查，发现更新后提示下载安装；用户确认后才下载代码 ZIP，验签并安装，然后重启后台和刷新面板。

密钥位于 `desktop/Momoi.Desktop/keys/`：

- `update-public.bin`：32 字节 Ed25519 公钥，提交并嵌入 .NET 程序。
- `update-private.pem`：PKCS#8 私钥，只在本地，git 已忽略，不打入安装包。

首次生成脚本拒绝覆盖已有密钥。私钥必须自行备份；换公钥需要升级外壳。
不在 GitHub Actions 中存放私钥，代码 ZIP 构建后在持有私钥的机器签名。

`latest.json` 格式为：

```json
{
  "signed": "<UTF-8 JSON payload 的 Base64>",
  "signature": "<Ed25519 签名的 Base64>"
}
```

签名覆盖原始 payload 字节，避免 Python/.NET 的 JSON 序列化差异。验签通过后
才读取版本、release_id、runtime_id、HTTPS ZIP 地址、SHA-256、长度和发布时间。
下载后核对 ZIP 长度与哈希，并逐文件校验 release.json、拒绝路径穿越、重复路径
和符号链接。COS/CDN 不需要持有私钥。使用不可变的版本 ZIP 地址，最后覆盖
latest.json，并设置 latest.json 为 `Cache-Control: no-cache`。

签名确保发布者身份与内容完整性；当前没有独立的在线时间戳/单调发布计数服务，
不能据此保证 CDN 永远返回最新清单。

## Windows x64 安装包构建

需要 Windows x64、uv、Node 24、.NET 10 SDK、Inno Setup 6。
开发工具只在构建机需要。首次构建会下载模型与 Microsoft 前置组件。

在仓库根目录运行：

```powershell
.\packaging\windows\build.ps1
```

只生成应用目录：

```powershell
.\packaging\windows\build.ps1 -SkipInstaller
```

构建脚本依次：

1. 安装锁定 Python 依赖，构建 Dashboard 与多尺寸 ICO。
2. 下载固定模型 revision，验证本地离线 512 维编码，生成模型哈希清单。
3. 生成代码 ZIP 和运行组件标识。
4. 发布自包含 .NET 外壳，复制独立 Python，按锁文件安装依赖（不安装主体代码）。
5. 验证私有环境的离线模型，以及中文/空格工作区、JWT、Dashboard、BGE 和退出。
6. 收集许可证，下载并验证 Microsoft 签名的 WebView2 与 VC++ 安装程序。
7. 用 Inno Setup 生成 `dist/windows/Momoi-Setup-<version>-x64.exe`。

安装包按机器安装并申请管理员权限，应用以普通用户运行。安装器为 `{app}\data`
授予普通用户修改权限，运行组件所在目录仍使用默认权限。程序启动时检查 data
写入权限；没有权限时显示修复提示。卸载不删除 data，备份时可复制整个 data 目录。内含离线 WebView2
与 VC++ 安装器，用户无需安装 Python、Node、Docker 或 .NET。
WebView2 后续由 Microsoft 的 Evergreen 机制维护。

`.github/workflows/windows.yml` 提供手动 Windows 构建及 artifact，默认不发布。
最终验收仍需干净 Windows 机器检查托盘、窗口、安装/卸载、升级和异常退出。

## 日常代码发布到 COS

不改依赖或模型时：

```bash
npm ci
npm run build
uv run --locked --extra desktop --group windows-build python packaging/windows/build_release.py --version 1.1.1
uv run --locked python packaging/windows/sign_latest.py \
  --archive dist/windows/releases/Momoi-Code-1.1.1-<content-id>.zip \
  --download-url 'https://<真实COS域名>/releases/Momoi-Code-1.1.1-<content-id>.zip'
```

先将 ZIP 上传到签名中指定的不可变 COS 地址，再将生成的 latest.json 上传到外壳
内置地址。日常发布只需 ZIP + latest.json，不需要重新构建外壳或安装包。
签名 ZIP 与清单中 runtime_id 必须与已安装运行组件一致。

应用前先正常停止后台，再备份配置与 SQLite。新版本启动失败时恢复原代码指针
与快照；原版本保留用于恢复。备份不自动清理，需根据磁盘情况人工管理。
不要在业务代码启动阶段执行无法由配置/SQLite 快照恢复的外部数据迁移。

## MCP 运行组件

安装包附带 Node 24（含 npm/npx）、uv/uvx，以及锁定的 Brave 官方 MCP Server。
它们位于 `runtime/node`、`runtime/uv`、`runtime/mcp`，不随业务代码 ZIP 改动。
后台只为自己的进程和 MCP 子进程设置 PATH，不改系统或用户 PATH。
uv、npm 缓存和工具安装目录放在 `<安装目录>\data\tool-cache`，
npm 全局安装前缀为 `<安装目录>\data\node-global`，避免写入 Program Files。

首次桌面启动在 mcp.json 中加入禁用的 Brave 示例，不覆盖用户已有同名配置。
在托盘点击“配置目录”，修改 mcp.json：填写 `BRAVE_API_KEY`，将 `disabled` 改为 false。
也可以通过 Dashboard 的 MCP 设置编辑。内置 Brave 用绝对路径 node 直接启动，
不执行 npx 下载。其他 npm/uvx MCP 首次安装仍需要联网；原生扩展、浏览器或
特定 Python 版本等依赖需按相应 Server 单独准备，内置工具链不承诺运行任意 Server。

MCP command、args、cwd 支持 `${ENV_VAR}` 展开；桌面设置 MOMOI_NODE、
MOMOI_BRAVE_MCP 为安装路径，用户不用硬编码路径。
运行组件版本固定在 `packaging/windows/components.json`，MCP 依赖固定在
`packaging/windows/mcp/package-lock.json`。这些变化要求新的运行组件安装包。
