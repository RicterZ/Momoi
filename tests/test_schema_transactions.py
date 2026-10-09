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
