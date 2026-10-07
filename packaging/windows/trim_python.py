"""Remove private-interpreter developer tooling, preserving runtime package metadata."""
import argparse
import json
from pathlib import Path
import shutil


def trim(root):
    removed = []
    candidates = [root / 'Lib' / name for name in ('ensurepip', 'idlelib', 'pydoc_data')]
    packages = root / 'Lib/site-packages'
    candidates += [packages / 'pip', *packages.glob('pip-*.dist-info')]
    candidates += list(root.rglob('__pycache__'))
    for path in candidates:
        if not path.exists():
            continue
        size = sum(p.stat().st_size for p in path.rglob('*') if p.is_file())
        removed.append({'path': path.relative_to(root).as_posix(), 'bytes': size})
        shutil.rmtree(path)
    return {'removed_bytes': sum(item['bytes'] for item in removed), 'removed': removed}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args()
    report = trim(args.root)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({'removed_bytes': report['removed_bytes']}))
