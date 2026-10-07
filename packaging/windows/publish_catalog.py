"""Publish a verified shell + locked QQ pair catalog; advance discovery last."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import tempfile
import zipfile

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from publish_cos import ORIGIN, upload, verify_remote
from sign_latest import KEYS


def artifact(path, version, identity, key, kind):
    return {'id': identity, 'version': version, 'url': ORIGIN + '/' + key,
            'sha256': hashlib.file_digest(path.open('rb'), 'sha256').hexdigest(),
            'size': path.stat().st_size, 'kind': kind}


def sign(payload):
    private = serialization.load_pem_private_key((KEYS / 'update-private.pem').read_bytes(), password=None)
    if not isinstance(private, Ed25519PrivateKey):
        raise ValueError('Expected Ed25519 signing key')
    if private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw) != (KEYS / 'update-public.bin').read_bytes():
        raise ValueError('Key mismatch')
    raw = json.dumps(payload, separators=(',', ':'), sort_keys=True).encode()
    return {'signed': base64.b64encode(raw).decode(), 'signature': base64.b64encode(private.sign(raw)).decode()}


def publish(shell, napcat, version):
    with zipfile.ZipFile(shell) as bundle:
        manifest = json.loads(bundle.read('shell-manifest.json'))
        if manifest['version'] != version or set(bundle.namelist()) != {'shell-manifest.json', *('payload/' + name for name in manifest['files'])}:
            raise ValueError('Invalid shell file manifest')
        for name, expected in manifest['files'].items():
            if hashlib.sha256(bundle.read('payload/' + name)).hexdigest() != expected:
                raise ValueError('Shell checksum mismatch')
    pair = json.loads(napcat.with_suffix('.json').read_text())
    if pair['filename'] != napcat.name or pair['sha256'] != hashlib.file_digest(napcat.open('rb'), 'sha256').hexdigest() or pair['bytes'] != napcat.stat().st_size:
        raise ValueError('QQ pair installer checksum mismatch')
    shell_key = 'windows/shell/' + shell.name
    pair_key = 'windows/components/' + napcat.name
    payload = {'format_version': 1, 'version': version,
               'shell': artifact(shell, version, version + '-' + manifest['commit'][:7], shell_key, 'shell-zip'),
               'napcat': artifact(napcat, pair['version'], pair['pair_id'], pair_key, 'napcat-installer')}
    with tempfile.TemporaryDirectory() as directory:
        catalog = Path(directory) / 'catalog.json'
        catalog.write_text(json.dumps(sign(payload), indent=2) + '\n')
        with httpx.Client(timeout=300, follow_redirects=False) as client:
            for local, key in [(shell, shell_key), (napcat, pair_key), (napcat.with_suffix('.json'), pair_key[:-4] + '.json')]:
                upload(local, key)
                verify_remote(client, key, local)
            upload(catalog, 'windows/catalog.json', mutable=True)
            verify_remote(client, 'windows/catalog.json', catalog)
    print(json.dumps(payload, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--shell', type=Path, required=True)
    parser.add_argument('--napcat', type=Path, required=True)
    parser.add_argument('--version', required=True)
    args = parser.parse_args()
    publish(args.shell, args.napcat, args.version)
