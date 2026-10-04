"""Sign latest.json using the native shell's local Ed25519 release key."""
import argparse
import base64
import hashlib
import json
import time
import zipfile
from pathlib import Path
from urllib.parse import urlparse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[2]
KEYS = ROOT / "desktop/Momoi.Desktop/keys"


def sign_latest(archive: Path, url: str, output: Path):
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username:
        raise ValueError("download URL must use HTTPS")
    private = serialization.load_pem_private_key((KEYS / "update-private.pem").read_bytes(), password=None)
    if not isinstance(private, Ed25519PrivateKey):
        raise ValueError("expected an Ed25519 key")
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    if public != (KEYS / "update-public.bin").read_bytes():
        raise ValueError("private key does not match the public key compiled into the shell")
    with zipfile.ZipFile(archive) as zip_file:
        release = json.loads(zip_file.read("release.json"))
    with archive.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    payload = {
        "format_version": 1, "version": release["version"], "release_id": release["release_id"],
        "runtime_id": release["runtime_id"], "url": url, "sha256": checksum,
        "size": archive.stat().st_size, "published_at": int(time.time()),
    }
    signed = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    signature = private.sign(signed)
    private.public_key().verify(signature, signed)
    envelope = {"signed": base64.b64encode(signed).decode("ascii"), "signature": base64.b64encode(signature).decode("ascii")}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(envelope, indent=2) + "\n", encoding="utf-8")
    print(f"Signed {output}; version={payload['version']} release={payload['release_id']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--download-url", required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/windows/releases/latest.json")
    args = parser.parse_args()
    sign_latest(args.archive, args.download_url, args.output)
