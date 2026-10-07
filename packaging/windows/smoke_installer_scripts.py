"""Compile the actual Inno scripts against tiny fixtures before staging runtimes."""
import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]


def check(compiler):
    with tempfile.TemporaryDirectory(prefix='momoi-inno-syntax-') as directory:
        root = Path(directory)
        scripts = root / 'packaging/windows'
        scripts.mkdir(parents=True)
        build = root / 'build'
        build.mkdir()
        for name in ('installer.iss', 'qq_components.iss'):
            shutil.copy2(ROOT / 'packaging/windows' / name, scripts / name)
        shutil.copy2(ROOT / 'LICENSE', root / 'LICENSE')
        assets = root / 'desktop/Momoi.Desktop/Assets'
        assets.mkdir(parents=True)
        shutil.copy2(ROOT / 'desktop/Momoi.Desktop/Assets/momoi.ico', assets / 'momoi.ico')
        fixture = root / 'fixture.txt'
        fixture.write_text('syntax check only', encoding='ascii')
        entry = f'Source: "{fixture}"; DestDir: "{{app}}"; Flags: ignoreversion\n'
        for name in ('windows-installer-files.iss', 'windows-qq-files.iss'):
            (build / name).write_text(entry, encoding='utf-8')
        defines = {'QQPairVersion': '1.1.2', 'QQPairId': 'syntax-check', 'QQPackageBase': 'QQ-syntax-check',
                   'QQPackageName': 'QQ-syntax-check.exe', 'QQPackageSHA256': '0' * 64,
                   'QQPackageURL': 'https://example.invalid/qq.exe'}
        (build / 'windows-qq-pin.iss').write_text('\n'.join(f'#define {key} "{value}"' for key, value in defines.items()), encoding='utf-8')
        prerequisites = build / 'windows-prerequisites'
        prerequisites.mkdir()
        for name in ('WebView2RuntimeInstallerX64.exe', 'vc_redist.x64.exe'):
            shutil.copy2(fixture, prerequisites / name)
        for name in ('qq_components.iss', 'installer.iss'):
            subprocess.run([str(compiler), '/Q', str(scripts / name)], check=True)
    print('PASS: both split installer scripts compiled against isolated tiny fixtures')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compiler', required=True, type=Path)
    args = parser.parse_args()
    check(args.compiler)
