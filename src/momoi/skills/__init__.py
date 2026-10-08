"""Standard SKILL.md folders installed and discovered within the workspace."""

import asyncio
import re
import shutil
import tempfile
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from ..config.workspace import _create
from ..tools.process import run_process


def skill_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value) or len(value) > 64:
        raise ValueError("skill name must be 1–64 lowercase letters, digits or single hyphens")
    return value


def read_skill(directory):
    entry = directory / "SKILL.md"
    if entry.is_symlink():
        raise ValueError("SKILL.md must not be a symbolic link")
    content = entry.read_text(encoding="utf-8-sig")
    lines = content.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError("SKILL.md requires YAML frontmatter with name and description")
    try:
        end = lines.index("---", 1)
        metadata = yaml.safe_load("\n".join(lines[1:end]))
    except (ValueError, yaml.YAMLError) as error:
        raise ValueError("invalid SKILL.md frontmatter") from error
    if not isinstance(metadata, dict):
        raise ValueError("SKILL.md frontmatter must be an object")
    name = skill_name(metadata.get("name"))
    description = metadata.get("description")
    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
        raise ValueError("skill description must be 1–1024 characters")
    return {"name": name, "description": " ".join(description.split()), "content": content}


class Skills:
    def __init__(self, workspace):
        self.root = workspace / "skills"

    def initialize(self):
        self.root.mkdir(parents=True, exist_ok=True)
        marker = self.root / ".momoi-builtins"
        if marker.exists():
            return
        for directory in files("momoi.skills").joinpath("builtin").iterdir():
            destination = self.root / directory.name
            if not destination.exists():
                _create(destination / "SKILL.md", directory.joinpath("SKILL.md").read_text(encoding="utf-8"))
        _create(marker, "Builtin skills initialized; existing skills are never overwritten.\n")

    def directory(self, name):
        directory = self.root / skill_name(name)
        if directory.is_symlink() or not directory.is_dir():
            raise FileNotFoundError(f"skill not installed: {name}")
        return directory

    @staticmethod
    def resources(directory):
        return sorted(path for path in directory.rglob("*") if path.is_file()
                      and not path.is_symlink() and path.resolve().is_relative_to(directory.resolve())
                      and ".git" not in path.relative_to(directory).parts)

    def catalog(self):
        self.initialize()
        items = []
        for directory in sorted(self.root.iterdir()):
            if not directory.is_dir() or directory.is_symlink() or directory.name.startswith("."):
                continue
            try:
                item = read_skill(directory)
                if item["name"] == directory.name:
                    items.append({"name": item["name"], "description": item["description"]})
            except (OSError, UnicodeError, ValueError):
                continue
        return items

    def load(self, name):
        self.initialize()
        directory = self.directory(name)
        item = read_skill(directory)
        if item["name"] != name:
            raise ValueError("skill name must match its installed directory")
        return {"ok": True, **item, "directory": str(directory.resolve()),
                "resources": [path.relative_to(directory).as_posix() for path in self.resources(directory)
                              if path.name != "SKILL.md"]}

    def install_directory(self, source):
        self.initialize()
        if not source.is_dir() or source.is_symlink():
            raise ValueError("source must be a skill directory containing SKILL.md")
        item = read_skill(source)
        destination = self.root / item["name"]
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(f"skill already installed: {item['name']}")
        if destination.resolve().is_relative_to(source.resolve()):
            raise ValueError("source cannot contain the installation directory")
        if any(path.is_symlink() for path in source.rglob("*")):
            raise ValueError("skill installation does not copy symbolic links")
        with tempfile.TemporaryDirectory(prefix=".install-", dir=self.root) as temporary:
            staged = Path(temporary) / item["name"]
            shutil.copytree(source, staged, ignore=shutil.ignore_patterns(".git"))
            staged.rename(destination)
        return {"ok": True, "name": item["name"], "description": item["description"], "directory": str(destination.resolve())}

    async def install(self, source, subdirectory=".", ref=None):
        if not isinstance(source, str) or not source.strip():
            raise ValueError("source must be a local directory or HTTPS Git repository")
        subdirectory = Path(subdirectory)
        if subdirectory.is_absolute() or ".." in subdirectory.parts:
            raise ValueError("subdirectory must remain inside the source")
        if source.startswith("https://"):
            url = urlsplit(source)
            if not url.netloc or url.username or url.password:
                raise ValueError("use an HTTPS Git URL without embedded credentials")
            with tempfile.TemporaryDirectory(prefix="momoi-skill-") as temporary:
                repository = Path(temporary) / "repository"
                argv = ["git", "clone", "--depth", "1"]
                if ref is not None:
                    if not isinstance(ref, str) or not ref.strip():
                        raise ValueError("ref must be a nonempty branch or tag")
                    argv.extend(["--branch", ref])
                argv.extend(["--", source, str(repository)])
                result = await run_process(argv, timeout=120)
                if result["exit_code"]:
                    return {"ok": False, "error": "skill_download_failed", "message": result["stderr_tail"]}
                target = repository / subdirectory
                if not target.resolve().is_relative_to(repository.resolve()):
                    raise ValueError("subdirectory must remain inside the source")
                return await asyncio.to_thread(self.install_directory, target)
        if ref is not None:
            raise ValueError("ref is only used for Git repositories")
        local = Path(source).expanduser()
        if not local.is_absolute():
            local = self.root.parent / local
        target = local / subdirectory
        if not target.resolve().is_relative_to(local.resolve()):
            raise ValueError("subdirectory must remain inside the source")
        return await asyncio.to_thread(self.install_directory, target)

    def uninstall(self, name):
        self.initialize()
        directory = self.directory(name)
        shutil.rmtree(directory)
        return {"ok": True, "name": name, "uninstalled": True}
