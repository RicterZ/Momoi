"""Publish a signed, immutable core/QQ installation set and the online installer."""
import argparse
import hashlib
import json
import re
from pathlib import Path
import tempfile

import httpx
from publish_cos import ORIGIN, upload, verify_remote
from publish_catalog import artifact, sign, asr_artifact


def publish(installer, napcat, runtime, version, online=None, asr=None):
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Invalid installer version')
    if installer.name != f'Momoi-Setup-{version}-x64.exe':
        raise ValueError('Installer version/name mismatch')
    pair = json.loads(napcat.with_suffix('.json').read_text())
    digest = hashlib.file_digest(napcat.open('rb'), 'sha256').hexdigest()
    if pair['filename'] != napcat.name or pair['sha256'] != digest or pair['bytes'] != napcat.stat().st_size:
        raise ValueError('QQ component checksum mismatch')
    runtime_id = json.loads(runtime.read_text())['runtime_id']
    if not re.fullmatch(r'[a-f0-9]{24}', runtime_id) or not re.fullmatch(r'[a-f0-9]{16}', pair['pair_id']):
        raise ValueError('Invalid runtime or QQ pair ID')
    if not re.fullmatch(r'\d+\.\d+\.\d+', pair['version']):
        raise ValueError('Invalid QQ version')
    core_digest = hashlib.file_digest(installer.open('rb'), 'sha256').hexdigest()
    core_key = f'windows/installers/Momoi-Setup-{version}-{core_digest[:16]}-x64.exe'
    pair_key = 'windows/components/' + napcat.name
    payload = {'format_version': 1, 'version': version, 'runtime_id': runtime_id, 'qq_pair_id': pair['pair_id'],
               'core': artifact(installer, version, version + '-' + core_digest[:16], core_key, 'core-installer'),
               'napcat': artifact(napcat, pair['version'], pair['pair_id'], pair_key, 'napcat-installer')}
    if asr is not None:
        payload["asr"] = asr_artifact(asr)
    with tempfile.TemporaryDirectory() as temporary:
        catalog = Path(temporary) / 'install.json'
        catalog.write_text(json.dumps(sign(payload), indent=2) + '\n')
        with httpx.Client(timeout=300, follow_redirects=False) as client:
            for path, key in [(installer, core_key), (napcat, pair_key)]:
                upload(path, key)
                verify_remote(client, key, path)
            if asr is not None:
                upload(asr, "windows/components/" + asr.name)
                verify_remote(client, "windows/components/" + asr.name, asr)
            if online:
                if online.name != f'Momoi-Online-Setup-{version}-x64.exe':
                    raise ValueError('Online installer version/name mismatch')
                meta = json.loads(online.with_suffix('.json').read_text())
                if meta['sha256'] != hashlib.file_digest(online.open('rb'), 'sha256').hexdigest() or meta['bytes'] != online.stat().st_size:
                    raise ValueError('Online installer checksum mismatch')
                key = 'windows/installers/' + online.name
                upload(online, key, mutable=True)
                verify_remote(client, key, online)
            # Switch discovery only after every download has been verified publicly.
            upload(catalog, 'windows/install.json', mutable=True)
            verify_remote(client, 'windows/install.json', catalog)
    print(json.dumps(payload, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--installer', required=True, type=Path)
    parser.add_argument('--napcat', required=True, type=Path)
    parser.add_argument('--runtime', required=True, type=Path)
    parser.add_argument('--version', required=True)
    parser.add_argument('--online', type=Path)
    parser.add_argument("--asr", type=Path)
    args = parser.parse_args()
    publish(args.installer, args.napcat, args.runtime, args.version, args.online, args.asr)
