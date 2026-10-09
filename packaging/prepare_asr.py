"""Download the pinned optional model; never download during recognition."""
import argparse
import hashlib
import json
from pathlib import Path
import tarfile
import urllib.request


def prepare(output, archive):
    spec = json.loads(Path(__file__).with_name('asr-model.json').read_text())
    archive.parent.mkdir(parents=True, exist_ok=True)
    if not archive.exists():
        temporary = archive.with_suffix('.partial')
        urllib.request.urlretrieve(spec['url'], temporary)
        temporary.replace(archive)
    with archive.open('rb') as file:
        if hashlib.file_digest(file, 'sha256').hexdigest() != spec['sha256']:
            raise ValueError('ASR model archive checksum mismatch')
    output.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as package:
        for member in package.getmembers():
            path = Path(member.name)
            if path.is_absolute() or '..' in path.parts or member.issym() or member.islnk():
                raise ValueError('Unsafe model archive')
            if member.isfile() and len(path.parts) == 2:
                (output / path.name).write_bytes(package.extractfile(member).read())
    (output / 'model-lock.json').write_text(json.dumps(spec, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parents[1] / 'models' / 'asr')
    parser.add_argument('--archive', type=Path, default=Path('build/local-asr/model.tar.bz2'))
    args = parser.parse_args()
    prepare(args.output, args.archive)
