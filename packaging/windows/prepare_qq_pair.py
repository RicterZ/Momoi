"""Pin a QQ/NapCat pair and bind its exact installer hash into the main installer."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def prepare(stage, output, archive=None):
    spec = json.loads((ROOT / 'packaging/windows/components.json').read_text())
    # v1.1.2 is the frozen pair release, independent of later Momoi code versions.
    pair = {'version': '1.1.2', 'layout': 'trimmed-avsdk-v1', 'napcat': spec['napcat'],
            'qq_call': spec['qq_call']['runtime']}
    lock = json.loads((ROOT / 'packaging/windows/qq-pair.lock.json').read_text())
    if pair != lock:
        raise ValueError('Native components differ from the frozen QQ 1.1.2 pair; publish a new pair lock explicitly')
    pair_id = hashlib.sha256(json.dumps(pair, sort_keys=True).encode()).hexdigest()[:16]
    name = f'Momoi-QQ-Components-1.1.2-{pair_id}-x64'
    marker = stage / 'runtime/qq-pair'
    marker.mkdir(parents=True, exist_ok=True)
    (marker / 'pair-id.txt').write_text(pair_id, encoding='ascii')
    (marker / 'components.json').write_text(json.dumps(pair, indent=2), encoding='utf-8')
    sha = ''
    if archive:
        if archive.name != name + '.exe':
            raise ValueError('QQ component filename differs from frozen pair')
        with archive.open('rb') as stream:
            sha = hashlib.file_digest(stream, 'sha256').hexdigest()
    package_name = name + '.exe'
    if archive:
        package_name = name.removesuffix('-x64') + '-' + sha[:16] + '-x64.exe'
        destination = archive.with_name(package_name)
        if destination != archive:
            archive.replace(destination)
            archive = destination
    url = 'https://momoi-1253047877.cos.ap-guangzhou.myqcloud.com/windows/components/' + package_name
    values = {'QQPairVersion': pair['version'], 'QQPairId': pair_id, 'QQPackageBase': name,
              'QQPackageName': package_name, 'QQPackageSHA256': sha, 'QQPackageURL': url}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text('\n'.join(f'#define {key} "{value}"' for key, value in values.items())+'\n', encoding='utf-8')
    if archive:
        archive.with_suffix('.json').write_text(json.dumps({**pair, 'pair_id': pair_id, 'filename': archive.name,
            'sha256': sha, 'bytes': archive.stat().st_size, 'url': url}, indent=2), encoding='utf-8')
    return values


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--archive', type=Path)
    args = parser.parse_args()
    print(json.dumps(prepare(args.stage, args.output, args.archive)))
