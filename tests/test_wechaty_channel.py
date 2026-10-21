from pathlib import Path

import pytest

from momoi.channel.wechaty import WechatyConfig


def test_wechaty_config_uses_private_workspace_storage(tmp_path: Path) -> None:
    config = WechatyConfig.from_mapping({"owner_id": "wxid_owner"}, tmp_path)
    assert config.data_dir == tmp_path.resolve() / "channel" / "wechaty"
    assert config.owner_id == "wxid_owner"


@pytest.mark.parametrize("value", [None, {"unexpected": True}, {"owner_id": "bad\0id"}])
def test_wechaty_config_rejects_invalid_settings(tmp_path: Path, value) -> None:
    with pytest.raises(ValueError):
        WechatyConfig.from_mapping(value, tmp_path)
