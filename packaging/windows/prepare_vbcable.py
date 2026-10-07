"""Preserve the complete official donationware driver package for optional installation."""
import argparse
import hashlib
import json
from pathlib import Path
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[2]


def prepare(destination):
    spec = json.loads((ROOT / 'packaging/windows/vbcable.json').read_text())
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / 'VBCABLE_Driver_Pack45.zip'
    if not archive.exists():
        with urllib.request.urlopen(spec['url'], timeout=120) as response:
            archive.write_bytes(response.read())
    if hashlib.sha256(archive.read_bytes()).hexdigest() != spec['sha256']:
        raise ValueError('VB-CABLE package checksum mismatch')
    with zipfile.ZipFile(archive) as bundle:
        for item in bundle.infolist():
            if Path(item.filename).name != item.filename or item.is_dir():
                raise ValueError('Unexpected VB-CABLE archive member')
        bundle.extractall(destination / 'vbcable')
    return archive


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--destination', type=Path, required=True)
    print(prepare(parser.parse_args().destination))
