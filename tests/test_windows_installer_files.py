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
