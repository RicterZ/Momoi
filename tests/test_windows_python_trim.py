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
