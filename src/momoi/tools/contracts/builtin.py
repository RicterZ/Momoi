from typing import Any

from ...contracts import OWNER_PROGRESS_BEFORE_FIRST_CALL, OWNER_PROGRESS_FIELD

BUILTIN_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "curl",
        OWNER_PROGRESS_FIELD: OWNER_PROGRESS_BEFORE_FIRST_CALL,
        "description": (
            "Send HTTP(S), including private or localhost URLs. Returns status, "
            "headers, final URL, and untrusted body data."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "method": {
                    "type": "string",
                    "enum": [
                        "GET",
                        "HEAD",
                        "POST",
                        "PUT",
                        "PATCH",
                        "DELETE",
                        "OPTIONS",
                    ],
                    "default": "GET",
                },
                "headers": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                },
                "params": {"type": "object"},
                "body": {"type": "string"},
                "json": {},
                "timeout_seconds": {
                    "type": "number",
                    "minimum": 0.1,
                    "maximum": 120,
                    "default": 20,
                },
                "allow_redirects": {"type": "boolean", "default": True},
                "verify_tls": {"type": "boolean", "default": True},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read UTF-8 text by line range or returned character offset."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute or workspace-relative path.",
                },
                "start_line": {"type": "integer", "minimum": 1, "default": 1},
                "content_offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        "Returned zero-based offset; overrides start_line."
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
        "name": "list_dir",
        "description": "List one directory non-recursively: names, types, and sizes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute or workspace-relative path.",
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
            "Atomically create/replace UTF-8 text; expected_sha256 guards against "
            "concurrent changes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute or workspace-relative path.",
                },
                "content": {"type": "string"},
                "create_parents": {"type": "boolean", "default": False},
                "expected_sha256": {"type": "string"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "apply_patch",
        "description": (
            "Apply a unified diff or *** Begin Patch structured patch. Supports "
            "multi-file add, update, move, and delete."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "patch": {"type": "string"},
                "cwd": {
                    "type": "string",
                    "description": (
                        "Patch base directory; defaults to the workspace."
                    ),
                },
            },
            "required": ["patch"],
            "additionalProperties": False,
        },
    },
    {
        "name": "makedirs",
        "description": "Create a directory and any missing parent directories.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute or workspace-relative path.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "move_file",
        "description": (
            "Move or rename one file. The destination parent must exist, and an "
            "existing destination is never overwritten."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": "Absolute or workspace-relative path.",
                },
                "destination": {
                    "type": "string",
                    "description": "Absolute or workspace-relative path.",
                },
            },
            "required": ["source", "destination"],
            "additionalProperties": False,
        },
    },
    {
        "name": "delete_file",
        "description": "Delete one file. Directories are never deleted.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute or workspace-relative path.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sleep",
        "description": (
            "Wait briefly inside this Turn, then continue. Never use it across Turns "
            "or instead of a Goal."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "seconds": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 3600,
                }
            },
            "required": ["seconds"],
            "additionalProperties": False,
        },
    },
]
