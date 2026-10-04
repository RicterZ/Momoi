"""Generate an Ed25519 release keypair once; refuse accidental key rotation."""
import hashlib
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

root = Path(__file__).resolve().parents[2]
directory = root / "desktop/Momoi.Desktop/keys"
directory.mkdir(parents=True, exist_ok=True)
private_path = directory / "update-private.pem"
public_path = directory / "update-public.bin"
if private_path.exists() or public_path.exists():
    raise SystemExit("An update key already exists. Refusing to replace it.")
key = Ed25519PrivateKey.generate()
private_bytes = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
public_bytes = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
descriptor = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "wb") as stream:
    stream.write(private_bytes)
public_path.write_bytes(public_bytes)
print("Created Ed25519 signing keypair. Public-key SHA-256: " + hashlib.sha256(public_bytes).hexdigest())
