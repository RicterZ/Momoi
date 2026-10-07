import importlib.util
from pathlib import Path
import zipfile


def test_distribution_layout_excludes_diagnostics(tmp_path):
    spec = importlib.util.spec_from_file_location('installer_package', Path(__file__).resolve().parents[1] / 'packaging/windows/package_installer.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    files = {
        'Momoi-Setup-1.1.3-x64.exe': b'main',
        'components/Momoi-QQ-Components-1.1.2-pair-x64.exe': b'qq',
        'components/prerequisites/vc.exe': b'vc',
        'install-test/log.txt': b'private diagnostics',
        'app/data/config.toml': b'private data',
    }
    for name, content in files.items():
        file = tmp_path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(content)
    result = module.package_installer(tmp_path, '1.1.3')
    with zipfile.ZipFile(result) as archive:
        assert set(archive.namelist()) == {name for name in files if name.startswith(('Momoi-Setup-', 'components/'))}
        assert archive.read('components/Momoi-QQ-Components-1.1.2-pair-x64.exe') == b'qq'
