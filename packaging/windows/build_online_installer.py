"""Build a single .NET online installer; no backend, model or QQ is embedded."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def build(output, version, dotnet='dotnet'):
    import re
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Invalid installer version')
    output.mkdir(parents=True, exist_ok=True)
    payload = output / 'online-payload'
    project = str(ROOT / 'desktop/Momoi.Installer/Momoi.Installer.csproj')
    subprocess.run([dotnet, 'restore', project, '-r', 'win-x64', '--locked-mode'], check=True)
    subprocess.run([dotnet, 'publish', project, '--no-restore',
                    '-c', 'Release', '-r', 'win-x64', '--self-contained', 'true',
                    '-p:PublishSingleFile=true', '-p:IncludeNativeLibrariesForSelfExtract=true',
                    '-p:EnableCompressionInSingleFile=true', '-p:Version=' + version,
                    '-o', str(payload)], check=True)
    executable = output / f'Momoi-Online-Setup-{version}-x64.exe'
    executable.write_bytes((payload / 'Momoi.Installer.exe').read_bytes())
    digest = hashlib.file_digest(executable.open('rb'), 'sha256').hexdigest()
    executable.with_suffix('.json').write_text(json.dumps({'version': version, 'filename': executable.name,
        'sha256': digest, 'bytes': executable.stat().st_size,
        'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()}, indent=2) + '\n')
    print(json.dumps({'installer': str(executable), 'bytes': executable.stat().st_size, 'sha256': digest}))
    return executable


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist/windows')
    parser.add_argument('--dotnet', default='dotnet')
    args = parser.parse_args()
    build(args.output, args.version, args.dotnet)
