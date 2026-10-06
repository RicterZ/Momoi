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
    monkeypatch.setattr(module, 'upload', lambda file, key, **kwargs: operations.append(('upload', key)))
    monkeypatch.setattr(module, 'verify_remote', lambda client, key, file: operations.append(('verify', key)))
    module.publish(archive(tmp_path / 'code.zip'))
    assert [operation for operation, _ in operations] == ['upload', 'verify', 'upload', 'verify']
    assert operations[-2][1] == module.LATEST_KEY
    operations.clear()
    def fail(*args):
        raise ValueError('Public download failed')
    monkeypatch.setattr(module, 'verify_remote', fail)
    with pytest.raises(ValueError, match='Public download failed'):
        module.publish(tmp_path / 'code.zip')
    assert len(operations) == 1
    assert operations[0][1] != module.LATEST_KEY
