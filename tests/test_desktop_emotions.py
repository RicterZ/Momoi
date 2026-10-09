import json
from importlib.resources import files
from pathlib import Path

from momoi_desktop.emotions import seed_emotions
from momoi.storage.store import Store


def test_desktop_seeds_bundled_emotions_and_preserves_deletions(tmp_path):
    database = tmp_path / "data/momoi.sqlite3"
    seed_emotions(tmp_path, database)
    store = Store(database, workspace=tmp_path)
    try:
        catalog = json.loads(files("momoi_desktop").joinpath("default_emotions/catalog.json").read_text(encoding="utf-8"))
        assert {item["slug"] for item in store.list_emotions()} == {"cry", "normal", "happy", "smug", "confused", "stunned"}
        for item in catalog:
            saved = store.emotion(item["slug"])
            assert saved["description"] == item["description"]
            asset = Path(saved["path"])
            assert asset.is_relative_to(tmp_path / "emotion")
            assert asset.read_bytes() == files("momoi_desktop").joinpath("default_emotions", item["file"]).read_bytes()
        store.delete_emotion("cry")
        happy = store.emotion("happy")
        store.add_emotion("happy", happy["path"], "用户修改")
    finally:
        store.close()
    seed_emotions(tmp_path, database)
    store = Store(database, workspace=tmp_path)
    try:
        assert store.emotion("cry") is None
        assert store.emotion("happy")["description"] == "用户修改"
    finally:
        store.close()


def test_desktop_keeps_existing_emotion_and_uses_custom_database(tmp_path):
    database = tmp_path / "custom/catalog.sqlite3"
    database.parent.mkdir()
    asset = tmp_path / "mine.png"
    asset.write_bytes(b"user-owned-asset")
    store = Store(database, workspace=tmp_path)
    try:
        store.add_emotion("cry", asset, "自定义哭泣")
    finally:
        store.close()
    seed_emotions(tmp_path, database)
    store = Store(database, workspace=tmp_path)
    try:
        assert len(store.list_emotions()) == 6
        assert store.emotion("cry")["description"] == "自定义哭泣"
        assert store.emotion("cry")["path"] == str(asset)
        assert asset.read_bytes() == b"user-owned-asset"
    finally:
        store.close()
