# 安装 MCP 工具

MCP 安装工作流现为 Momoi 内置的标准 skill：[mcp-install/SKILL.md](../src/momoi/skills/builtin/mcp-install/SKILL.md)。

首次使用 skill 工具时，它会被复制到 `<workspace>/skills/mcp-install/`，不覆盖已有文件。
根据 system 中的 Skill 索引，用 `skill_load` 加载 `mcp-install`。
安装步骤、Windows/Linux 环境、固定工具目录和最后的 `mcp_reload` 验证均在 skill 中维护。
