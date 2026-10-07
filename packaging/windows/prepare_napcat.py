"""Stage the pinned official Windows Node bundle, without changing upstream code."""
import argparse
import hashlib
import json
import shutil
import os
import subprocess
import time
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
REQUIRED = ('node.exe', 'index.js', 'wrapper.node', 'QQNT.dll', 'package.json', 'config.json', 'napcat/napcat.mjs')


def download_verified(destination: Path, component: dict) -> None:
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix('.download')
        try:
            for attempt in range(4):
                try:
                    with urllib.request.urlopen(component['url'], timeout=120) as source, temporary.open('wb') as target:
                        shutil.copyfileobj(source, target)
                    temporary.replace(destination)
                    break
                except (OSError, urllib.error.URLError):
                    temporary.unlink(missing_ok=True)
                    if attempt == 3:
                        raise
                    print(f'Network download retry {attempt + 1}/3: {destination.name}', flush=True)
                    time.sleep(2 ** attempt)
        finally:
            temporary.unlink(missing_ok=True)
    with destination.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
    if digest != component['sha256']:
        raise ValueError(f'Component SHA-256 mismatch: {destination}; remove the cache and retry')


def complete_native_dependencies(destination: Path, cache: Path, component: dict) -> None:
    missing = [name for name in ('crypto.dll', 'ssl.dll', 'dbghelp.dll') if not (destination / name).exists()]
    if not missing:
        return
    installer = cache / ('QQ-' + component['version'] + '.exe')
    download_verified(installer, component)
    sevenzip = shutil.which('7z') or str(Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / '7-Zip/7z.exe')
    extracted = cache / ('QQ-' + component['version'])
    if extracted.exists():
        shutil.rmtree(extracted)
    subprocess.run([sevenzip, 'x', '-y', '-r', '-o' + str(extracted), str(installer), '*wrapper.node', '*crypto.dll', '*ssl.dll', '*dbghelp.dll'], check=True, stdout=subprocess.DEVNULL)
    # Use the exact QQ distribution that produced the bundle's wrapper.node.
    expected = hashlib.sha256((destination / 'wrapper.node').read_bytes()).digest()
    matches = [path.parent for path in extracted.rglob('wrapper.node') if hashlib.sha256(path.read_bytes()).digest() == expected]
    if len(matches) != 1:
        raise ValueError('QQ installer wrapper.node does not uniquely match the pinned NapCat native runtime')
    for name in missing:
        source = matches[0] / name
        if not source.is_file():
            raise FileNotFoundError(f'Missing QQ native dependency: {source}')
        shutil.copy2(source, destination / name)
        print('Completed QQ native dependency:', name, flush=True)


def prepare(stage: Path, archive: Path) -> None:
    component = json.loads((ROOT / 'packaging/windows/components.json').read_text())['napcat']
    download_verified(archive, component)
    destination = stage / 'runtime/napcat'
    if destination.exists():
        shutil.rmtree(destination)
    with zipfile.ZipFile(archive) as bundle:
        for item in bundle.infolist():
            name = PurePosixPath(item.filename)
            if name.is_absolute() or '..' in name.parts or '\\' in item.filename or ':' in item.filename or (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('Unsafe NapCat archive path')
        if not all(name in bundle.namelist() for name in REQUIRED):
            raise ValueError('NapCat Windows Node bundle is incomplete')
        bundle.extractall(destination)
    complete_native_dependencies(destination, archive.parent, component['qq_native'])
    notices = stage / 'licenses/napcat'
    notices.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / 'packaging/windows/napcat-LICENSE', notices / 'LICENSE')
    shutil.copy2(ROOT / 'packaging/windows/napcat-node-LICENSE', notices / 'Node-v22.11.0-LICENSE')
    (notices / 'SOURCE.txt').write_text('NapCat © 2024 Mlikiowa\nSource: https://github.com/NapNeko/NapCatQQ/tree/' + component['version'] + '\nOfficial unmodified Windows Node bundle\nSHA256: ' + component['sha256'] + '\n', encoding='utf-8')
    for path in destination.rglob('*'):
        if path.is_file() and path.name.upper().startswith(('LICENSE', 'NOTICE', 'COPYING')):
            target = notices / path.relative_to(destination)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--stage', required=True, type=Path)
    parser.add_argument('--archive', type=Path, default=ROOT / 'build/windows-downloads/NapCat-v4.18.30.zip')
    args = parser.parse_args()
    prepare(args.stage, args.archive)
