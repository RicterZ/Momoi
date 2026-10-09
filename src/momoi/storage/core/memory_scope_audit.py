"""Preflight and rehearse scope migration on temporary copies, never the source."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from .memory_scope_migration import add_memory_scope, scope_preflight
from .migrations import MIGRATIONS, migration_transaction
def check_integrity(database):
    if database.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
        raise ValueError('database integrity check failed')
    if database.execute('PRAGMA foreign_key_check').fetchall():
        raise ValueError('database foreign key check failed')


def digest(database):
    return hashlib.sha256('\n'.join(database.iterdump()).encode()).hexdigest()


def canonical(records):
    return sorted(json.dumps(row, sort_keys=True, default=lambda value: value.hex()) for row in records)


def rows(database, table):
    cursor = database.execute(f'SELECT * FROM "{table}"')
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor]


def audit(path: Path):
    previous = MIGRATIONS.index(add_memory_scope)
    with TemporaryDirectory(prefix='momoi-scope-migration-') as directory:
        backup_path, work_path = [Path(directory) / name for name in ('backup.sqlite3', 'work.sqlite3')]
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as source:
            with closing(sqlite3.connect(backup_path)) as backup:
                source.backup(backup)
        with closing(sqlite3.connect(backup_path)) as backup:
            version = backup.execute('PRAGMA user_version').fetchone()[0]
            if version != previous:
                raise ValueError(f'expected schema {previous}, got {version}')
            if 'scope_key' in {row[1] for row in backup.execute('PRAGMA table_info(memories)')}:
                raise ValueError('source already has scope_key; inspect schema before migration')
            check_integrity(backup)
            report = scope_preflight(backup)
            result = {'from_version': version, 'to_version': version + 1,
                      'source_modified': False, 'preflight': report}
            if not report['ready']:
                return result
            before = digest(backup)
            # Only memory identity fields and affected index invalidations may change.
            tables = [row[0] for row in backup.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            expected = {table: rows(backup, table) for table in tables}
            mapping = {item['id']: item for item in report['memories']}
            for row in expected['memories']:
                row['scope_key'], row['key'] = mapping[row['id']]['scope'], mapping[row['id']]['key']
            mapping = {(item['kind'], item['old_key']): item for item in report['tombstones']}
            for row in expected['memory_tombstones']:
                converted = mapping[(row['kind'], row['key'])]
                row['scope_key'], row['key'] = converted['scope'], converted['key']
            with closing(sqlite3.connect(work_path)) as work:
                backup.backup(work)
        with closing(sqlite3.connect(work_path)) as work:
            assert digest(work) == before
            with migration_transaction(work):
                add_memory_scope(work)
                work.execute(f'PRAGMA user_version={version + 1}')
            check_integrity(work)
            for table, original in expected.items():
                if table != 'semantic_dirty_sources' and canonical(rows(work, table)) != canonical(original):
                    raise ValueError(f'unexpected data change: {table}')
            dirty_before = {(row['source_type'], row['source_id']) for row in expected['semantic_dirty_sources']}
            dirty_after = {(row['source_type'], row['source_id']) for row in rows(work, 'semantic_dirty_sources')}
            required = {('confirmed_memory', str(row['id'])) for row in report['memories'] if row['scope']}
            if dirty_after != dirty_before | required:
                raise ValueError('incorrect index invalidation')
            migrated = digest(work)
            with migration_transaction(work):
                add_memory_scope(work)
            if digest(work) != migrated:
                raise ValueError('migration is not idempotent')
        with closing(sqlite3.connect(backup_path)) as backup:
            with closing(sqlite3.connect(work_path)) as work:
                backup.backup(work)
        with closing(sqlite3.connect(work_path)) as work:
            check_integrity(work)
            if digest(work) != before or work.execute('PRAGMA user_version').fetchone()[0] != version:
                raise ValueError('backup restoration failed')
        return {**result, 'integrity': 'ok', 'repeat': 'ok', 'restore': 'ok',
                'memory_count': len(expected['memories']),
                'tombstone_count': len(expected['memory_tombstones'])}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    args = parser.parse_args()
    result = audit(args.database)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result['preflight']['ready'] else 2)
