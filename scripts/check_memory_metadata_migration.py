#!/usr/bin/env python3
"""Rehearse only the metadata migration on disposable copies; source is read-only."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from momoi.storage.core.migrations import MIGRATIONS, _add_memory_metadata, apply_migrations


TABLES = ('memories', 'memory_evidence', 'memory_tombstones', 'semantic_spaces',
          'semantic_documents', 'semantic_dirty_sources', 'memory_operation_batches')


def fingerprint(database):
    result = {}
    for table in TABLES:
        columns = [row[1] for row in database.execute(f'PRAGMA table_info("{table}")')
                   if row[1] != 'meta_json']
        if not columns:
            raise ValueError(f'missing table: {table}')
        projection = ','.join('"' + name.replace('"', '""') + '"' for name in columns)
        digest, count = hashlib.sha256(), 0
        for row in database.execute(f'SELECT {projection} FROM "{table}" ORDER BY rowid'):
            digest.update(json.dumps(tuple(row), ensure_ascii=False,
                                     default=lambda value: value.hex()).encode())
            digest.update(b'\n')
            count += 1
        result[table] = {'count': count, 'sha256': digest.hexdigest()}
    return result


def check_integrity(database):
    if database.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
        raise ValueError('database integrity check failed')
    if database.execute('PRAGMA foreign_key_check').fetchall():
        raise ValueError('database foreign key check failed')


def audit(path: Path):
    previous_version = MIGRATIONS.index(_add_memory_metadata)
    with TemporaryDirectory(prefix='momoi-memory-migration-') as directory:
        backup_path = Path(directory) / 'backup.sqlite3'
        restored_path = Path(directory) / 'restored.sqlite3'
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as source:
            with closing(sqlite3.connect(backup_path)) as backup:
                source.backup(backup)
        with closing(sqlite3.connect(backup_path)) as backup:
            version = backup.execute('PRAGMA user_version').fetchone()[0]
            if version != previous_version:
                raise ValueError(f'expected pre-metadata schema {previous_version}, got {version}')
            if any(row[1] == 'meta_json' for row in backup.execute('PRAGMA table_info(memories)')):
                raise ValueError('source already has meta_json; inspect schema before migration')
            check_integrity(backup)
            before = fingerprint(backup)
            kinds = dict(backup.execute('SELECT kind, COUNT(*) FROM memories GROUP BY kind'))
            activations = dict(backup.execute('SELECT activation, COUNT(*) FROM memories GROUP BY activation'))
            pending = backup.execute(
                "SELECT COUNT(*) FROM memory_operation_batches WHERE state IN ('pending', 'running')"
            ).fetchone()[0]
            with closing(sqlite3.connect(restored_path)) as restored:
                backup.backup(restored)
        # Reopen the restored backup before testing migration and recovery.
        with closing(sqlite3.connect(restored_path)) as restored:
            if fingerprint(restored) != before:
                raise ValueError('backup restore differs from source snapshot')
            apply_migrations(restored)
            check_integrity(restored)
            if fingerprint(restored) != before:
                raise ValueError('migration changed existing memory or index data')
            if restored.execute("SELECT COUNT(*) FROM memories WHERE meta_json!='{}'").fetchone()[0]:
                raise ValueError('migration unexpectedly classified historical memories')
            migrated_version = restored.execute('PRAGMA user_version').fetchone()[0]
        with closing(sqlite3.connect(backup_path)) as backup:
            with closing(sqlite3.connect(restored_path)) as restored:
                backup.backup(restored)
        with closing(sqlite3.connect(restored_path)) as restored:
            check_integrity(restored)
            if fingerprint(restored) != before or restored.execute('PRAGMA user_version').fetchone()[0] != version:
                raise ValueError('rollback restore failed')
        return {'source_modified': False, 'from_version': version, 'to_version': migrated_version,
                'tables': before, 'kinds': kinds, 'activations': activations,
                'pending_operations': pending, 'integrity': 'ok', 'restore': 'ok'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    print(json.dumps(audit(parser.parse_args().database), ensure_ascii=False, indent=2))
