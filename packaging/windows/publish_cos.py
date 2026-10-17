"""Publish verified code ZIPs and Ed25519 latest to Momoi's fixed COS origin."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
import zipfile

import httpx

from sign_latest import sign_latest

BUCKET = "momoi-1253047877"
ENDPOINT = "cos.ap-guangzhou.myqcloud.com"
ORIGIN = "https://" + BUCKET + "." + ENDPOINT
PREFIX = "windows"
LATEST_KEY = PREFIX + "/latest.json"


def verify_archive(archive):
    if not 0 < archive.stat().st_size <= 64 * 1024 * 1024:
        raise ValueError("Code archive exceeds the shell's download limit")
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Duplicate code archive paths")
        for name in names:
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
                raise ValueError("Unsafe code archive path")
        release = json.loads(bundle.read("release.json"))
        if release.get("format_version") != 1 or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9.-]{0,100}", release.get("release_id", "")):
            raise ValueError("Invalid release manifest")
        if set(names) - {"release.json"} != set(release["files"]):
            raise ValueError("Code archive file list differs from release manifest")
        for name, expected in release["files"].items():
            if hashlib.sha256(bundle.read(name)).hexdigest() != expected:
                raise ValueError("Code archive content hash mismatch: " + name)
    return release


def upload(local, key, *, mutable=False):
    metadata = ("Cache-Control:no-cache, no-store, must-revalidate#Content-Type:" + ("application/json" if Path(local).suffix == ".json" else "application/octet-stream")
                if mutable else "Cache-Control:public, max-age=31536000, immutable")
    logs = tempfile.mkdtemp(prefix="momoi-cos-", dir="/tmp" if Path("/tmp").is_dir() else None)
    subprocess.run(["coscli", "cp", str(local), f"cos://{BUCKET}/{key}",
                    "--endpoint", ENDPOINT, "--disable-log", "--acl", "public-read",
                    "--log-path", logs, "--process-log-path", logs, "--fail-output-path", logs,
                    "--meta", metadata], check=True)


def verify_remote(client, key, local):
    with local.open("rb") as stream:
        expected = hashlib.file_digest(stream, "sha256").hexdigest()
    digest = hashlib.sha256()
    size = 0
    with client.stream("GET", ORIGIN + "/" + key, headers={"Cache-Control": "no-cache"}) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            digest.update(chunk)
            size += len(chunk)
    if size != local.stat().st_size or digest.hexdigest() != expected:
        raise ValueError("Published object differs from local file: " + key)


def sync_catalog(client, version, directory):
    """Keep old desktop shells' catalog/version gate compatible with code updates."""
    import base64
    from urllib.parse import urlsplit
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from sign_latest import KEYS
    from publish_catalog import sign

    response = client.get(ORIGIN + "/windows/catalog.json", headers={"Cache-Control": "no-cache"})
    response.raise_for_status()
    envelope = response.json()
    raw = base64.b64decode(envelope["signed"], validate=True)
    Ed25519PublicKey.from_public_bytes((KEYS / "update-public.bin").read_bytes()).verify(
        base64.b64decode(envelope["signature"], validate=True), raw)
    catalog = json.loads(raw)
    if catalog.get("format_version") != 1:
        raise ValueError("Unsupported update catalog")
    if catalog["version"] == version:
        return
    for name in ("shell", "napcat", "asr"):
        artifact = catalog.get(name)
        if artifact is None and name == "asr":
            continue
        url = artifact["url"]
        if not url.startswith(ORIGIN + "/windows/") or urlsplit(url).netloc != urlsplit(ORIGIN).netloc:
            raise ValueError("Unexpected catalog artifact origin")
        digest, size = hashlib.sha256(), 0
        with client.stream("GET", url) as download:
            download.raise_for_status()
            for chunk in download.iter_bytes():
                digest.update(chunk)
                size += len(chunk)
        if size != artifact["size"] or digest.hexdigest() != artifact["sha256"]:
            raise ValueError("Catalog artifact checksum mismatch: " + name)
    # This is the release version, not a claim that every component was rebuilt.
    catalog["version"] = version
    output = Path(directory) / "catalog.json"
    output.write_text(json.dumps(sign(catalog), indent=2) + "\n", encoding="utf-8")
    upload(output, "windows/catalog.json", mutable=True)
    verify_remote(client, "windows/catalog.json", output)


def publish(archive, *, installer=None):
    release = verify_archive(archive)
    code_key = PREFIX + "/releases/Momoi-Code-" + release["release_id"] + ".zip"
    with tempfile.TemporaryDirectory(prefix="momoi-cos-publish-") as directory:
        signed = Path(directory) / "latest.json"
        sign_latest(archive, ORIGIN + "/" + code_key, signed)
        with httpx.Client(timeout=120, follow_redirects=False) as client:
            upload(archive, code_key)
            verify_remote(client, code_key, archive)
            if installer is not None:
                if not re.fullmatch(r"Momoi-Setup-[A-Za-z0-9.-]+-x64\.exe", installer.name):
                    raise ValueError("Unexpected installer filename")
                installer_key = PREFIX + "/installers/" + installer.name
                upload(installer, installer_key)
                verify_remote(client, installer_key, installer)
            sync_catalog(client, release["version"], directory)
            # Advance discovery only after every referenced download is readable and verified.
            upload(signed, LATEST_KEY, mutable=True)
            verify_remote(client, LATEST_KEY, signed)
    print(json.dumps({"version": release["version"], "release_id": release["release_id"],
                      "latest": ORIGIN + "/" + LATEST_KEY, "code": ORIGIN + "/" + code_key}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--installer", type=Path)
    args = parser.parse_args()
    publish(args.archive, installer=args.installer)
