# 使用 Skill 工作流

Skill 使用标准目录格式，存放在 `<workspace>/skills/<name>/`。入口 `SKILL.md` 包含
YAML frontmatter 的 `name`、`description` 和 Markdown 指引，支持附带 `references/`、
`scripts/`、`assets/` 等资源。安装和加载不会执行其中的脚本。

Skill 名称、描述和正文不会预先加入 system 或 tool index。Momoi 默认只暴露 `skill_search` 和
`skill_load`，配合简短使用指引让 agent 主动按任务搜索；选定 skill 后，完整指引作为工具结果进入当前
对话，按需读取其他资源。加载 skill 不改变执行权限；安装 MCP 依赖仍需开启命令执行。

## 搜索和加载

调用 `skill_search`：

```json
{"query": "安装 MCP", "limit": 5}
```

搜索扫描 `SKILL.md` 的完整内容和 skill 内其他 Markdown 文档，包括引用文档。匹配
按相关性排序，返回名称和最多 200 字符的简短描述，不返回正文片段或整个目录索引。
不匹配脚本代码和二进制资源。每次搜索读取当前文件，新安装或手工修改后立即生效。

调用 `skill_load`：

```json
{"name": "mcp-install"}
```

返回完整 `SKILL.md`、绝对基准目录和资源的相对路径。相对引用以该目录为基准，
不是当前 Shell 工作目录。长结果可通过 `read_tool_result` 继续读取，再按需通过
文件工具或 `exec` 读取引用文档、使用资源或运行脚本。

## 安装

`skill_install` 默认不暴露，先用 `tool_search` 查找，再用 `tool_enable` 加载。

从工作区内本地目录安装：

```json
{"source": "downloads/paper-reading"}
```

从 Git 仓库的指定目录安装：

```json
{
  "source": "https://github.com/anthropics/skills.git",
  "subdirectory": "skills/pdf",
  "ref": "main"
}
```

远程安装需要可用的 `git`，不需要开启通用命令执行工具；只下载文件，不执行安装
脚本。Windows 未安装 Git 时，可先把目录下载到本地，再按本地方式安装。仓库 URL
应指向 Git 仓库本身，GitHub 的 `/tree/...` 页面 URL 需拆成仓库、ref 和 subdirectory。
`ref` 可省略，使用仓库默认分支；它接受分支或标签。

安装校验标准 frontmatter，以其中的 `name` 命名安装目录，保留引用、脚本和其他
资源，不复制 `.git`。已有同名 skill 不覆盖，安装失败不留下半份 skill。符号链接
不复制；仓库子目录必须位于仓库内。

也可以直接把标准 skill 文件夹放进工作区 `skills/`。名称应与文件夹名称相同：
小写字母、数字、单个连字符，最多 64 字符。description 必填，最多 1024 字符；
其他标准元数据保留。格式无效的 skill 不参与搜索，直接加载时返回错误。

## 卸载

`skill_uninstall` 默认不暴露，同样先通过 `tool_search` 和 `tool_enable` 加载。

调用 `skill_uninstall`：

```json
{"name": "paper-reading"}
```

删除该 skill 的整个目录。不会删除它曾安装的 MCP 服务、依赖或产出的文件。
已加载的指引仍保留在当前对话文本中，后续搜索和加载不再包含被卸载的 skill。
更新可先卸载再安装，也可通过已有文件工具修改目录。

## Momoi 内置 MCP 安装 Skill

[mcp-install](../src/momoi/skills/builtin/mcp-install/SKILL.md) 随 Momoi 包分发。
首次使用任一 skill 工具时复制到工作区 `skills/mcp-install/`，已有同名目录保持原样。
初始化标记 `skills/.momoi-builtins` 保证用户卸载后不会在下次启动时自动装回。

它包括 Windows 附带 uv/Node、Linux/Docker 环境、固定安装目录
`tools/mcp/<server-id>/`、能力 description、合并 MCP 配置，以及最后调用
`mcp_reload` 检查连接并加载新工具的流程。
