"""Pin verified Microsoft prerequisite installers without embedding them in Momoi."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
from urllib.parse import urlparse


def prepare(source, destination, include):
    specs = []
    destination.mkdir(parents=True, exist_ok=True)
    for key, name in [('WebView', 'WebView2RuntimeInstallerX64.exe'), ('VC', 'vc_redist.x64.exe')]:
        path = source / name
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        filename = path.stem + '-' + digest[:16] + '.exe'
        shutil.copy2(path, destination / filename)
        origin = json.loads(path.with_name(path.name + '.origin.json').read_text(encoding='utf-8-sig'))
        url = origin['url']
        host = urlparse(url).hostname or ''
        if urlparse(url).scheme != 'https' or not (host == 'microsoft.com' or host.endswith('.microsoft.com')):
            raise ValueError('Prerequisite download must use Microsoft HTTPS origin')
        if origin['sha256'] != digest:
            raise ValueError('Prerequisite differs from captured official download')
        specs.append({'key': key, 'filename': filename, 'sha256': digest, 'bytes': path.stat().st_size, 'url': url})
    include.write_text('\n'.join(f'#define {item["key"]}{suffix} "{item[field]}"' for item in specs
        for suffix, field in [('Name', 'filename'), ('SHA256', 'sha256'), ('URL', 'url')])+'\n', encoding='utf-8')
    (destination / 'prerequisites.json').write_text(json.dumps(specs, indent=2), encoding='utf-8')
    return specs


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--destination', required=True, type=Path)
    parser.add_argument('--include', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(prepare(args.source, args.destination, args.include)))
