"""Verify bundled MCP toolchains without shipping a default MCP server."""
import argparse
import json
import subprocess
from pathlib import Path


def check(node: Path, uv: Path):
    node = node.resolve()
    result = subprocess.check_output(
        [str(node), "-e", "console.log(JSON.stringify({version:process.version,arch:process.arch}))"],
        text=True, timeout=30,
    )
    info = json.loads(result)
    assert info["arch"] == "x64", info
    npm = node.parent / "node_modules/npm/bin/npm-cli.js"
    assert npm.is_file(), npm
    version = subprocess.check_output([str(node), str(npm), "--version"], text=True, timeout=30)
    assert version.strip(), version
    version = subprocess.check_output([str(uv.resolve()), "--version"], text=True, timeout=30)
    assert version.startswith("uv "), version
    print("Bundled Node/npm and uv MCP toolchains passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--node", type=Path, required=True)
    parser.add_argument("--uv", type=Path, required=True)
    args = parser.parse_args()
    check(args.node, args.uv)
