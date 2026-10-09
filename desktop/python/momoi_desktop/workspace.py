"""Seed desktop prompts and the initial system timezone."""

import json
import os
from zoneinfo import ZoneInfo
from importlib.resources import files
from pathlib import Path

from momoi.config.workspace import atomic_write, bootstrap


def prepare_workspace(workspace: Path) -> None:
    config_path = workspace / "config.json"
    fresh = not config_path.exists()
    app = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    soul_path = workspace / app.get("context", {}).get("soul_prompt", "prompts/SOUL.md")
    for name, path in (
        ("SOUL", soul_path),
        ("PLANNER", soul_path.parent / "PLANNER.md"),
        ("REPLYER", soul_path.parent / "REPLYER.md"),
    ):
        if not path.exists() or not path.read_text(encoding="utf-8-sig").strip():
            content = files("momoi_desktop").joinpath(f"default_prompts/{name}.md").read_text(encoding="utf-8")
            atomic_write(path, content)
    bootstrap(config_path)
    if fresh and (timezone := os.environ.get("MOMOI_DESKTOP_TIMEZONE")):
        ZoneInfo(timezone)
        initialized = json.loads(config_path.read_text(encoding="utf-8"))
        initialized["timezone"] = timezone
        atomic_write(config_path, json.dumps(initialized, ensure_ascii=False, indent=2) + "\n")
