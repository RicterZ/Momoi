"""Load the shipped native QQ runtime without logging in or contacting QQ."""
import argparse
import ctypes
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory


def check(runtime: Path, preload: Path):
    runtime = runtime.resolve()
    with TemporaryDirectory(prefix='Momoi QQ 原生测试 ') as temporary:
        data = Path(temporary) / 'qq'
        env = {**os.environ, 'MOMOI_NAPCAT_RUNTIME': str(runtime), 'MOMOI_QQ_DATA': str(data)}
        env['PATH'] = str(runtime) + os.pathsep + env.get('PATH', '')
        script = '''
const assert = require('node:assert/strict');
const wrapper = { exports: {} };
process.dlopen(wrapper, process.env.NAPCAT_WRAPPER_PATH);
assert.equal(wrapper.exports.NodeQQNTWrapperUtil.getNTUserDataInfoConfig(), process.env.MOMOI_QQ_DATA);
console.log('PASS: real QQ native library loaded and data isolated');
// Loading the native QQ library creates persistent handles even without login.
process.exit(0);
'''
        if os.name == 'nt':
            # Check Windows loader availability, including server Media Foundation.
            handles = []
            with os.add_dll_directory(str(runtime)), os.add_dll_directory(str(runtime / 'win64')):
                for library in ['mfplat.dll', 'mf.dll', 'mfreadwrite.dll', 'MSVCP140.dll', 'VCRUNTIME140.dll', 'VCRUNTIME140_1.dll'] + [str(path) for path in sorted(runtime.glob('*.dll'))] + [str(path) for path in sorted((runtime / 'win64').glob('*.dll'))]:
                    try:
                        handles.append(ctypes.WinDLL(library))
                        print('DLL available:', Path(library).name, flush=True)
                    except OSError as error:
                        print('DLL unavailable:', Path(library).name, error, flush=True)
        subprocess.run([str(runtime / 'node.exe'), '--require', str(preload.resolve()), '-e', script], cwd=runtime, env=env, check=True, timeout=30)
        assert (data / '.native-data-ready').read_text(encoding='utf-8') == str(data)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--preload', type=Path, default=Path('desktop/python/momoi_desktop/napcat_entry.cjs'))
    args = parser.parse_args()
    check(args.runtime, args.preload)
