"""Scope migration and read-only preflight; unknown identities block conversion."""
import json
import re
import time
from collections import Counter
from pathlib import Path

from ...memory.storage.records import MEMORY_KINDS


def split_legacy_key(key, activation=None):
    match = re.fullmatch(r'(goal\.[0-9a-f]{32}|heartbeat|webhook)\.([a-z0-9][a-z0-9_.-]*)', key)
    if activation == 'scoped' or (activation is None and key.startswith(('goal.', 'heartbeat.', 'webhook.'))):
        if not match:
            raise ValueError('unrecognized scoped key')
        scope, key = match.groups()
        return scope.replace('goal.', 'goal:', 1), key
    if key.startswith(('goal.', 'heartbeat.', 'webhook.')):
        raise ValueError('workflow prefix on global memory')
    return '', key


def scope_preflight(database):
    cursor = database.execute('SELECT * FROM memories')
    rows = [dict(zip([c[0] for c in cursor.description], row)) for row in cursor]
    cursor = database.execute('SELECT * FROM memory_tombstones')
    tombstones = [dict(zip([c[0] for c in cursor.description], row)) for row in cursor]
    issues, mappings, deleted = [], [], []
    existing = 'scope_key' in {row[1] for row in database.execute('PRAGMA table_info(memories)')}
    identities = {}
    for row in rows:
        try:
            scope, key = (row['scope_key'], row['key']) if existing else split_legacy_key(row['key'], row['activation'])
            if row['activation'] not in ('always', 'recall', 'scoped'):
                raise ValueError('unsupported activation')
            if row['kind'] not in MEMORY_KINDS:
                raise ValueError('unknown kind; explicit classification required')
            if (row['activation'] == 'scoped') != bool(scope):
                raise ValueError('activation and scope disagree')
            if 'scope' in json.loads(row['meta_json']):
                raise ValueError('scope already present in meta_json')
            identity = (scope, row['kind'], key)
            if row['superseded_by'] is None and (row['expires_at'] is None or row['expires_at'] > time.time()):
                if identity in identities:
                    raise ValueError(f'duplicate active identity with memory {identities[identity]}')
                identities[identity] = row['id']
            mappings.append({'id': row['id'], 'scope': scope, 'key': key, 'old_key': row['key']})
        except (ValueError, TypeError) as error:
            issues.append({'memory_id': row['id'], 'reason': str(error)})
    seen = set()
    for row in tombstones:
        try:
            scope, key = (row['scope_key'], row['key']) if existing else split_legacy_key(row['key'])
            if row['kind'] not in MEMORY_KINDS:
                raise ValueError('unknown tombstone kind')
            identity = (scope, row['kind'], key)
            if identity in seen:
                raise ValueError('duplicate tombstone identity')
            seen.add(identity)
            deleted.append({**row, 'scope': scope, 'key': key, 'old_key': row['key']})
        except ValueError as error:
            issues.append({'tombstone': [row['kind'], row['key']], 'reason': str(error)})
    pending = [row[0] for row in database.execute(
        "SELECT id FROM memory_operation_batches WHERE state IN ('pending','running')")]
    maintenance = [row[0] for row in database.execute(
        "SELECT id FROM turns WHERE workflow_kind='memory_maintenance' AND state IN ('running','needs_reconciliation')")]
    if pending or maintenance:
        issues.append({'reason': 'finish or explicitly cancel in-flight memory work before migration',
                       'operation_batches': pending, 'maintenance_turns': maintenance})
    return {'ready': not issues, 'issues': issues, 'memories': mappings, 'tombstones': deleted,
            'kinds': dict(Counter(row['kind'] for row in rows)),
            'activations': dict(Counter(row['activation'] for row in rows))}


def _install_scope_objects(database):
    database.execute('DROP INDEX IF EXISTS memories_active')
    database.execute('CREATE INDEX memories_active ON memories(scope_key,kind,key) WHERE superseded_by IS NULL')
    # Baseline schema remains usable before old databases have scope columns.
    script = Path(__file__).with_name('schema.sql').read_text()
    names = ['semantic_memories_update', 'semantic_tombstones_insert',
             'semantic_tombstones_update', 'semantic_tombstones_delete']
    for name in names:
        start = script.index('CREATE TRIGGER IF NOT EXISTS ' + name + '\n')
        statement = script[start:script.index('\nEND;', start) + 5]
        statement = statement.replace('AFTER UPDATE OF kind, key, content, activation, expires_at, superseded_by',
                                      'AFTER UPDATE OF scope_key, kind, key, content, activation, expires_at, superseded_by')
        statement = statement.replace('AFTER UPDATE OF kind, key ON memory_tombstones',
                                      'AFTER UPDATE OF scope_key, kind, key ON memory_tombstones')
        statement = statement.replace('kind=NEW.kind AND key=NEW.key', 'scope_key=NEW.scope_key AND kind=NEW.kind AND key=NEW.key')
        statement = statement.replace('kind=OLD.kind AND key=OLD.key', 'scope_key=OLD.scope_key AND kind=OLD.kind AND key=OLD.key')
        database.execute(f'DROP TRIGGER IF EXISTS {name}')
        database.execute(statement)


def add_memory_scope(database):
    if 'scope_key' in {row[1] for row in database.execute('PRAGMA table_info(memories)')}:
        primary = {row[1]: row[5] for row in database.execute('PRAGMA table_info(memory_tombstones)') if row[5]}
        if primary != {'scope_key': 1, 'kind': 2, 'key': 3}:
            raise ValueError('incomplete memory scope schema; inspect before migration')
        _install_scope_objects(database)
        return
    report = scope_preflight(database)
    if not report['ready']:
        raise ValueError('memory scope migration blocked: ' + json.dumps(report['issues'], ensure_ascii=False))
    database.execute("ALTER TABLE memories ADD COLUMN scope_key TEXT NOT NULL DEFAULT ''")
    for row in report['memories']:
        if row['scope']:
            database.execute('UPDATE memories SET scope_key=?,key=? WHERE id=?', (row['scope'], row['key'], row['id']))
    triggers = list(database.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND lower(sql) LIKE '%memory_tombstones%'"))
    for name, _ in triggers:
        database.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
    indexes = [row[0] for row in database.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name='memory_tombstones' AND type='index' AND sql IS NOT NULL")]
    database.execute("""CREATE TABLE memory_tombstones_scoped (
        scope_key TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL, key TEXT NOT NULL,
        source_event_id TEXT NOT NULL, evidence_quote TEXT NOT NULL, created_at REAL NOT NULL,
        PRIMARY KEY(scope_key,kind,key))""")
    for row in report['tombstones']:
        database.execute('INSERT INTO memory_tombstones_scoped VALUES (?,?,?,?,?,?)',
                         (row['scope'], row['kind'], row['key'], row['source_event_id'], row['evidence_quote'], row['created_at']))
    database.execute('DROP TABLE memory_tombstones')
    database.execute('ALTER TABLE memory_tombstones_scoped RENAME TO memory_tombstones')
    for statement in indexes:
        database.execute(statement)
    for _, statement in triggers:
        database.execute(statement)
    _install_scope_objects(database)
