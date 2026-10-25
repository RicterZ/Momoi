"""SnowLuma calls reuse QQ's own session and the existing media protocol."""
import importlib.util
import inspect
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT / 'infra/snowluma-call'


def load_script(name):
    spec = importlib.util.spec_from_file_location('snowcall_' + name, INFRA / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_napcat_and_snowluma_use_shared_bridge_patcher():
    module = load_script_from(ROOT / 'infra/napcat-call/prepare.py')
    assert Path(inspect.getsourcefile(module.patch_bridge)) == ROOT / 'infra/qq-call/bridge_patch.py'


def load_script_from(path):
    spec = importlib.util.spec_from_file_location('infra_' + path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_qq_session_capture():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required to exercise QQ session capture')
    for name in ('session.cjs', 'loader.cjs'):
        subprocess.run([node, '--check', str(INFRA / name)], check=True, capture_output=True)
    subprocess.run([node, '--test', str(ROOT / 'tests/snowluma_call.test.cjs')], check=True,
                   capture_output=True, text=True)


def test_prepare_removes_base_image_qq_update_block(tmp_path):
    module = load_script('prepare')
    startup = tmp_path / 'start.sh'
    startup.write_text('#!/bin/bash\n' + module.QQ_UPDATE_BLOCK + 'exec supervisord\n')

    module.remove_qq_update_block(startup)

    assert startup.read_text() == '#!/bin/bash\nexec supervisord\n'
    module.remove_qq_update_block(startup)


@pytest.mark.parametrize("main", ["./main.js", "./application.asar/app_launcher/index.js"])
def test_prepare_preserves_original_main_and_patches_bridge(tmp_path, main):
    root = tmp_path / 'call'
    qq = tmp_path / 'qq'
    qq.mkdir()
    (qq / 'main.js').write_text('// original')
    (qq / 'application.asar').write_bytes(b'archive')
    package = {'main': main, 'name': 'QQ'}
    (qq / 'package.json').write_text(json.dumps(package))
    (root / 'qq-call').mkdir(parents=True)
    shutil.copyfile(ROOT / 'infra/qq-call/bridge_patch.py', root / 'qq-call/bridge_patch.py')
    plugin = root / 'upstream/bridge/napcat-plugin/index.mjs'
    plugin.parent.mkdir(parents=True)
    # Version-checked anchors from the pinned bridge. Missing anchors must fail builds.
    plugin.write_text('''let acceptTimer = null;
  if (!Array.isArray(activeSDKInvite) || state.call.phase === "ended") return;
    if (req.method === "GET" && url.pathname === "/v1/status") {
(command === 20050 || command === 120043)
    loginPosted: false,
    state.avHost.loginPosted = false;
  state.avHost.lastOutputCommand = command;
    logger.error(`[MaiBotQQCall] bridge startup failed: ${state.listenerError}`);
''')
    load_script('prepare').prepare(root, qq)
    assert (qq / 'main.js').read_text() == '// original'
    assert json.loads((qq / 'package.json').read_text()) == {**package, 'main': './momoi-call.cjs'}
    entry = (qq / 'momoi-call.cjs').read_text()
    assert 'MAIBOT_QQ_CALL_AV_HOST !== "1"' in entry
    assert 'require(' + json.dumps('./' + str(Path(main))) + ')' in entry
    assert 'momoi/ready' in plugin.read_text()
    assert 'loginSucceeded' in plugin.read_text()
    assert 'callerUin !== momoiReady.ownerUin' in plugin.read_text()


@pytest.mark.parametrize('main', ['/absolute.js', '../escape.js', './missing.js', './momoi-call.cjs'])
def test_prepare_rejects_unknown_entry_before_editing(tmp_path, main):
    root = tmp_path / 'call'
    (root / 'qq-call').mkdir(parents=True)
    shutil.copyfile(ROOT / 'infra/qq-call/bridge_patch.py', root / 'qq-call/bridge_patch.py')
    qq = tmp_path / 'qq'
    qq.mkdir()
    original = json.dumps({'main': main})
    (qq / 'package.json').write_text(original)
    with pytest.raises(ValueError, match='Unsupported QQ'):
        load_script('prepare').prepare(root, qq)
    assert (qq / 'package.json').read_text() == original


def test_private_runtime_preserves_token_and_clears_stale_pulse(tmp_path, monkeypatch):
    module = load_script('runtime')
    scripts = tmp_path / 'scripts-source'
    scripts.mkdir()
    (scripts / 'audio-control.sh').write_text('pulseaudio --daemonize=yes')
    (scripts / 'common.sh').write_text('# shared')
    real_path = module.Path
    monkeypatch.setattr(module, 'Path', lambda name: scripts if str(name) ==
                        '/opt/qq-call/upstream/bridge/scripts' else real_path(name))
    runtime = tmp_path / 'runtime'
    module.initialize(runtime)
    token = runtime / 'runtime/control.token'
    original = token.read_text()
    assert len(original.strip()) >= 32
    assert token.stat().st_mode & 0o777 == 0o600
    pulse = runtime / 'runtime/pulse'
    pulse.mkdir()
    (pulse / 'pid').write_text('123')
    (pulse / 'native').touch()
    module.initialize(runtime)
    assert token.read_text() == original
    assert not (pulse / 'pid').exists() and not (pulse / 'native').exists()
    assert '--daemonize=no' in (runtime / 'scripts/audio-control.sh').read_text()
    token.write_text('short')
    with pytest.raises(ValueError, match='shorter'):
        module.initialize(runtime)
