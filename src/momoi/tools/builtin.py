import asyncio
import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import aiohttp

from ..models import ToolCall
from .contracts.builtin import BUILTIN_TOOL_SPECS


class BuiltinTools:
    def __init__(
        self,
        workspace: Path | None = None,
        *,
        private_roots: tuple[Path, ...] = (),
    ) -> None:
        self.workspace = (
            workspace.expanduser().resolve() if workspace is not None else Path.cwd()
        )
        self.private_roots = tuple(
            path.expanduser().resolve() for path in private_roots
        )

    def resolve_path(self, value: object) -> Path:
        path = Path(str(value or "")).expanduser()
        if not path.is_absolute():
            path = self.workspace / path
        path = path.resolve()
        self._ensure_public_path(path)
        return path

    def _ensure_public_path(self, path: Path) -> None:
        for root in self.private_roots:
            try:
                path.relative_to(root)
            except ValueError:
                continue
            raise PermissionError("path is runtime-private")

    def _is_public_path(self, path: Path) -> bool:
        try:
            self._ensure_public_path(path.resolve())
        except PermissionError:
            return False
        return True

    @staticmethod
    def has_tool(name: str) -> bool:
        return any(spec["name"] == name for spec in BUILTIN_TOOL_SPECS)

    @staticmethod
    def capability(call: ToolCall) -> str:
        if call.name in {"read_file", "list_dir", "sleep"}:
            return "read"
        if call.name in {
            "write_file",
            "apply_patch",
            "makedirs",
            "move_file",
            "delete_file",
        }:
            return "write"
        if call.name == "curl":
            method = str(call.arguments.get("method", "GET")).upper()
            return "read" if method in {"GET", "HEAD", "OPTIONS"} else "external_effect"
        return "external_effect"

    async def execute(self, call: ToolCall) -> dict[str, Any]:
        try:
            if call.name == "curl":
                return await self._curl(call.arguments)
            if call.name == "read_file":
                return await asyncio.to_thread(self._read_file, call.arguments)
            if call.name == "list_dir":
                return await asyncio.to_thread(self._list_dir, call.arguments)
            if call.name == "write_file":
                return await asyncio.to_thread(self._write_file, call.arguments)
            if call.name == "apply_patch":
                return await asyncio.to_thread(self._apply_patch, call.arguments)
            if call.name == "makedirs":
                return await asyncio.to_thread(self._makedirs, call.arguments)
            if call.name == "move_file":
                return await asyncio.to_thread(self._move_file, call.arguments)
            if call.name == "delete_file":
                return await asyncio.to_thread(self._delete_file, call.arguments)
            if call.name == "sleep":
                seconds = min(3600.0, max(0.0, float(call.arguments.get("seconds", 0))))
                await asyncio.sleep(seconds)
                return {"ok": True, "slept_seconds": seconds}
            return {"ok": False, "error": "tool_not_allowed"}
        except Exception as error:
            return {
                "ok": False,
                "error": type(error).__name__,
                "message": str(error)[:1000],
            }

    @staticmethod
    async def _curl(arguments: dict[str, Any]) -> dict[str, Any]:
        url = str(arguments.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("url must use http or https")
        method = str(arguments.get("method", "GET")).upper()
        headers = {
            str(key): str(value)
            for key, value in (arguments.get("headers") or {}).items()
        }
        body = arguments.get("body")
        json_body = arguments.get("json")
        if body is not None and json_body is not None:
            raise ValueError("body and json are mutually exclusive")
        timeout = aiohttp.ClientTimeout(
            total=min(120.0, max(0.1, float(arguments.get("timeout_seconds", 20))))
        )
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(
                method,
                url,
                headers=headers,
                params=arguments.get("params"),
                data=body,
                json=json_body,
                allow_redirects=bool(arguments.get("allow_redirects", True)),
                ssl=bool(arguments.get("verify_tls", True)),
            ) as response:
                try:
                    raw = await response.content.readexactly(200_001)
                except asyncio.IncompleteReadError as error:
                    raw = error.partial
                return {
                    "ok": True,
                    "status": response.status,
                    "url": str(response.url),
                    "headers": dict(response.headers),
                    "body": raw[:200_000].decode(
                        response.charset or "utf-8", errors="replace"
                    ),
                    "truncated": len(raw) > 200_000,
                }

    def _read_file(self, arguments: dict[str, Any]) -> dict[str, Any]:
        path = self.resolve_path(arguments.get("path"))
        if path.stat().st_size > 2_000_000:
            raise ValueError("file exceeds 2 MB read limit")
        content = path.read_text(encoding="utf-8")
        lines = content.splitlines(keepends=True)
        requested_offset = arguments.get("content_offset")
        if requested_offset is None:
            start = max(1, int(arguments.get("start_line", 1)))
            content_offset = sum(len(line) for line in lines[: start - 1])
        else:
            content_offset = max(0, int(requested_offset))
            if content_offset > len(content):
                raise ValueError("content_offset exceeds file length")
            start = content.count("\n", 0, content_offset) + 1
        limit = min(4000, max(1, int(arguments.get("max_lines", 1000))))
        remaining_lines = content[content_offset:].splitlines(keepends=True)
        selected = "".join(remaining_lines[:limit])
        char_truncated = len(selected) > 200_000
        selected = selected[:200_000]
        next_content_offset = content_offset + len(selected)
        has_more = next_content_offset < len(content)
        if selected:
            end_line = start + selected.count("\n")
            if selected.endswith("\n"):
                end_line -= 1
        else:
            end_line = start - 1
        return {
            "ok": True,
            "path": str(path),
            "start_line": start,
            "end_line": end_line,
            "total_lines": len(lines),
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
            "content_offset": content_offset,
            "next_content_offset": next_content_offset if has_more else None,
            "content": selected,
            "truncated": char_truncated or has_more,
        }

    def _list_dir(self, arguments: dict[str, Any]) -> dict[str, Any]:
        path = self.resolve_path(arguments.get("path"))
        if not path.exists():
            raise FileNotFoundError(f"directory does not exist: {path}")
        if not path.is_dir():
            raise NotADirectoryError(f"not a directory: {path}")
        include_hidden = bool(arguments.get("include_hidden", False))
        limit = min(2000, max(1, int(arguments.get("max_entries", 200))))
        children = sorted(
            path.iterdir(),
            key=lambda item: (not item.is_dir(), item.name.casefold()),
        )
        children = [item for item in children if self._is_public_path(item)]
        if not include_hidden:
            children = [item for item in children if not item.name.startswith(".")]
        truncated = len(children) > limit
        entries: list[dict[str, object]] = []
        for item in children[:limit]:
            try:
                if item.is_symlink():
                    kind = "symlink"
                elif item.is_dir():
                    kind = "dir"
                else:
                    kind = "file"
                entry: dict[str, object] = {"name": item.name, "type": kind}
                if kind == "file":
                    entry["size"] = item.stat().st_size
                entries.append(entry)
            except OSError:
                entries.append({"name": item.name, "type": "unknown"})
        return {
            "ok": True,
            "path": str(path),
            "entries": entries,
            "count": len(entries),
            "truncated": truncated,
        }

    def _write_file(self, arguments: dict[str, Any]) -> dict[str, Any]:
        path = self.resolve_path(arguments.get("path"))
        content = str(arguments.get("content") or "")
        if len(content.encode()) > 2_000_000:
            raise ValueError("content exceeds 2 MB write limit")
        if arguments.get("create_parents"):
            path.parent.mkdir(parents=True, exist_ok=True)
        if not path.parent.is_dir():
            raise FileNotFoundError(f"parent directory does not exist: {path.parent}")
        expected = arguments.get("expected_sha256")
        if expected is not None:
            current = (
                hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""
            )
            if current != str(expected):
                raise ValueError("expected_sha256 does not match current file")
        mode = path.stat().st_mode if path.exists() else None
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=path.parent, delete=False
            ) as file:
                temporary = Path(file.name)
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            if mode is not None:
                os.chmod(temporary, mode)
            os.replace(temporary, path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
        return {
            "ok": True,
            "path": str(path),
            "bytes": len(content.encode()),
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
        }

    def _apply_patch(self, arguments: dict[str, Any]) -> dict[str, Any]:
        patch = str(arguments.get("patch") or "")
        if not patch.strip():
            raise ValueError("patch is required")
        if len(patch.encode()) > 500_000:
            raise ValueError("patch exceeds 500 KB limit")
        cwd = self.resolve_path(arguments.get("cwd"))
        if not cwd.is_dir():
            raise FileNotFoundError(f"cwd does not exist: {cwd}")
        stripped = patch.strip()
        if stripped.startswith("*** Begin Patch"):
            if not stripped.endswith("*** End Patch"):
                raise ValueError("structured patch is missing *** End Patch")
            patch = (
                stripped.removeprefix("*** Begin Patch")
                .removesuffix("*** End Patch")
                .strip()
            )
            if not patch.startswith(("diff --git ", "--- ")):
                return self._apply_structured_patch(cwd, patch)
            patch += "\n"
        command = [
            "git",
            "-C",
            str(cwd),
            "apply",
            "--recount",
            "--whitespace=nowarn",
            "-",
        ]
        check = subprocess.run(
            [*command[:4], "--check", *command[4:]],
            input=patch,
            text=True,
            capture_output=True,
            timeout=30,
        )
        if check.returncode:
            raise ValueError(
                (check.stderr or check.stdout or "patch check failed").strip()
            )
        applied = subprocess.run(
            command,
            input=patch,
            text=True,
            capture_output=True,
            timeout=30,
        )
        if applied.returncode:
            raise RuntimeError(
                (applied.stderr or applied.stdout or "patch failed").strip()
            )
        return {"ok": True, "cwd": str(cwd)}

    def _makedirs(self, arguments: dict[str, Any]) -> dict[str, Any]:
        path = self.resolve_path(arguments.get("path"))
        existed = path.exists()
        if existed and not path.is_dir():
            raise FileExistsError(f"path exists and is not a directory: {path}")
        path.mkdir(parents=True, exist_ok=True)
        return {"ok": True, "path": str(path), "created": not existed}

    def _move_file(self, arguments: dict[str, Any]) -> dict[str, Any]:
        source = self.resolve_path(arguments.get("source"))
        destination = self.resolve_path(arguments.get("destination"))
        if not source.exists():
            raise FileNotFoundError(f"source file does not exist: {source}")
        if not source.is_file():
            raise IsADirectoryError(f"source is not a file: {source}")
        if destination.exists():
            raise FileExistsError(f"destination already exists: {destination}")
        if not destination.parent.is_dir():
            raise FileNotFoundError(
                f"destination parent directory does not exist: {destination.parent}"
            )
        size = source.stat().st_size
        shutil.move(str(source), str(destination))
        return {
            "ok": True,
            "source": str(source),
            "destination": str(destination),
            "bytes": size,
        }

    def _delete_file(self, arguments: dict[str, Any]) -> dict[str, Any]:
        path = self.resolve_path(arguments.get("path"))
        if not path.exists():
            raise FileNotFoundError(f"file does not exist: {path}")
        if not path.is_file():
            raise IsADirectoryError(f"path is not a file: {path}")
        size = path.stat().st_size
        path.unlink()
        return {"ok": True, "path": str(path), "bytes": size}

    def _apply_structured_patch(self, cwd: Path, patch: str) -> dict[str, Any]:
        lines = patch.splitlines()
        index = 0
        changed: list[str] = []
        while index < len(lines):
            header = lines[index]
            if not header.startswith("*** Update File: "):
                raise ValueError("structured patch currently requires *** Update File")
            raw_path = header.removeprefix("*** Update File: ").strip()
            path = Path(raw_path).expanduser()
            if not path.is_absolute():
                path = cwd / path
            path = path.resolve()
            self._ensure_public_path(path)
            content = path.read_text(encoding="utf-8")
            index += 1
            hunks: list[list[str]] = []
            current: list[str] = []
            while index < len(lines) and not lines[index].startswith("*** "):
                line = lines[index]
                if line.startswith("@@"):
                    if current:
                        hunks.append(current)
                        current = []
                elif line[:1] in {" ", "+", "-"}:
                    current.append(line)
                else:
                    raise ValueError("invalid structured patch line")
                index += 1
            if current:
                hunks.append(current)
            if not hunks:
                raise ValueError("structured patch has no hunks")
            for hunk in hunks:
                old = "\n".join(
                    line[1:] for line in hunk if line.startswith((" ", "-"))
                )
                new = "\n".join(
                    line[1:] for line in hunk if line.startswith((" ", "+"))
                )
                matches = [
                    (old + "\n", new + "\n"),
                    (old, new),
                ]
                selected = next(
                    (
                        pair
                        for pair in matches
                        if pair[0] and content.count(pair[0]) == 1
                    ),
                    None,
                )
                if selected is None:
                    raise ValueError(
                        f"structured patch context is not unique in {path}"
                    )
                content = content.replace(selected[0], selected[1], 1)
            result = self._write_file({"path": str(path), "content": content})
            changed.append(str(result["path"]))
        return {
            "ok": True,
            "cwd": str(cwd),
            "files": changed,
            "format": "structured",
        }
