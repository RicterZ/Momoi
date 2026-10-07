"""Pin the compressed optional ASR installer before compiling the core installer."""
import argparse
import hashlib
import json
from pathlib import Path


def pin(archive, stage, output, version):
    identity = (stage / 'runtime/asr/component-id.txt').read_text().strip()
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    name = f'Momoi-ASR-Components-{version}-{identity}-{digest[:16]}-x64.exe'
    target = archive.with_name(name)
    if archive != target: archive.replace(target)
    metadata = {'id': identity, 'version': version, 'filename': name,
                'sha256': digest, 'bytes': target.stat().st_size}
    target.with_suffix('.json').write_text(json.dumps(metadata, indent=2))
    constants = {'ASRPackageName': name, 'ASRComponentId': identity, 'ASRPackageSHA256': digest,
                 'ASRPackageURL': 'https://momoi-1253047877.cos.ap-guangzhou.myqcloud.com/windows/components/' + name}
    output.write_text('\n'.join(f'#define {k} "{v}"' for k,v in constants.items())+'\n')

if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--archive',type=Path,required=True)
    p.add_argument('--stage',type=Path,default=Path('build/windows-asr-stage'))
    p.add_argument('--output',type=Path,default=Path('build/windows-asr-pin.iss'))
    p.add_argument('--version',required=True)
    a=p.parse_args(); pin(a.archive,a.stage,a.output,a.version)
