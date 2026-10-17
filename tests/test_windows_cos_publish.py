"""Release discovery must never advance before downloads are verified."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import zipfile

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'packaging/windows'


def publisher(monkeypatch):
    monkeypatch.setitem(sys.modules, 'sign_latest', SimpleNamespace(
        sign_latest=lambda archive, url, output: output.write_text('{"signed":"test"}')))
    spec = importlib.util.spec_from_file_location('cos_publish', SCRIPTS / 'publish_cos.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def archive(path, *, corrupt=False, unsafe=False):
    name = '../escape' if unsafe else 'app/backend_entry.py'
    content = b'code'
    manifest = {'format_version': 1, 'version': '1.2.3', 'release_id': '1.2.3-content',
                'runtime_id': 'runtime', 'files': {name: hashlib.sha256(content).hexdigest()}}
    with zipfile.ZipFile(path, 'w') as bundle:
        bundle.writestr(name, b'bad' if corrupt else content)
        bundle.writestr('release.json', json.dumps(manifest))
    return path


@pytest.mark.parametrize('options, error', [({'corrupt': True}, 'hash mismatch'),
                                          ({'unsafe': True}, 'Unsafe')])
def test_invalid_archive_never_uploads(tmp_path, monkeypatch, options, error):
    module = publisher(monkeypatch)
    monkeypatch.setattr(module, 'upload', lambda *_a, **_k: pytest.fail('Invalid file uploaded'))
    with pytest.raises(ValueError, match=error):
        module.publish(archive(tmp_path / 'code.zip', **options))


def test_latest_is_last_and_not_published_after_failed_download(tmp_path, monkeypatch):
    module = publisher(monkeypatch)
    operations = []
    monkeypatch.setattr(module, "sync_catalog", lambda client, version, directory: operations.append(("catalog", version)))
    monkeypatch.setattr(module, 'upload', lambda file, key, **kwargs: operations.append(('upload', key)))
    monkeypatch.setattr(module, 'verify_remote', lambda client, key, file: operations.append(('verify', key)))
    module.publish(archive(tmp_path / 'code.zip'))
    assert [operation for operation, _ in operations] == ['upload', 'verify', 'catalog', 'upload', 'verify']
    assert operations[-2][1] == module.LATEST_KEY
    operations.clear()
    def fail(*args):
        raise ValueError('Public download failed')
    monkeypatch.setattr(module, 'verify_remote', fail)
    with pytest.raises(ValueError, match='Public download failed'):
        module.publish(tmp_path / 'code.zip')
    assert len(operations) == 1
    assert operations[0][1] != module.LATEST_KEY


@pytest.mark.parametrize('corrupt', [False, True])
def test_catalog_sync_keeps_real_component_versions_and_checks_hashes(tmp_path, monkeypatch, corrupt):
    import base64
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    module = publisher(monkeypatch)
    key = Ed25519PrivateKey.generate()
    (tmp_path / 'update-public.bin').write_bytes(key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw))
    monkeypatch.setattr(sys.modules['sign_latest'], 'KEYS', tmp_path, raising=False)
    monkeypatch.setitem(sys.modules, 'publish_catalog', SimpleNamespace(sign=lambda x: x))
    component = {'version': '1.1.5', 'id': 'old', 'url': module.ORIGIN + '/windows/component.zip',
                 'size': 4, 'sha256': hashlib.sha256(b'good').hexdigest()}
    catalog = {'format_version': 1, 'version': '1.1.5', 'shell': component, 'napcat': component}
    raw = json.dumps(catalog).encode()
    envelope = {'signed': base64.b64encode(raw).decode(), 'signature': base64.b64encode(key.sign(raw)).decode()}
    class Response:
        def raise_for_status(self): pass
        def json(self): return envelope
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def iter_bytes(self): yield b'bad!' if corrupt else b'good'
    client = SimpleNamespace(get=lambda *a, **kw: Response(), stream=lambda *a, **kw: Response())
    published = []
    monkeypatch.setattr(module, 'upload', lambda path, name, **kw: published.append((name, json.loads(path.read_text()))))
    monkeypatch.setattr(module, 'verify_remote', lambda *args: None)
    if corrupt:
        with pytest.raises(ValueError, match='checksum mismatch'):
            module.sync_catalog(client, '1.1.7', tmp_path)
        assert not published
    else:
        module.sync_catalog(client, '1.1.7', tmp_path)
        assert published[0][0] == 'windows/catalog.json'
        payload = published[0][1]
        assert payload['version'] == '1.1.7'
        assert payload['shell'] == component and payload['napcat'] == component
