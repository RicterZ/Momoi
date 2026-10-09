"""Windows distribution and code/native update boundaries."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tarfile
import sys
from types import SimpleNamespace
import zipfile

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'packaging/windows'


def load_script(name):
    spec = importlib.util.spec_from_file_location('test_' + name, SCRIPTS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_bundle(path, *, platform='windows', extra=None, corrupt=False):
    files = {name: name.encode() for name in ('LICENSE', 'av-host/host.cjs', 'av-host/host.html', 'av-host/commands.cjs',
             'napcat-plugin/index.mjs', 'napcat-plugin/package.json',
             'windows/start-av-host.ps1', 'windows/components.json',
             'windows/virtual_audio.py', 'windows/audio_stream.py', 'windows/audio_backend.py', 'windows/wasapi.py')}
    if extra:
        files[extra] = b'linux'
    manifest = {'platform': platform, 'files': {name: hashlib.sha256(data).hexdigest()
                                              for name, data in files.items()}}
    if corrupt:
        files['av-host/host.cjs'] = b'changed-after-hashing'
    with zipfile.ZipFile(path, 'w') as archive:
        for name, data in files.items():
            archive.writestr(name, data)
        archive.writestr('bundle.json', json.dumps(manifest))
    return path


def test_accepts_verified_windows_runtime_code(tmp_path):
    module = load_script('prepare_qq_call')
    files = module.read_windows_bundle(write_bundle(tmp_path / 'windows.zip'))
    assert 'windows/start-av-host.ps1' in files
    assert not any(name.endswith('.sh') or '/probe/' in name for name in files)


@pytest.mark.parametrize('options, message', [
    ({'platform': 'linux'}, 'Not a Windows'),
    ({'extra': 'scripts/audio-control.sh'}, 'Unexpected platform'),
    ({'extra': '../escape'}, 'Unexpected platform'),
    ({'corrupt': True}, 'content hash mismatch'),
])
def test_rejects_other_platforms_and_modified_code(tmp_path, options, message):
    module = load_script('prepare_qq_call')
    with pytest.raises(ValueError, match=message):
        module.read_windows_bundle(write_bundle(tmp_path / 'invalid.zip', **options))


def test_code_revision_does_not_require_runtime_reinstallation(tmp_path, monkeypatch):
    module = load_script('build_release')
    path = tmp_path / 'packaging/windows/components.json'
    path.parent.mkdir(parents=True)
    components = {'node': '24', 'qq_call': {'source': {'revision': 'one', 'sha256': 'one'},
                                           'runtime': {'qq': {'sha256': 'native-one'}}}}
    path.write_text(json.dumps(components))
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    first = module.runtime_components()
    components['qq_call']['source'] = {'revision': 'two', 'sha256': 'two'}
    path.write_text(json.dumps(components))
    assert module.runtime_components() == first
    components['qq_call']['runtime']['qq']['sha256'] = 'native-two'
    path.write_text(json.dumps(components))
    assert module.runtime_components() != first


def test_rejects_unsafe_source_before_extraction(tmp_path, monkeypatch):
    module = load_script('prepare_qq_call')
    archive = tmp_path / 'qq-call-test.tar.gz'
    with tarfile.open(archive, 'w:gz') as source:
        entry = tarfile.TarInfo('../escape')
        source.addfile(entry)
    source_spec = {'revision': 'test', 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}
    monkeypatch.setattr(module, 'component', lambda: {'source': source_spec})
    with pytest.raises(ValueError, match='Unsafe QQ call source'):
        with module.fork_source(tmp_path):
            pytest.fail('Unsafe source must not be yielded')
    assert not (tmp_path.parent / 'escape').exists()


def test_code_zip_ships_windows_bridge_and_portable_media_worker(tmp_path, monkeypatch):
    module = load_script('build_release')
    paths = {
        'src/momoi/dashboard/static/index.html': '<html></html>',
        'src/momoi/channel/napcat/voice_call/audio.py': '# Shared PCM processing',
        'src/momoi/channel/napcat/voice_call/broker.py': '# Portable media worker',
        'desktop/python/backend_entry.py': '# Backend entry',
        'desktop/python/momoi_desktop/backend.py': '# Desktop backend',
        'desktop/python/momoi_desktop/default_emotions/happy.png': 'resource',
        'packaging/windows/components.json': '{}',
    }
    for name, content in paths.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    monkeypatch.setattr(module, 'runtime_requirements', lambda: '')
    monkeypatch.setitem(sys.modules, 'prepare_qq_call', SimpleNamespace(
        windows_code_files=lambda: {'windows/start-av-host.ps1': b'# Windows',
                                   'av-host/host.cjs': b'// shared host'}))
    archive, manifest = module.build_release(tmp_path / 'output', '1.2.3')
    with zipfile.ZipFile(archive) as bundle:
        assert 'app/qq_call_bridge/windows/start-av-host.ps1' in bundle.namelist()
        assert 'app/momoi/channel/napcat/voice_call/audio.py' in bundle.namelist()
        assert 'app/momoi/channel/napcat/voice_call/broker.py' in bundle.namelist()
        assert bundle.read('app/momoi_desktop/backend.py') == b'# Desktop backend'
        assert bundle.read('app/momoi_desktop/default_emotions/happy.png') == b'resource'
        assert set(manifest['files']) == set(bundle.namelist()) - {'release.json'}


def test_linux_broker_imports_with_only_voice_call_package(tmp_path):
    """The NapCat image copies no Momoi parent package initializers."""
    import shutil
    import subprocess

    root = Path(__file__).resolve().parents[1]
    destination = tmp_path / 'momoi/channel/napcat/voice_call'
    shutil.copytree(root / 'src/momoi/channel/napcat/voice_call', destination)
    subprocess.run([sys.executable, '-I', '-c', '''
import sys
sys.path.insert(0, sys.argv[1])
from momoi.channel.napcat.voice_call.broker import MediaBroker
assert MediaBroker('x' * 32).status['phase'] == 'unavailable'
''', str(tmp_path)], check=True, capture_output=True, text=True)
