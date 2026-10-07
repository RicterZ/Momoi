"""Generate Inno entries that store byte-identical installed files only once."""
import argparse
import hashlib
import json
from pathlib import Path


def quoted(value):
    return '"' + str(value).replace('"', '""') + '"'


def prepare(stage: Path, output: Path):
    stage = stage.resolve()
    files = sorted(p for p in stage.rglob('*') if p.is_file() and p.relative_to(stage).parts[0] != 'data')
    sources = {}
    entries = []
    duplicates = []
    for path in files:
        relative = path.relative_to(stage)
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        key = (path.stat().st_size, digest)
        source = sources.setdefault(key, path)
        parent = str(relative.parent).replace('/', '\\')
        destination = '{app}' + ('\\' + parent if parent != '.' else '')
        entries.append(f'Source: {quoted(source)}; DestDir: {quoted(destination)}; DestName: {quoted(path.name)}; Flags: ignoreversion')
        if source != path:
            duplicates.append({'path': relative.as_posix(), 'source': source.relative_to(stage).as_posix(), 'bytes': key[0]})
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text('\n'.join(entries) + '\n', encoding='utf-8-sig')
    report = {'files': len(files), 'unique_files': len(sources), 'input_bytes': sum(p.stat().st_size for p in files),
              'duplicate_bytes': sum(item['bytes'] for item in duplicates), 'duplicates': duplicates}
    output.with_suffix('.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    report = prepare(args.stage, args.output)
    print(json.dumps({key: value for key, value in report.items() if key != 'duplicates'}))
