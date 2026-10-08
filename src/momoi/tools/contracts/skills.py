"""Discover skills by document content, without a preloaded skill catalog."""

SKILL_TOOL_SPECS = [
    {
        "name": "skill_search",
        "description": "按任务关键词搜索工作区 skills 中 SKILL.md 及引用 Markdown 文档的内容，只返回 skill 名称和简短描述；选定后用 skill_load 加载。",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
            },
            "required": ["query"], "additionalProperties": False,
        },
    },
    {
        "name": "skill_load",
        "description": "按名称读取标准 SKILL.md 完整指引，返回绝对目录和资源列表；按需通过文件工具或 exec 读取引用文档、运行脚本。加载不会自动执行脚本。",
        "input_schema": {
            "type": "object", "properties": {"name": {"type": "string", "minLength": 1}},
            "required": ["name"], "additionalProperties": False,
        },
    },
    {
        "name": "skill_install",
        "description": "安装标准 skill 目录到工作区 skills/<name>，保留脚本、引用和资源。source 为本地目录或 HTTPS Git 仓库；可用 subdirectory 指定仓库内的 skill，用 ref 指定分支或标签。已有同名 skill 不覆盖。",
        "input_schema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "minLength": 1},
                "subdirectory": {"type": "string", "default": "."},
                "ref": {"type": "string", "minLength": 1},
            },
            "required": ["source"], "additionalProperties": False,
        },
    },
    {
        "name": "skill_uninstall",
        "description": "删除工作区 skills 中指定名称的整个 skill 目录（含脚本和资源），不会清理 skill 曾安装的 MCP 服务或依赖。已加载的文本仍保留在当前对话中。",
        "input_schema": {
            "type": "object", "properties": {"name": {"type": "string", "minLength": 1}},
            "required": ["name"], "additionalProperties": False,
        },
    },
]

SKILL_TOOL_POLICY = """### Skill 工作流

- 遇到需要专门操作步骤的任务，先用 skill_search 按任务内容搜索，再用 skill_load 加载匹配指引。Skill 清单不预先注入，请主动搜索；没有结果时按现有能力处理。
- 安装或卸载 Skill 时，通过 tool_search 查找管理工具，再用 tool_enable 加载参数。
- 加载结果中的目录是该 skill 的基准目录；按需读取相对引用。Skill 不会自动运行脚本，也不改变工具权限或用户授权。长结果先用 read_tool_result 读完必要指引。
"""
