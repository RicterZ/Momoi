"""Build the signed-catalog shell payload, independently of the Windows runtimes."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[2]


def build(output, version, dotnet='dotnet'):
    output.mkdir(parents=True, exist_ok=True)
    payload = output / 'payload'
    subprocess.run([dotnet, 'publish', str(ROOT / 'desktop/Momoi.Desktop/Momoi.Desktop.csproj'),
                    '-c', 'Release', '-r', 'win-x64', '--self-contained', 'true',
                    '-p:PublishSingleFile=false', '-p:Version=' + version, '-o', str(payload)], check=True)
    files = {p.relative_to(payload).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(payload.rglob('*')) if p.is_file()}
    manifest = {'version': version, 'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(), 'files': files}
    archive = output / f'Momoi-Shell-{version}-x64.zip'
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        bundle.writestr('shell-manifest.json', json.dumps(manifest, indent=2))
        for name in files:
            bundle.write(payload / name, 'payload/' + name)
    return archive


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist/windows/shell')
    parser.add_argument('--dotnet', default='dotnet')
    args = parser.parse_args()
    print(build(args.output, args.version, args.dotnet))
