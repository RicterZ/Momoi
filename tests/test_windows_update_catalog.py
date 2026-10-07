import importlib.util
import json
from pathlib import Path
import sys
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_module():
    directory = str(ROOT / 'packaging/windows')
    sys.path.insert(0, directory)
    try:
        spec = importlib.util.spec_from_file_location('publish_catalog_test', ROOT / 'packaging/windows/publish_catalog.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(directory)


def test_catalog_rejects_tampered_shell_before_upload(tmp_path, monkeypatch):
    module = load_module()
    shell = tmp_path / 'shell.zip'
    with zipfile.ZipFile(shell, 'w') as bundle:
        bundle.writestr('shell-manifest.json', json.dumps({'version': '1.1.3', 'files': {'Momoi.exe': '0' * 64}}))
        bundle.writestr('payload/Momoi.exe', b'tampered')
    called = []
    monkeypatch.setattr(module, 'upload', lambda *args, **kwargs: called.append(args))
    with pytest.raises(ValueError, match='checksum'):
        module.publish(shell, tmp_path / 'missing.exe', '1.1.3')
    assert called == []


def test_catalog_rejects_unlisted_shell_files(tmp_path):
    module = load_module()
    shell = tmp_path / 'shell.zip'
    with zipfile.ZipFile(shell, 'w') as bundle:
        bundle.writestr('shell-manifest.json', json.dumps({'version': '1.1.3', 'files': {}}))
        bundle.writestr('payload/evil.exe', b'extra')
    with pytest.raises(ValueError, match='file manifest'):
        module.publish(shell, tmp_path / 'missing.exe', '1.1.3')
