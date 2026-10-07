"""Publication switches signed install discovery only after verifying its artifacts."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / 'packaging/windows'


def module():
    sys.path.insert(0, str(PACKAGING))
    try:
        spec = importlib.util.spec_from_file_location('publish_install', PACKAGING / 'publish_install.py')
        value = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(value)
        return value
    finally:
        sys.path.remove(str(PACKAGING))


def fixtures(tmp_path):
    installer = tmp_path / 'Momoi-Setup-1.1.3-x64.exe'
    installer.write_bytes(b'core runtime/model installer')
    pair = tmp_path / 'Momoi-QQ-Components-1.1.2-test-x64.exe'
    pair.write_bytes(b'locked QQ pair')
    pair.with_suffix('.json').write_text(json.dumps({'filename': pair.name, 'sha256': hashlib.sha256(pair.read_bytes()).hexdigest(),
        'bytes': pair.stat().st_size, 'version': '1.1.2', 'pair_id': '0123456789abcdef'}))
    runtime = tmp_path / 'runtime.json'
    runtime.write_text(json.dumps({'runtime_id': 'a' * 24}))
    return installer, pair, runtime


def test_install_discovery_follows_verified_immutable_components(tmp_path):
    publisher = module()
    installer, pair, runtime = fixtures(tmp_path)
    events = []
    with patch.object(publisher, 'upload', side_effect=lambda path, key, **kwargs: events.append(('upload', key))), \
         patch.object(publisher, 'verify_remote', side_effect=lambda client, key, local: events.append(('verify', key))), \
         patch.object(publisher, 'sign', side_effect=lambda payload: payload):
        publisher.publish(installer, pair, runtime, '1.1.3')
    core_hash = hashlib.sha256(installer.read_bytes()).hexdigest()[:16]
    assert events == [
        ('upload', f'windows/installers/Momoi-Setup-1.1.3-{core_hash}-x64.exe'),
        ('verify', f'windows/installers/Momoi-Setup-1.1.3-{core_hash}-x64.exe'),
        ('upload', 'windows/components/' + pair.name), ('verify', 'windows/components/' + pair.name),
        ('upload', 'windows/install.json'), ('verify', 'windows/install.json')]


def test_failed_component_verification_never_advances_install_discovery(tmp_path):
    import pytest
    publisher = module()
    installer, pair, runtime = fixtures(tmp_path)
    uploads = []
    with patch.object(publisher, 'upload', side_effect=lambda path, key, **kwargs: uploads.append(key)), \
         patch.object(publisher, 'verify_remote', side_effect=ValueError('remote checksum mismatch')), \
         patch.object(publisher, 'sign', side_effect=lambda payload: payload), pytest.raises(ValueError):
        publisher.publish(installer, pair, runtime, '1.1.3')
    assert 'windows/install.json' not in uploads
