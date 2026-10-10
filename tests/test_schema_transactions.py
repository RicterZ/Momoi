import sqlite3
import pytest
from momoi.storage.core import migrations
from momoi.storage.core.schema import initialize_schema, execute_schema
from momoi.memory.storage.transactions import transaction


def test_future_schema_is_rejected_before_creating_tables():
    db = sqlite3.connect(":memory:")
    db.execute(f"PRAGMA user_version={migrations.SCHEMA_VERSION + 1}")
    with pytest.raises(RuntimeError, match="newer than supported"):
        initialize_schema(db)
    assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
    db.close()


def test_failed_migration_rolls_back_ddl_data_and_version(monkeypatch):
    db = sqlite3.connect(":memory:")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("CREATE TABLE original (id INTEGER PRIMARY KEY)")
    db.execute("INSERT INTO original VALUES (1)")
    db.commit()
    def fail(connection):
        connection.execute("ALTER TABLE original ADD COLUMN extra TEXT")
        connection.execute("DELETE FROM original")
        connection.execute("CREATE TABLE partial (id INTEGER)")
        raise RuntimeError("failed upgrade")
    monkeypatch.setattr(migrations, "MIGRATIONS", (fail,))
    with pytest.raises(RuntimeError, match="failed upgrade"):
        migrations.apply_migrations(db)
    assert db.execute("PRAGMA user_version").fetchone()[0] == 0
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert len(db.execute("PRAGMA table_info(original)").fetchall()) == 1
    assert db.execute("SELECT * FROM original").fetchall() == [(1,)]
    assert not db.execute("SELECT name FROM sqlite_master WHERE name='partial'").fetchall()
    def succeed(connection):
        connection.execute("ALTER TABLE original ADD COLUMN extra TEXT")
    monkeypatch.setattr(migrations, "MIGRATIONS", (succeed,))
    migrations.apply_migrations(db)
    migrations.apply_migrations(db)
    assert db.execute("PRAGMA user_version").fetchone()[0] == 1
    db.close()


def test_bootstrap_script_rolls_back_and_preserves_trigger_bodies():
    db = sqlite3.connect(":memory:")
    script = """CREATE TABLE items (id INTEGER);
CREATE TRIGGER copy_item AFTER INSERT ON items WHEN NEW.id=1
BEGIN
  INSERT INTO items VALUES (2);
END;
"""
    with pytest.raises(sqlite3.OperationalError):
        with transaction(db):
            execute_schema(db, script + "INVALID SQL;\n")
    assert db.execute("SELECT name FROM sqlite_master").fetchall() == []
    with transaction(db):
        execute_schema(db, script)
        db.execute("INSERT INTO items VALUES (1)")
    assert db.execute("SELECT * FROM items").fetchall() == [(1,), (2,)]
    db.close()


def test_outbox_turn_index_is_added_to_existing_database_without_changing_rows(tmp_path):
    with sqlite3.connect(tmp_path / "existing.sqlite3") as db:
        initialize_schema(db)
        assert [row[2] for row in db.execute("PRAGMA index_info(outbox_turn_state)")] == ["turn_id", "state"]
        db.execute("DROP INDEX outbox_turn_state")
        db.executemany("INSERT INTO outbox(turn_id,dedupe_key,text,state) VALUES (?,?,?,?)", [
            ("old", "old", "已发送", "sent"),
            ("cancelled", "cancelled", "已取消", "superseded"),
            ("pending", "pending", "待发送", "pending"),
        ])
        db.commit()
        before = db.execute("SELECT * FROM outbox ORDER BY id").fetchall()
        initialize_schema(db)
        initialize_schema(db)
        assert db.execute("SELECT * FROM outbox ORDER BY id").fetchall() == before
        sql = "SELECT 1 FROM outbox WHERE turn_id=? AND state='superseded'"
        plan = db.execute("EXPLAIN QUERY PLAN " + sql, ("cancelled",)).fetchall()
        assert any("USING COVERING INDEX outbox_turn_state" in row[3] for row in plan)
        assert db.execute(sql, ("cancelled",)).fetchall() == [(1,)]
        assert db.execute(sql, ("old",)).fetchall() == []
