"""Prepare the optional Windows CPU ASR component separately from core runtime."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import shutil
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'packaging'))
from prepare_asr import prepare


def build(destination, archive):
    model = destination / 'models' / 'asr'
    libs = destination / 'runtime' / 'asr' / 'site-packages'
    wheels = ROOT / 'build' / 'windows-asr-wheels'
    source = ROOT / 'models' / 'asr'
    prepare(source, archive)
    if model.exists(): shutil.rmtree(model)
    shutil.copytree(source, model)
    wheels.mkdir(parents=True, exist_ok=True)
    subprocess.run(['uv', 'run', '--no-project', '--with', 'pip', 'python', '-m', 'pip',
                    'download', '--only-binary=:all:', '--platform', 'win_amd64',
                    '--python-version', '312', '--implementation', 'cp', '--abi', 'cp312',
                    '--no-deps', '--dest', str(wheels), 'sherpa-onnx==1.13.8',
                    'sherpa-onnx-core==1.13.8'], check=True)
    if libs.exists(): shutil.rmtree(libs)
    libs.mkdir(parents=True, exist_ok=True)
    for wheel in sorted(wheels.glob('*1.13.8*win_amd64.whl')):
        with zipfile.ZipFile(wheel) as package:
            for member in package.infolist():
                path = Path(member.filename)
                if any(part.endswith('.data') for part in path.parts):
                    continue
                if path.is_absolute() or '..' in path.parts or any(part.endswith('.data') for part in path.parts):
                    raise ValueError('Unsafe or unsupported ASR wheel layout')
            for member in package.infolist():
                if not any(part.endswith('.data') for part in Path(member.filename).parts):
                    package.extract(member, libs)
    files = {p.relative_to(destination).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(destination.rglob('*')) if p.is_file()
             and p.name not in ('component.json', 'component-id.txt')}
    identity = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:24]
    marker = destination / 'runtime' / 'asr'
    (marker / 'component-id.txt').write_text(identity)
    (marker / 'component.json').write_text(json.dumps({'id': identity, 'files': files}, indent=2))
    return identity


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path, default=ROOT / 'build' / 'windows-asr-stage')
    parser.add_argument('--archive', type=Path, default=ROOT / 'build' / 'local-asr' / 'model.tar.bz2')
    args = parser.parse_args()
    print(build(args.destination, args.archive))
