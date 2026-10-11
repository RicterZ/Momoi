import json
import sqlite3
import zipfile
from dataclasses import replace
from pathlib import Path
import pytest

from momoi.config.manager import ConfigurationManager
from momoi.config.workspace import bootstrap
from momoi.dashboard.backup import export_archive, validate_archive, restore_archive
from momoi.storage import Store


def test_backup_roundtrip_and_exclusions(tmp_path):
    workspace = tmp_path / 'workspace'
    path = workspace / 'config.json'
    bootstrap(path)
    config = ConfigurationManager(path).validate()
    store = Store(config.database, workspace)
    try:
        soul = workspace / 'prompts/SOUL.md'
        soul.write_text('我始终是同一个人')
        (workspace / 'llm-dumps').mkdir()
        (workspace / 'llm-dumps/secret.json').write_text('not exported')
        creation = workspace / 'artifacts/故事草稿/chapter.bak.txt'
        creation.parent.mkdir(parents=True)
        creation.write_text('Momoi 的创作')
        workflow = workspace / 'workflows/custom.yaml'
        workflow.parent.mkdir(parents=True, exist_ok=True)
        workflow.write_text('name: custom-workflow\n')
        attachment = workspace / 'channel/napcat/files/received.txt'
        attachment.parent.mkdir(parents=True)
        attachment.write_text('渠道收到的文件')
        store.record_llm_call(created_at=123, model='test', metrics={'output': 100})
        out = tmp_path / 'out'; out.mkdir()
        archive = export_archive(config, out)
        with zipfile.ZipFile(archive) as z:
            assert 'prompts/SOUL.md' in z.namelist()
            assert 'artifacts/故事草稿/chapter.bak.txt' in z.namelist()
            assert 'workflows/custom.yaml' in z.namelist()
            assert not any(name.startswith('channel/') for name in z.namelist())
            assert 'providers.yaml' not in z.namelist()
            assert not any('llm-dumps' in name or 'thinking' in name for name in z.namelist())
        soul.write_text('后来修改的内容')
        creation.write_text('后来修改的创作')
        workflow.write_text('name: changed-workflow\n')
        store._db.execute('DELETE FROM llm_usage'); store._db.commit()
        extracted = validate_archive(archive, tmp_path / 'restore', store._db)
        restore_archive(extracted, config, store)
        assert soul.read_text() == '我始终是同一个人'
        assert creation.read_text() == 'Momoi 的创作'
        assert workflow.read_text() == 'name: custom-workflow\n'
        assert attachment.read_text() == '渠道收到的文件'
        assert store._db.execute('SELECT output_tokens FROM llm_usage').fetchone()[0] == 100
        assert store._db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    finally:
        store.close()


@pytest.mark.parametrize('name', ['../escape', '/absolute', 'prompts/../../escape', 'prompts/evil\\file', 'providers.yaml'])
def test_reject_unsafe_archive(tmp_path, name):
    archive = tmp_path / 'bad.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr(name, 'unsafe')
        z.writestr('manifest.json', '{}')
    with sqlite3.connect(':memory:') as db, pytest.raises(ValueError):
        validate_archive(archive, tmp_path / 'extracted', db)
    assert not (tmp_path / 'escape').exists()


def test_reject_changed_database_schema(tmp_path):
    path = tmp_path / 'workspace/config.json'; bootstrap(path)
    config = ConfigurationManager(path).validate()
    store = Store(config.database, config.workspace)
    try:
        out = tmp_path / 'out'; out.mkdir()
        archive = export_archive(config, out)
        store._db.execute('CREATE TABLE unexpected (data TEXT)')
        with pytest.raises(ValueError, match='结构不兼容'):
            validate_archive(archive, tmp_path / 'restore', store._db)
    finally:
        store.close()


def test_cancelled_backup_waits_for_snapshot_writer():
    import asyncio
    import threading
    from momoi.dashboard.backup_routes import finish_thread
    started, finish = threading.Event(), threading.Event()
    def writer():
        started.set()
        assert finish.wait(2)
    async def check():
        task = asyncio.create_task(finish_thread(writer))
        await asyncio.to_thread(started.wait)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(check())


@pytest.mark.parametrize("newer", [False, True])
def test_restore_database_versions(tmp_path, newer):
    from momoi.storage.core.migrations import SCHEMA_VERSION
    path = tmp_path / "workspace/config.json"
    bootstrap(path)
    config = ConfigurationManager(path).validate()
    store = Store(config.database, config.workspace)
    try:
        store.record_llm_call(created_at=123, model="test", metrics={"output": 100})
        if not newer:
            store._db.execute("ALTER TABLE reflection_memories DROP COLUMN triggers_json")
        store._db.execute(f"PRAGMA user_version={SCHEMA_VERSION + (1 if newer else -1)}")
        store._db.commit()
        out = tmp_path / "out"
        out.mkdir()
        archive = export_archive(config, out)
        store.close()
        config.database.unlink()
        store = Store(config.database, config.workspace)
        if newer:
            with pytest.raises(ValueError, match="请升级"):
                validate_archive(archive, tmp_path / "restore", store._db)
            assert store._db.execute("SELECT count(*) FROM llm_usage").fetchone()[0] == 0
        else:
            extracted = validate_archive(archive, tmp_path / "restore", store._db)
            restore_archive(extracted, config, store)
            assert store._db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            assert store._db.execute("SELECT output_tokens FROM llm_usage").fetchone()[0] == 100
            assert "triggers_json" in {row[1] for row in store._db.execute("PRAGMA table_info(reflection_memories)")}
    finally:
        store.close()


def test_legacy_channel_attachments_are_not_restored(tmp_path):
    import hashlib
    path = tmp_path / 'workspace/config.json'
    bootstrap(path)
    config = ConfigurationManager(path).validate()
    store = Store(config.database, config.workspace)
    try:
        out = tmp_path / 'out'
        out.mkdir()
        archive = export_archive(config, out)
        with zipfile.ZipFile(archive) as source:
            files = {name: source.read(name) for name in source.namelist()}
        manifest = json.loads(files['manifest.json'])
        name = 'channel/napcat/files/old.txt'
        files[name] = b'legacy attachment'
        manifest['files'][name] = hashlib.sha256(files[name]).hexdigest()
        files['manifest.json'] = json.dumps(manifest).encode()
        with zipfile.ZipFile(archive, 'w') as target:
            for name, content in files.items():
                target.writestr(name, content)
        extracted = validate_archive(archive, tmp_path / 'restore', store._db)
        restore_archive(extracted, config, store)
        assert not (config.workspace / 'channel/napcat/files/old.txt').exists()
    finally:
        store.close()
