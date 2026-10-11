"""Portable relationship archives; runtime diagnostics and credentials stay local."""
import hashlib
import json
import re
import shutil
import sqlite3
import stat
import zipfile
from contextlib import closing
from pathlib import Path, PurePosixPath

from ..storage.core.schema import initialize_schema
from ..storage.core.migrations import SCHEMA_VERSION

FORMAT = "momoi-backup"
MAX_BYTES = 2 * 1024**3
DIRECTORIES = ("prompts", "emotion", "artifacts")
# Older archives may contain channel attachments; validate but do not restore them.
ARCHIVE_DIRECTORIES = (*DIRECTORIES, "channel/napcat/files", "channel/weixin/media")


def schema(db):
    objects = {}
    for kind, name, sql in db.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
    ):
        tokens = tuple(re.findall(r"'[^']*(?:''[^']*)*'|\"[^\"]*\"|`[^`]*`|\[[^\]]*\]|\w+|[^\s]", sql))
        if kind == "table":
            # ALTER TABLE appends columns; compare definitions without column order.
            start = tokens.index("(")
            depth, clauses, clause = 0, [], []
            for token in tokens[start + 1:-1]:
                if token == "," and depth == 0:
                    clauses.append(tuple(clause))
                    clause = []
                    continue
                depth += (token == "(") - (token == ")")
                clause.append(token)
            clauses.append(tuple(clause))
            objects[kind, name] = (tokens[:start], tuple(sorted(clauses)))
        else:
            objects[kind, name] = tokens
    return objects


def sanitize(db):
    with db:
        # Conversation messages are authoritative; execution exchanges are disposable.
        db.execute("DELETE FROM turn_journal")
        db.execute("DELETE FROM tool_audit")
        db.execute("DELETE FROM llm_request_metrics")
        db.execute("DELETE FROM context_plans")
        db.execute("DELETE FROM transcript_enabled_tools")
        db.execute("DELETE FROM replyer_history_windows")
        db.execute("UPDATE outbox SET state='superseded' WHERE state IN ('pending','sending')")
        db.execute("UPDATE task_plans SET status='paused' WHERE status IN ('running','ready')")
        db.execute("UPDATE task_plans SET context_json=NULL")
        db.execute("UPDATE turns SET state='cancelled', failure_reason='backup_snapshot' WHERE state='running'")
    db.execute("VACUUM")


def export_archive(config, directory):
    root = Path(directory)
    database = root / "momoi.sqlite3"
    with closing(sqlite3.connect(f"{config.database.as_uri()}?mode=ro", uri=True)) as source, closing(sqlite3.connect(database)) as target:
        source.backup(target)
        sanitize(target)
        version = target.execute("PRAGMA user_version").fetchone()[0]
    files = {"data/momoi.sqlite3": database}
    for name in DIRECTORIES:
        folder = config.workspace / name
        if folder.is_symlink():
            continue
        for path in folder.rglob("*") if folder.exists() else ():
            if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(folder.resolve()) and (name != "prompts" or (".bak" not in path.name and ".before" not in path.name)):
                files[path.relative_to(config.workspace).as_posix()] = path
    for name, path in (("SOUL.md", config.soul_prompt_path), ("HEARTBEAT.md", config.heartbeat_prompt_path), ("PLANNER.md", config.soul_prompt_path.parent / "PLANNER.md"), ("REPLYER.md", config.soul_prompt_path.parent / "REPLYER.md")):
        if path is not None and path.is_file() and not path.is_symlink():
            files[f"prompts/{name}"] = path
    if sum(p.stat().st_size for p in files.values()) > MAX_BYTES:
        raise ValueError("备份超过 2 GiB，请先整理素材")
    manifest = {"format": FORMAT, "version": 1, "schema_version": version, "workspace": str(config.workspace), "files": {}}
    archive = root / "momoi-backup.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for name, path in files.items():
            digest = hashlib.sha256()
            with path.open("rb") as stream, output.open(name, "w", force_zip64=True) as dest:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
                    dest.write(chunk)
            manifest["files"][name] = digest.hexdigest()
        output.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
    return archive


