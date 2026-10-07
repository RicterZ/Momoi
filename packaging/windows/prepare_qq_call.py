"""Build the Windows-only fork bundle; keep code and native components separate."""
import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[2]


def component():
    return json.loads((ROOT / 'packaging/windows/components.json').read_text())['qq_call']


def verified_download(spec, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        temporary = destination.with_suffix('.partial')
        try:
            with urllib.request.urlopen(spec['url'], timeout=120) as source, temporary.open('wb') as target:
                shutil.copyfileobj(source, target)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    with destination.open('rb') as stream:
        actual = hashlib.file_digest(stream, 'sha256').hexdigest()
    if actual != spec['sha256']:
        raise ValueError('QQ call source SHA256 mismatch: ' + str(destination))
    return destination


@contextmanager
def fork_source(cache):
    spec = component()
    source = spec['source']
    archive = verified_download(source, cache / ('qq-call-' + source['revision'] + '.tar.gz'))
    with tempfile.TemporaryDirectory(prefix='qq-call-source-', dir=cache) as directory:
        with tarfile.open(archive) as bundle:
            for entry in bundle.getmembers():
                path = PurePosixPath(entry.name)
                if path.is_absolute() or '..' in path.parts or not (entry.isfile() or entry.isdir()):
                    raise ValueError('Unsafe QQ call source archive')
            bundle.extractall(directory, filter='data')
        roots = list(Path(directory).iterdir())
        if len(roots) != 1:
            raise ValueError('Expected one QQ call source root')
        root = roots[0]
        native = json.loads((root / 'bridge/windows/components.json').read_text())
        if native != spec['runtime']:
            raise ValueError('Fork native components differ from the pinned runtime; update components.json')
        yield root


def read_windows_bundle(archive):
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        if len(names) != len(set(names)):
            raise ValueError('Duplicate QQ call bundle paths')
        allowed = {'LICENSE', 'bundle.json', 'av-host/host.cjs', 'av-host/host.html', 'av-host/commands.cjs',
                   'napcat-plugin/index.mjs', 'napcat-plugin/package.json',
                   'windows/start-av-host.ps1', 'windows/components.json',
                   'windows/virtual_audio.py', 'windows/audio_stream.py', 'windows/audio_backend.py', 'windows/wasapi.py'}
        if set(names) != allowed:
            raise ValueError('Unexpected platform/runtime files in Windows QQ call bundle')
        files = {name: bundle.read(name) for name in names}
        manifest = json.loads(files['bundle.json'])
        if manifest['platform'] != 'windows':
            raise ValueError('Not a Windows QQ call bundle')
        hashes = {name: hashlib.sha256(data).hexdigest() for name, data in files.items() if name != 'bundle.json'}
        if hashes != manifest['files']:
            raise ValueError('QQ call bundle content hash mismatch')
        return files


def windows_code_files(cache=None):
    cache = cache or ROOT / 'build/windows-downloads'
    with fork_source(cache) as source:
        archive = source / 'windows.zip'
        subprocess.run([sys.executable, str(source / 'bridge/build_bundle.py'),
                        '--platform', 'windows', '--output', str(archive)], check=True,
                       stdout=subprocess.DEVNULL)
        files = read_windows_bundle(archive)
        spec = component()['source']
        files['SOURCE.txt'] = ('https://github.com/RicterZ/maibot-qq-voice-call\n'
                              + 'Revision: ' + spec['revision'] + '\n').encode('utf-8')
        return files


def prepare_native(stage, cache=None):
    cache = cache or ROOT / 'build/windows-downloads'
    with fork_source(cache) as source:
        output = stage / 'runtime/qq-call'
        sevenzip = shutil.which('7z') or r'C:\Program Files\7-Zip\7z.exe'
        subprocess.run([sys.executable, str(source / 'bridge/windows/prepare_runtime.py'),
                        '--output', str(output), '--cache', str(cache), '--sevenzip', sevenzip], check=True)
        notices = stage / 'licenses/qq-call'
        notices.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / 'LICENSE', notices / 'LICENSE')
        spec = component()['source']
        (notices / 'SOURCE.txt').write_text(
            'Source: https://github.com/RicterZ/maibot-qq-voice-call\n'
            + 'Revision: ' + spec['revision'] + '\nSHA256: ' + spec['sha256'] + '\n'
            + 'Windows host adaptation; GPL-3.0-only\n', encoding='utf-8')
        shutil.copy2(source / 'bridge/windows/components.json', notices / 'components.json')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', required=True, type=Path)
    args = parser.parse_args()
    prepare_native(args.stage)
