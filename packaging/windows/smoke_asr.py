"""Run in the private Windows interpreter with the optional component installed."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import tarfile


def run(install, source, archive, evidence, bge_model=None):
    os.environ['MOMOI_INSTALL_DIR'] = str(install)
    sys.path.insert(0, str(source / 'src'))
    from momoi.integrations.adapters.sherpa import SherpaASRProvider
    from momoi.integrations.contracts.asr import AudioInput
    encoder = None
    if bge_model is not None:
        from momoi.integrations.adapters.local_embedding import load_encoder
        encoder = load_encoder(bge_model)
        assert len(list(encoder.embed(['ASR 初始化前的记忆测试']))[0]) == 512
    with tarfile.open(archive) as package:
        member = next(m for m in package.getmembers() if m.name.endswith('/test_wavs/0.wav'))
        wav = package.extractfile(member).read()
    async def probe():
        provider = SherpaASRProvider()
        try:
            text = await provider.transcribe(AudioInput(wav, 'wav'))
            assert text, 'Native Windows ASR returned empty text'
            stream = await provider.create_stream()
            await stream.feed(bytes(640))
            await stream.close()
            again = await provider.transcribe(AudioInput(wav, 'wav'))
            assert again == text, (again, text)
            return {'ok': True, 'text': text, 'sessionIsolation': True, 'modelPath': provider.model_path}
        finally:
            await provider.close()
    result = asyncio.run(probe())
    if encoder is not None:
        assert len(list(encoder.embed(['ASR 初始化后的记忆测试']))[0]) == 512
        result['bgeCoexistence'] = True
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--install',type=Path,required=True)
    p.add_argument('--source',type=Path,required=True)
    p.add_argument('--archive',type=Path,required=True)
    p.add_argument('--evidence',type=Path,required=True)
    p.add_argument('--bge-model',type=Path)
    a=p.parse_args(); run(a.install,a.source,a.archive,a.evidence,a.bge_model)
