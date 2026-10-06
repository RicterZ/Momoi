"""Bounded Windows native AVSDK host probe; does not log in or place calls."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[3]
TARGET = ROOT / 'build/windows-call-probe'
TARGET.mkdir(parents=True, exist_ok=True)
REPORT = TARGET / 'report.json'
result = {'platform': sys.platform, 'dll_load': False, 'exports': {}, 'host': None}


def main():
    component = json.loads((ROOT / 'packaging/windows/components.json').read_text())['napcat']['qq_native']
    result['qq_version'] = component['version']
    installer = TARGET / 'QQ.exe'
    with urllib.request.urlopen(component['url'], timeout=120) as source, installer.open('wb') as target:
        shutil.copyfileobj(source, target)
    with installer.open('rb') as stream:
        actual = hashlib.file_digest(stream, 'sha256').hexdigest()
    assert actual == component['sha256'], actual
    result['installer_sha256'] = actual
    extracted = TARGET / 'qq'
    subprocess.run(['7z', 'x', '-y', '-o' + str(extracted), str(installer)], check=True, stdout=subprocess.DEVNULL)
    packages = list(extracted.glob('Files/versions/*/resources/app/package.json'))
    assert len(packages) == 1, packages
    package = packages[0]
    app = package.parent
    avsdk = app / 'avsdk/AVSDKPlugin.dll'
    result['avsdk_bytes'] = avsdk.stat().st_size
    # Native SDK constructors/teardown may terminate their process. Isolate
    # inspection so a native exit cannot terminate the diagnostic runner.
    native_report = TARGET / 'native-report.json'
    native_report.unlink(missing_ok=True)
    native = subprocess.run([sys.executable, str(Path(__file__).parent / 'native_probe.py'),
        str(app), str(extracted / 'Files'), str(native_report)], timeout=30)
    result['native_probe_exit'] = native.returncode
    if native_report.exists():
        result.update(json.loads(native_report.read_text(encoding='utf-8')))
    probe_app = TARGET / 'probe-app'
    probe_app.mkdir(exist_ok=True)
    for name in ('host.cjs', 'host.html'):
        shutil.copy2(Path(__file__).parent / name, probe_app / name)
    data = json.loads(package.read_text(encoding='utf-8'))
    result['original_main'] = data['main']
    # Windows QQ ignores normal external-app CLI arguments. Use the same
    # manifest redirection used by the official pinned NapCat shell-loader.
    data['main'] = './loadNapCat.js'
    patch_package = TARGET / 'qqnt.json'
    patch_package.write_text(json.dumps(data), encoding='utf-8')
    bootstrap = app / 'loadNapCat.js'
    bootstrap.write_text('require(' + json.dumps(str(probe_app / 'host.cjs')) + ');', encoding='utf-8')
    loaders = {
        'NapCatWinBootMain.exe': 'bd8ec316582bfb25e3b9b7f27a22c91437584f29879950462f789fd7bb111949',
        'NapCatWinBootHook.dll': '962bd5caf59e9792c37eed99c9130bf1464d619d0209820be570874017d66925',
    }
    loader_dir = TARGET / 'loader'
    loader_dir.mkdir(exist_ok=True)
    for name, expected in loaders.items():
        destination = loader_dir / name
        url = 'https://raw.githubusercontent.com/NapNeko/NapCatQQ/v4.18.30/packages/napcat-shell-loader/' + name
        with urllib.request.urlopen(url, timeout=60) as source, destination.open('wb') as target:
            shutil.copyfileobj(source, target)
        if hashlib.sha256(destination.read_bytes()).hexdigest() != expected:
            raise ValueError('Loader hash mismatch: ' + name)
    result['loader_hashes'] = loaders
    profile = TARGET / 'isolated-profile'
    profile.mkdir(exist_ok=True)
    host_report = TARGET / 'host-report.json'
    env = {**os.environ, 'MOMOI_PROBE_AVSDK': str(avsdk), 'MOMOI_PROBE_REPORT': str(host_report), 'MOMOI_PROBE_PROFILE': str(profile)}
    env.pop('ELECTRON_RUN_AS_NODE', None)
    env['NAPCAT_PATCH_PACKAGE'] = str(patch_package)
    env['NAPCAT_LOAD_PATH'] = str(bootstrap)
    host_report.unlink(missing_ok=True)

    exe = extracted / 'Files/QQ.exe'
    with (TARGET / 'host.log').open('wb') as log:
        process = subprocess.Popen([str(loader_dir / 'NapCatWinBootMain.exe'), str(exe), str(loader_dir / 'NapCatWinBootHook.dll')], cwd=exe.parent, env=env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline and not host_report.exists():
                if process.poll() is not None:
                    break
                time.sleep(.25)
            result['launcher_exit'] = process.poll()
            if host_report.exists():
                result['host'] = json.loads(host_report.read_text())
            else:
                result['host_error'] = 'No host report from redirected QQ host'
        finally:
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    result['assets'] = {str(path.relative_to(app / 'avsdk')): path.stat().st_size for path in (app / 'avsdk').rglob('*') if path.is_file()}


try:
    main()
except Exception as error:
    result['probe_error'] = repr(error)
finally:
    REPORT.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2), flush=True)

