"""Create the end-user installer ZIP, excluding build and diagnostic artifacts."""
import argparse
from pathlib import Path
import zipfile


def package_installer(directory: Path, version: str) -> Path:
    installers = list(directory.glob('Momoi-Setup-*-x64.exe'))
    components = directory / 'components'
    qq = list(components.glob('Momoi-QQ-Components-*-x64.exe'))
    if len(installers) != 1 or len(qq) != 1:
        raise ValueError('Expected one main installer and one locked QQ component')
    files = [installers[0], *sorted(p for p in components.rglob('*') if p.is_file())]
    output = directory / f'Momoi-Windows-{version}-x64.zip'
    # EXEs are already compressed by Inno Setup; avoid recompressing hundreds of MB.
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED) as archive:
        for file in files:
            archive.write(file, file.relative_to(directory).as_posix())
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--version', required=True)
    args = parser.parse_args()
    print(package_installer(args.directory, args.version))
