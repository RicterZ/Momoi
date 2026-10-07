import importlib.util
from pathlib import Path


def test_trim_preserves_runtime_dependencies_metadata_and_numpy_tests(tmp_path):
    spec = importlib.util.spec_from_file_location('trim_python', Path(__file__).resolve().parents[1] / 'packaging/windows/trim_python.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    removed = ['Lib/ensurepip/wheel.whl', 'Lib/idlelib/idle.py', 'Lib/pydoc_data/topics.py',
               'Lib/site-packages/pip/main.py', 'Lib/site-packages/pip-1.dist-info/METADATA', 'Lib/site-packages/numpy/__pycache__/a.pyc']
    kept = ['python.exe', 'Lib/encodings/utf_8.py', 'Lib/site-packages/numpy/tests/test_a.py',
            'Lib/site-packages/numpy-1.dist-info/METADATA', 'Lib/site-packages/huggingface_hub/__init__.py']
    for name in removed + kept:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'test')
    report = module.trim(tmp_path)
    assert report['removed_bytes'] == len(removed) * 4
    assert all(not (tmp_path / name).exists() for name in removed)
    assert all((tmp_path / name).exists() for name in kept)


def test_prerequisite_manifest_binds_offline_files_and_downloads(tmp_path):
    import hashlib
    import json
    spec = importlib.util.spec_from_file_location('prepare_prerequisites', Path(__file__).resolve().parents[1] / 'packaging/windows/prepare_prerequisites.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source, destination = tmp_path / 'source', tmp_path / 'output'
    source.mkdir()
    for name in ('WebView2RuntimeInstallerX64.exe', 'vc_redist.x64.exe'):
        (source / name).write_bytes(name.encode())
    include = tmp_path / 'pin.iss'
    result = module.prepare(source, destination, include)
    assert len(result) == 2
    assert json.loads((destination / 'prerequisites.json').read_text()) == result
    for item in result:
        shipped = destination / item['filename']
        assert hashlib.sha256(shipped.read_bytes()).hexdigest() == item['sha256']
        assert item['sha256'][:16] in shipped.name
        assert item['url'].endswith(shipped.name)
        assert item['sha256'] in include.read_text()
