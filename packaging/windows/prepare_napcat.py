"""Stage the pinned official Windows Node bundle, without changing upstream code."""
import argparse
import hashlib
import json
import shutil
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
REQUIRED = ('node.exe', 'index.js', 'wrapper.node', 'QQNT.dll', 'package.json', 'config.json', 'napcat/napcat.mjs')


def prepare(stage: Path, archive: Path) -> None:
    component = json.loads((ROOT / 'packaging/windows/components.json').read_text())['napcat']
    if not archive.exists():
        archive.parent.mkdir(parents=True, exist_ok=True)
        temporary = archive.with_suffix('.download')
        try:
            with urllib.request.urlopen(component['url'], timeout=120) as source, temporary.open('wb') as target:
                shutil.copyfileobj(source, target)
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)
    with archive.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
    if digest != component['sha256']:
        raise ValueError('NapCat archive SHA-256 mismatch; remove the cache and retry')
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
