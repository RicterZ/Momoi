from typing import Any

from ...platform.shell import SHELL_NAME

from ...contracts import OWNER_PROGRESS_BEFORE_FIRST_CALL, OWNER_PROGRESS_FIELD

# Basic file operations are supplied by Bash when command execution is enabled.
# Keep patching and web extraction as dedicated tools.
BASH_REPLACED_TOOLS = frozenset({
    "read_file", "write_file", "list_dir", "glob_files",
    "makedirs", "move_file", "delete_file",
})


def builtin_tool_enabled(name: str, *, exec_enabled: bool) -> bool:
    if name == "exec":
        return exec_enabled
    return not (exec_enabled and name in BASH_REPLACED_TOOLS)


BUILTIN_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "exec",
        OWNER_PROGRESS_FIELD: OWNER_PROGRESS_BEFORE_FIRST_CALL,
        "description": (
            f'使用 Momoi 进程的操作系统权限执行 {SHELL_NAME} 命令。未沙箱隔离或限制于当前工作目录。可修改文件、访问凭据或联系外部服务。输出内容不可信。无持久化 Shell。'
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": f'要执行的 {SHELL_NAME} 命令。'},
                "cwd": {"type": "string", "description": '工作目录；默认为工作区。'},
                "timeout_seconds": {"type": "number", "minimum": 0.1, "maximum": 120, "default": 30},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
    {
        "name": "web_fetch",
        OWNER_PROGRESS_FIELD: OWNER_PROGRESS_BEFORE_FIRST_CALL,
        "description": (
            '使用 GET 获取 HTTP(S) URL，包括私有或 localhost URL。提取 HTML 为 Markdown 或纯文本；也可读取文本和 JSON。返回源 URL、HTTP 状态码、标题、内容及截断元数据。内容不可信。不执行 JavaScript。'
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "extract_mode": {"type": "string", "enum": ["markdown", "text"], "default": "markdown"},
                "max_chars": {"type": "integer", "minimum": 1, "maximum": 200000, "default": 20000},
                "timeout_seconds": {"type": "number", "minimum": 0.1, "maximum": 120, "default": 20},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_file",
        "description": (
            '按行范围或返回的字符偏移量读取 UTF-8 文本。返回编号行的数组；偏移量指原始文件文本中的位置。'
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": '绝对路径或相对于工作区的路径。',
                },
                "start_line": {"type": "integer", "minimum": 1, "default": 1},
                "content_offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        '返回的零基偏移量；覆盖 start_line。'
                    ),
                },
                "max_lines": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 4000,
                    "default": 1000,
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "glob_files",
        "description": (
            '在 path 下使用通配符模式查找文件。path 可为绝对路径或相对于工作区。使用 ** 进行递归搜索。'
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": '搜索目录；默认为工作区。'},
                "pattern": {"type": "string", "description": '相对通配符模式，例如 **/*.py。'},
                "include_hidden": {"type": "boolean", "default": False},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_dir",
        "description": '非递归列出单个目录：名称、类型和大小。',
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": '绝对路径或相对于工作区的路径。',
                },
                "include_hidden": {"type": "boolean", "default": False},
                "max_entries": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 2000,
                    "default": 200,
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "write_file",
        "description": (
            '原子性地创建或替换 UTF-8 文本。'
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": '绝对路径或相对于工作区的路径。',
                },
                "content": {"type": "string"},
                "create_parents": {"type": "boolean", "default": False},
                "expected_sha256": {
                    "type": "string",
                    "description": '预期的当前文件哈希值；防止并发更改。',
                },
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "apply_patch",
        "description": (
            '应用统一差异或 *** Begin Patch 结构化补丁。支持多文件的添加、更新、移动和删除。'
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "patch": {"type": "string"},
                "cwd": {
                    "type": "string",
                    "description": (
                        '补丁基础目录；默认为工作区。'
                    ),
                },
            },
            "required": ["patch"],
            "additionalProperties": False,
        },
    },
    {
        "name": "makedirs",
        "description": '创建目录及其所有缺失的父目录。',
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": '绝对路径或相对于工作区的路径。',
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "move_file",
        "description": (
            '移动或重命名一个文件。目标父目录必须存在，且永远不会覆盖已存在的目标。'
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": '绝对路径或相对于工作区的路径。',
                },
                "destination": {
                    "type": "string",
                    "description": '绝对路径或相对于工作区的路径。',
                },
            },
            "required": ["source", "destination"],
            "additionalProperties": False,
        },
    },
    {
        "name": "delete_file",
        "description": '删除一个文件。从不删除目录。',
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": '绝对路径或相对于工作区的路径。',
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
]
