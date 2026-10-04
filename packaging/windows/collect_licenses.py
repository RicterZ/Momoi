"""Copy installed Python package license notices into the distribution."""
import argparse
import importlib.metadata
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=True)
packages = []
for distribution in importlib.metadata.distributions():
    name = distribution.metadata["Name"]
    packages.append({"name": name, "version": distribution.version, "license": distribution.metadata.get("License-Expression") or distribution.metadata.get("License", "")})
    for entry in distribution.files or ():
        if any(part.lower().startswith(("license", "copying", "notice")) for part in entry.parts):
            source = Path(distribution.locate_file(entry))
            if source.is_file():
                target = args.output / name / str(entry).replace("/", "_")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
(args.output / "python-packages.json").write_text(json.dumps(packages, indent=2, ensure_ascii=False), encoding="utf-8")
