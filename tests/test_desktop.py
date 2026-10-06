import json
import sqlite3
from contextlib import closing
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
    with closing(sqlite3.connect(config.database)) as connection, connection:
        connection.execute("create table memories (text text)")
        connection.execute("insert into memories values ('桃井')")
    before = (workspace / "providers.yaml").read_bytes()
    backup = tmp_path / "backup"
    snapshot(workspace, backup)
    prepare_workspace(workspace, "http://127.0.0.1:19002/v1/embeddings")
    with closing(sqlite3.connect(config.database)) as connection, connection:
        connection.execute("drop table memories")
        connection.execute("pragma user_version = 9999")
    restore(backup)
    assert (workspace / "providers.yaml").read_bytes() == before
    with closing(sqlite3.connect(config.database)) as connection, connection:
        assert connection.execute("select text from memories").fetchone() == ("桃井",)
        assert connection.execute("pragma user_version").fetchone() == (0,)


def test_snapshot_handles_database_created_by_failed_start(tmp_path):
    workspace = tmp_path / "workspace"
    prepare_workspace(workspace, "http://127.0.0.1:19001/v1/embeddings")
    config = ConfigurationManager(workspace / "config.json").dashboard_config()
    backup = tmp_path / "backup"
    snapshot(workspace, backup)
    config.database.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(config.database)) as connection, connection:
        connection.execute("create table partial (id integer)")
    restore(backup)
    assert not config.database.exists()


def test_mcp_runtime_uses_writable_user_caches_and_private_toolchain(tmp_path):
    from momoi.desktop.mcp_runtime import prepare_mcp_environment
    install, workspace = tmp_path / "Program Files" / "Momoi", tmp_path / "user" / "Momoi"
    env = {"PATH": "existing"}
    prepare_mcp_environment(install, workspace, env)
    assert env["MOMOI_NODE"] == str(install / "runtime/node/node.exe")
    assert env["PATH"].startswith(str(install / "runtime/node"))
    assert env["PATH"].endswith("existing")
    for name in ("UV_CACHE_DIR", "UV_TOOL_DIR", "UV_TOOL_BIN_DIR", "UV_PYTHON_INSTALL_DIR", "npm_config_cache", "npm_config_prefix"):
        assert Path(env[name]).is_dir()
        assert Path(env[name]).is_relative_to(workspace)
    bootstrap(workspace / "config.json")
    path = workspace / "mcp.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {"mcpServers": {}}
    custom = {"mcpServers": {"custom": {"command": "user-owned-command"}}}
    atomic_write(path, json.dumps(custom))
    before = path.read_bytes()
    prepare_mcp_environment(install, workspace, env)
    assert path.read_bytes() == before
    assert "MOMOI_BRAVE_MCP" not in env


def test_desktop_prompt_defaults_match_examples(tmp_path):
    from importlib.resources import files

    prepare_workspace(tmp_path, "http://127.0.0.1:19001/v1/embeddings")
    examples = Path(__file__).resolve().parents[1] / "config.example/prompts"
    for name in ("SOUL", "PLANNER", "REPLYER"):
        expected = (examples / f"{name}.md").read_text(encoding="utf-8")
        assert expected.strip()
        assert files("momoi.desktop").joinpath(f"default_prompts/{name}.md").read_text(encoding="utf-8") == expected
        assert (tmp_path / f"prompts/{name}.md").read_text(encoding="utf-8") == expected


def test_desktop_repairs_empty_prompts_at_custom_path(tmp_path):
    prepare_workspace(tmp_path, "http://127.0.0.1:19001/v1/embeddings")
    config_path = tmp_path / "config.json"
    app = json.loads(config_path.read_text(encoding="utf-8"))
    app["context"]["soul_prompt"] = "custom/SOUL.md"
    atomic_write(config_path, json.dumps(app))
    atomic_write(tmp_path / "custom/SOUL.md", "我的自定义人格\n")
    atomic_write(tmp_path / "custom/PLANNER.md", "\ufeff  \n")
    prepare_workspace(tmp_path, "http://127.0.0.1:19001/v1/embeddings")
    assert (tmp_path / "custom/SOUL.md").read_text(encoding="utf-8") == "我的自定义人格\n"
    examples = Path(__file__).resolve().parents[1] / "config.example/prompts"
    for name in ("PLANNER", "REPLYER"):
        assert (tmp_path / f"custom/{name}.md").read_text(encoding="utf-8") == (examples / f"{name}.md").read_text(encoding="utf-8")
    atomic_write(tmp_path / "custom/REPLYER.md", "用户自定义回复风格")
    prepare_workspace(tmp_path, "http://127.0.0.1:19001/v1/embeddings")
    assert (tmp_path / "custom/REPLYER.md").read_text(encoding="utf-8") == "用户自定义回复风格"
