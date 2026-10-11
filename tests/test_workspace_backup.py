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
        store.record_llm_call(created_at=123, model='test', metrics={'output': 100})
        out = tmp_path / 'out'; out.mkdir()
        archive = export_archive(config, out)
        with zipfile.ZipFile(archive) as z:
            assert 'prompts/SOUL.md' in z.namelist()
            assert 'providers.yaml' not in z.namelist()
            assert not any('llm-dumps' in name or 'thinking' in name for name in z.namelist())
        soul.write_text('后来修改的内容')
        store._db.execute('DELETE FROM llm_usage'); store._db.commit()
        extracted = validate_archive(archive, tmp_path / 'restore', store._db)
        restore_archive(extracted, config, store)
        assert soul.read_text() == '我始终是同一个人'
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
        with pytest.raises(ValueError, match='版本'):
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
