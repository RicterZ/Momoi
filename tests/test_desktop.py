import json
import sqlite3
from pathlib import Path

import yaml

from momoi.config.manager import ConfigurationManager
from momoi.config.workspace import atomic_write, bootstrap
from momoi.desktop.workspace import prepare_workspace
from momoi.desktop.snapshot import snapshot, restore


def test_desktop_manages_only_local_default_embedding(tmp_path):
    prepare_workspace(tmp_path, "http://127.0.0.1:19001/v1/embeddings")
    path = tmp_path / "providers.yaml"
    catalog = yaml.safe_load(path.read_text())
    binding = catalog["bindings"]["embedding"]
    assert binding["options"]["endpoint"] == "http://127.0.0.1:19001/v1/embeddings"
    assert binding["options"]["model"] == "BAAI/bge-small-zh-v1.5"
    assert binding["options"]["dimensions"] == 512
    binding["enabled"] = False
    atomic_write(path, yaml.safe_dump(catalog))
    prepare_workspace(tmp_path, "http://127.0.0.1:19002/v1/embeddings")
    catalog = yaml.safe_load(path.read_text())
    assert catalog["bindings"]["embedding"]["options"]["endpoint"] == "http://127.0.0.1:19002/v1/embeddings"
    assert catalog["bindings"]["embedding"]["enabled"] is False
    catalog["bindings"]["embedding"]["options"]["endpoint"] = "https://custom.example/v1/embeddings"
    atomic_write(path, yaml.safe_dump(catalog))
    before = path.read_bytes()
    prepare_workspace(tmp_path, "http://127.0.0.1:19003/v1/embeddings")
    assert path.read_bytes() == before


def test_desktop_respects_custom_provider_path(tmp_path):
    bootstrap(tmp_path / "config.json")
    (tmp_path / "providers.yaml").rename(tmp_path / "custom.yaml")
    app = json.loads((tmp_path / "config.json").read_text())
    app["providers"] = "custom.yaml"
    atomic_write(tmp_path / "config.json", json.dumps(app))
    prepare_workspace(tmp_path, "http://127.0.0.1:19001/v1/embeddings")
    assert not (tmp_path / "providers.yaml").exists()
    assert yaml.safe_load((tmp_path / "custom.yaml").read_text())["bindings"]["embedding"]["options"]["endpoint"].startswith("http://127.0.0.1:19001/")


def test_failed_update_restores_database_and_configuration(tmp_path):
    workspace = tmp_path / "workspace"
    prepare_workspace(workspace, "http://127.0.0.1:19001/v1/embeddings")
    config = ConfigurationManager(workspace / "config.json").dashboard_config()
    config.database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(config.database) as connection:
        connection.execute("create table memories (text text)")
        connection.execute("insert into memories values ('桃井')")
    before = (workspace / "providers.yaml").read_bytes()
    backup = tmp_path / "backup"
    snapshot(workspace, backup)
    prepare_workspace(workspace, "http://127.0.0.1:19002/v1/embeddings")
    with sqlite3.connect(config.database) as connection:
        connection.execute("drop table memories")
        connection.execute("pragma user_version = 9999")
    restore(backup)
    assert (workspace / "providers.yaml").read_bytes() == before
    with sqlite3.connect(config.database) as connection:
        assert connection.execute("select text from memories").fetchone() == ("桃井",)
        assert connection.execute("pragma user_version").fetchone() == (0,)


def test_snapshot_handles_database_created_by_failed_start(tmp_path):
    workspace = tmp_path / "workspace"
    prepare_workspace(workspace, "http://127.0.0.1:19001/v1/embeddings")
    config = ConfigurationManager(workspace / "config.json").dashboard_config()
    backup = tmp_path / "backup"
    snapshot(workspace, backup)
    config.database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(config.database) as connection:
        connection.execute("create table partial (id integer)")
    restore(backup)
    assert not config.database.exists()
