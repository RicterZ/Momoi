import importlib.util
from pathlib import Path


def test_identical_files_share_source_without_changing_destinations(tmp_path):
    spec = importlib.util.spec_from_file_location('installer_files', Path(__file__).resolve().parents[1] / 'packaging/windows/prepare_installer_files.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stage = tmp_path / '桃井 application'
    for name, content in {'runtime/napcat/wrapper.node': b'native', 'runtime/qq-call/wrapper.node': b'native',
                          'runtime/qq-call/other.node': b'other!', 'data/config.json': b'private'}.items():
        path = stage / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    output = tmp_path / 'entries.iss'
    report = module.prepare(stage, output)
    entries = output.read_text(encoding='utf-8-sig')
    assert report['files'] == 3
    assert report['unique_files'] == 2
    assert report['duplicate_bytes'] == 6
    assert entries.count(str(stage / 'runtime/napcat/wrapper.node')) == 2
    assert 'DestDir: "{app}\\runtime\\qq-call"; DestName: "wrapper.node"' in entries
    assert 'other.node' in entries
    assert 'config.json' not in entries


def test_split_installer_keeps_qq_pair_together(tmp_path):
    spec = importlib.util.spec_from_file_location('installer_split', Path(__file__).resolve().parents[1] / 'packaging/windows/prepare_installer_files.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stage = tmp_path / 'app'
    for name in ('Momoi.exe', 'runtime/python/python.exe', 'runtime/napcat/node.exe',
                 'runtime/qq-call/qq/Files/QQ.exe', 'runtime/qq-pair/pair-id.txt'):
        path = stage / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())
    main, qq = tmp_path / 'main.iss', tmp_path / 'qq.iss'
    module.prepare(stage, main, 'main')
    module.prepare(stage, qq, 'qq')
    main_text, qq_text = main.read_text(encoding='utf-8-sig'), qq.read_text(encoding='utf-8-sig')
    assert 'napcat' not in main_text and 'qq-call' not in main_text and 'qq-pair' not in main_text
    assert 'python.exe' in main_text and 'Momoi.exe' in main_text
    assert 'napcat' in qq_text and 'qq-call' in qq_text and 'qq-pair' in qq_text
    assert 'python.exe' not in qq_text


def test_pair_lock_rejects_silent_native_version_change(tmp_path, monkeypatch):
    import json
    import pytest
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('qq_pair', root / 'packaging/windows/prepare_qq_pair.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    packaging = tmp_path / 'packaging/windows'
    packaging.mkdir(parents=True)
    for name in ('components.json', 'qq-pair.lock.json'):
        (packaging / name).write_bytes((root / 'packaging/windows' / name).read_bytes())
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    first = module.prepare(tmp_path / 'app', tmp_path / 'pin.iss')
    assert first['QQPairVersion'] == '1.1.2'
    components = json.loads((packaging / 'components.json').read_text())
    components['napcat']['version'] = 'different'
    (packaging / 'components.json').write_text(json.dumps(components))
    with pytest.raises(ValueError, match='frozen QQ'):
        module.prepare(tmp_path / 'app', tmp_path / 'pin.iss')


def test_component_installer_hash_is_pinned_in_main_include(tmp_path):
    import hashlib
    spec = importlib.util.spec_from_file_location('qq_pair_hash', Path(__file__).resolve().parents[1] / 'packaging/windows/prepare_qq_pair.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stage, include = tmp_path / 'app', tmp_path / 'pin.iss'
    initial = module.prepare(stage, include)
    archive = tmp_path / initial['QQPackageName']
    archive.write_bytes(b'component installer bytes')
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    final = module.prepare(stage, include, archive)
    assert final['QQPackageSHA256'] == digest
    assert digest[:16] in final['QQPackageName']
    assert final['QQPackageName'].endswith('-x64.exe')
    assert final['QQPairId'] == initial['QQPairId']
    assert (tmp_path / final['QQPackageName']).read_bytes() == b'component installer bytes'
    assert final['QQPackageURL'].endswith(final['QQPackageName'])
    assert digest in include.read_text()