def validate_archive(archive, directory, current_db):
    root = Path(directory)
    with zipfile.ZipFile(archive) as source:
        entries = source.infolist()
        names = [entry.filename for entry in entries]
        if len(names) != len(set(names)) or len(entries) > 10000 or sum(e.file_size for e in entries) > MAX_BYTES:
            raise ValueError("备份文件过大或包含重复路径")
        for entry in entries:
            path = PurePosixPath(entry.filename)
            if (entry.filename != path.as_posix() or path.is_absolute() or ".." in path.parts or "\\" in entry.filename
                    or ":" in entry.filename or entry.is_dir()
                    or stat.S_ISLNK(entry.external_attr >> 16)
                    or not (entry.filename in {"manifest.json", "data/momoi.sqlite3"}
                            or any(entry.filename.startswith(prefix + "/") for prefix in ARCHIVE_DIRECTORIES))):
                raise ValueError("备份包含不允许的路径")
        if source.getinfo("manifest.json").file_size > 1024 * 1024:
            raise ValueError("备份清单过大")
        manifest = json.loads(source.read("manifest.json"))
        if (not isinstance(manifest, dict) or manifest.get("format") != FORMAT or manifest.get("version") != 1
                or not isinstance(manifest.get("files"), dict)
                or set(manifest["files"]) != set(names) - {"manifest.json"}
                or "data/momoi.sqlite3" not in manifest["files"]):
            raise ValueError("不是有效的 Momoi 备份")
        for name, digest in manifest["files"].items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            checksum = hashlib.sha256()
            with source.open(name) as stream, target.open("wb") as dest:
                while chunk := stream.read(1024 * 1024):
                    checksum.update(chunk)
                    dest.write(chunk)
            if checksum.hexdigest() != digest:
                raise ValueError("备份校验失败")
    with closing(sqlite3.connect(root / "data/momoi.sqlite3")) as db:
        db.execute("PRAGMA trusted_schema=OFF")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise ValueError("备份数据库版本较新，请升级 Momoi 后恢复")
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("备份数据库损坏")
        # Upgrade only the extracted copy; the running database stays untouched.
        if version < SCHEMA_VERSION:
            try:
                initialize_schema(db)
            except (RuntimeError, ValueError, sqlite3.Error) as error:
                raise ValueError(f"备份数据库迁移失败：{error}") from error
        if schema(db) != schema(current_db):
            raise ValueError("备份数据库结构不兼容，无法恢复")
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("备份数据库损坏")
        sanitize(db)
    (root / "validated-manifest.json").write_text(json.dumps(manifest))
    return root


def restore_archive(root, config, store):
    rollback = root / "rollback.sqlite3"
    with closing(sqlite3.connect(rollback)) as db:
        store._db.backup(db)
    originals = root / "originals"
    originals.mkdir()
    changed = []
    extra_prompts = []
    try:
        for name in DIRECTORIES:
            source, target = root / name, config.workspace / name
            if source.exists():
                if target.is_symlink() or not target.resolve().is_relative_to(config.workspace.resolve()):
                    raise ValueError("恢复目标不能是符号链接")
                if target.exists():
                    shutil.copytree(target, originals / name)
                changed.append(name)
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(source, target)
        for name, target in (("SOUL.md", config.soul_prompt_path), ("HEARTBEAT.md", config.heartbeat_prompt_path), ("PLANNER.md", config.soul_prompt_path.parent / "PLANNER.md"), ("REPLYER.md", config.soul_prompt_path.parent / "REPLYER.md")):
            source = root / "prompts" / name
            if not source.exists() or target == config.workspace / "prompts" / name:
                continue
            if target.is_symlink():
                raise ValueError("提示词恢复目标不能是符号链接")
            old = originals / ("custom-" + name)
            if target.exists():
                shutil.copy2(target, old)
            extra_prompts.append((target, old))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        with closing(sqlite3.connect(root / "data/momoi.sqlite3")) as db:
            old_workspace = json.loads((root / "validated-manifest.json").read_text()).get("workspace")
            if isinstance(old_workspace, str) and old_workspace:
                with db:
                    db.execute("UPDATE emotions SET path=replace(path, ?, ?)", (old_workspace, str(config.workspace)))
                    db.execute("UPDATE outbox SET media_path=replace(media_path, ?, ?)", (old_workspace, str(config.workspace)))
                    db.execute("UPDATE events SET payload_json=replace(payload_json, ?, ?)", (json.dumps(old_workspace)[1:-1], json.dumps(str(config.workspace))[1:-1]))
            db.backup(store._db)
        store.request_metrics._dashboard_cache.clear()
    except BaseException:
        with closing(sqlite3.connect(rollback)) as db:
            db.backup(store._db)
        for target, old in reversed(extra_prompts):
            if old.exists():
                shutil.copy2(old, target)
            elif target.exists():
                target.unlink()
        for name in changed:
            target = config.workspace / name
            if target.exists():
                shutil.rmtree(target)
            if (originals / name).exists():
                shutil.copytree(originals / name, target)
        raise
