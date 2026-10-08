---
name: mcp-install
description: 安装、配置或修复 MCP 工具服务。检查 Windows 或 Linux/Docker 运行环境，在工作区安装依赖，填写能力描述并合并 mcp.json，最后重载连接、发现并使用新工具。
---

# 安装 MCP 工具

安装依赖需要开启
工具 → MCP 工具 → 命令执行。Windows 的 `exec` 使用 PowerShell，Linux 使用 Bash。
每个服务必须填写 `description`：说明提供什么工具、能做什么，供 tool index 检索。
旧配置（包括禁用条目）缺少此字段时，需要补齐后再重载。

## 工作区与安装目录

工作区是 `config.json` 所在目录，也是 `exec` 的默认工作目录。将所有 MCP 源码、
独立环境和服务数据放在 `<workspace>/tools/mcp/<server-id>/`；这是约定的安装位置，
不增加路径访问限制。复用已有安装，勿把第三方依赖装进 Momoi 自身的 Python 环境。

读取 `config.json` 中的 `tools.mcp_config` 确认实际配置文件路径；相对路径以工作区
为基准，默认 `mcp.json`。若它为 `null` 或空字符串，先启用 MCP 配置；此项属于运行
配置变更，可能重启聊天运行时，应在安装流程开始前完成。

## 1. 阅读安装说明、检测环境

先阅读目标项目的官方说明，确认包名、启动入口、需要的 Python/Node 版本和凭据。
MCP 没有通用安装 ZIP；根据 npm 包、Python 包、源码或远程地址选择安装方式。
可根据项目要求使用系统依赖、浏览器等，不限定只能安装某类工具。

Windows 桌面安装包已经提供 Node/npm/npx、uv/uvx 和 Python，通过 Momoi 子进程的
PATH 使用。不要替换系统 PATH；`MOMOI_NODE` 指向附带的 Node。可检查：

```powershell
Get-Command uv, uvx, node, npm, npx
uv --version
node --version
New-Item -ItemType Directory -Force tools/mcp | Out-Null
```

Linux 官方 Docker 镜像已提供 Python、uv、Node 和 npm；在运行 Momoi 的容器内安装，
工作区中的安装目录应在持久化挂载内。无需再次安装全局 uv/Node：

```bash
command -v uv uvx node npm npx
uv --version
node --version
mkdir -p tools/mcp
```

源码部署先检测这些命令，按实际需要补足环境。

## 2. 安装到服务目录

Python 服务优先在服务目录创建独立虚拟环境，使用 `uv pip install --python` 安装目标
包。下面以官方 Fetch 包为例；其他项目按其说明选入口和版本。

Linux / Docker：

```bash
mkdir -p tools/mcp/fetch
uv venv tools/mcp/fetch/.venv
uv pip install --python tools/mcp/fetch/.venv/bin/python mcp-server-fetch
```

Windows / PowerShell：

```powershell
New-Item -ItemType Directory -Force tools/mcp/fetch | Out-Null
uv venv tools/mcp/fetch/.venv
uv pip install --python tools/mcp/fetch/.venv/Scripts/python.exe mcp-server-fetch
```

npm 服务使用 `npm install --prefix <服务目录> <官方包名>`，按项目说明找到本地启动
入口，用 `node` 的绝对路径和入口路径配置。需要源码构建时，在同一服务目录 clone、
安装依赖和构建。优先记录实际安装版本，后续重载使用已安装的入口，避免每次启动
通过 npx/uvx 下载包。远程 Streamable HTTP 服务无需本地安装。

## 3. 合并 MCP 配置

读取当前 JSON，仅增加或更新目标条目，保留其他服务和未知字段，不覆盖整份配置。
写文件时使用 UTF-8 和临时文件原子替换，避免 watcher 读到写入一半的 JSON。
Windows 的路径可用 `/`；若使用反斜杠，需按 JSON 转义为 `\\`。

以下示例适用于从工作区运行的 Linux；写入实际配置时，将 `command` 和 `cwd` 转为
当前工作区下的绝对路径。Windows 对应的 Python 入口为 `.venv/Scripts/python.exe`。

```json
{
  "mcpServers": {
    "fetch": {
      "description": "提供网页抓取工具，将指定 URL 的正文提取为 Markdown，支持分页读取内容。",
      "command": "tools/mcp/fetch/.venv/bin/python",
      "args": ["-m", "mcp_server_fetch"],
      "cwd": "."
    }
  }
}
```

`description` 必填，长度 1–500 字符，描述真实能力，不写“已安装”或安装命令。
本地服务使用 `command`、`args`、`cwd`、`env`；远程服务使用 `url`、`headers`。
需要凭据时按项目要求填写，别将凭据放进 description 或公开日志。`${VARIABLE}`
引用的是 Momoi 进程环境，在一次 exec 中 export / 设置环境变量不会改变父进程环境。
不要凭空填写 `readOnlyTools`；需要限制工具时使用 `enabled_tools`。

## 4. 最后重载、确认可用

通过现有 `tool_search` 搜索 `mcp_reload`，用 `tool_enable` 加载后调用：

```json
{}
```

该工具读取配置文件、校验全部条目，等待在途 MCP 调用完成后重建 MCP 连接，不重启
聊天运行时。配置无效时返回 `invalid_mcp_config`，旧连接保持；某服务连接失败时返回
`mcp_connect_failed` 和逐服务结果，其他成功服务仍可使用。返回 `servers` 包含服务名、
能力说明、连接状态、可用工具名和错误类型。

仅 MCP 文件变化时，dashboard 的现有 watcher 也会自动重载；最后仍调用
`mcp_reload` 明确检查安装结果。当前对话会在下一轮更新工具目录，随后通过
`tool_search` / `tool_enable` 加载新工具，继续原任务。不能仅凭依赖安装或 JSON 写入
成功就报告工具可用。没有工具或筛选掉全部工具时，检查服务说明与 `enabled_tools`。
