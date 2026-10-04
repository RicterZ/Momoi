"""Build a code-only ZIP and deterministic runtime compatibility identifier."""
import argparse
import hashlib
import json
import re
import subprocess
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def runtime_requirements() -> str:
    return subprocess.check_output([
        "uv", "export", "--locked", "--extra", "desktop", "--no-dev", "--no-default-groups",
        "--no-emit-project", "--no-header", "--no-annotate",
    ], cwd=ROOT, text=True, encoding="utf-8")


def build_release(output: Path, version: str):
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[.-][A-Za-z0-9.-]+)?", version):
        raise ValueError("invalid release version")
    if not (ROOT / "src/momoi/dashboard/static/index.html").is_file():
        raise FileNotFoundError("Run npm ci && npm run build first")
    requirements = runtime_requirements()
    from momoi.desktop.embedding import MODEL_REVISION
    components = {"requirements": requirements, "python_abi": "cp312-win_amd64", "model_revision": MODEL_REVISION}
    runtime_id = hashlib.sha256(json.dumps(components, sort_keys=True).encode("utf-8")).hexdigest()[:24]
    payload = {}
    for path in sorted((ROOT / "src/momoi").rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix not in (".pyc", ".pyo"):
            payload["app/" + path.relative_to(ROOT / "src").as_posix()] = path.read_bytes()
    payload["app/backend_entry.py"] = (ROOT / "packaging/windows/backend_entry.py").read_bytes()
    # Keep existing importlib.metadata version consumers working without installing code.
    payload[f"app/momoi-{version}.dist-info/METADATA"] = f"Metadata-Version: 2.3\nName: momoi\nVersion: {version}\n".encode()
    payload[f"app/momoi-{version}.dist-info/top_level.txt"] = b"momoi\n"
    hashes = {name: hashlib.sha256(content).hexdigest() for name, content in sorted(payload.items())}
    content_id = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()[:16]
    release_id = f"{version}-{content_id}"
    manifest = {"format_version": 1, "release_id": release_id, "version": version, "runtime_id": runtime_id, "files": hashes}
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"Momoi-Code-{release_id}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for name, content in sorted(payload.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            zip_file.writestr(info, content)
        zip_file.writestr("release.json", json.dumps(manifest, sort_keys=True, indent=2).encode())
    (output / "requirements.txt").write_text(requirements, encoding="utf-8", newline="\n")
    (output / "runtime.json").write_text(json.dumps({"format_version": 1, "runtime_id": runtime_id}, indent=2), encoding="utf-8")
    print(json.dumps({"archive": str(archive.resolve()), "release_id": release_id, "runtime_id": runtime_id, "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}))
    return archive, manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "dist/windows/releases")
    parser.add_argument("--version", default=tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"])
    args = parser.parse_args()
    build_release(args.output, args.version)
