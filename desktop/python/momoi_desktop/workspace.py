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
        ("AFFINITY", soul_path.parent / "AFFINITY.md"),
        ("IMAGE_RECOGNITION", soul_path.parent / "IMAGE_RECOGNITION.md"),
        ("REPLYER", soul_path.parent / "REPLYER.md"),
    ):
        if not path.exists() or (name not in {"AFFINITY", "IMAGE_RECOGNITION"} and not path.read_text(encoding="utf-8-sig").strip()):
            resource = (files("momoi").joinpath(f"prompts/{name.lower()}.md") if name in {"AFFINITY", "IMAGE_RECOGNITION"}
                        else files("momoi_desktop").joinpath(f"default_prompts/{name}.md"))
            if name not in {"AFFINITY", "IMAGE_RECOGNITION"} and not resource.is_file():
                # Source checkouts use the same examples copied into release packages.
                resource = Path(__file__).resolve().parents[3] / f"config.example/prompts/{name}.md"
            content = resource.read_text(encoding="utf-8")
            atomic_write(path, content)
    bootstrap(config_path)
    if fresh and (timezone := os.environ.get("MOMOI_DESKTOP_TIMEZONE")):
        ZoneInfo(timezone)
        initialized = json.loads(config_path.read_text(encoding="utf-8"))
        initialized["timezone"] = timezone
        atomic_write(config_path, json.dumps(initialized, ensure_ascii=False, indent=2) + "\n")
