import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from aiohttp import web

from momoi.channel.napcat import NapCatChannel, NapCatConfig

ROOT = Path(__file__).resolve().parents[1]


def test_managed_qq_loader_isolates_native_data_without_modifying_bundle(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is needed to check the Windows QQ bootstrap')
    runtime = tmp_path / 'runtime 中文'
    (runtime / 'napcat').mkdir(parents=True)
    account_data = tmp_path / 'data 中文'
    (runtime / 'napcat/napcat.mjs').write_text('''
import assert from 'node:assert/strict';
const original = { exports: {} };
process.dlopen(original, '/other.node');
assert.equal(original.exports.NodeQQNTWrapperUtil.getNTUserDataInfoConfig(), 'system');
for (let i = 0; i < 2; i++) {
  const module = { exports: {} };
  process.dlopen(module, process.env.NAPCAT_WRAPPER_PATH);
  assert.equal(module.exports.NodeQQNTWrapperUtil.getNTUserDataInfoConfig(), process.env.MOMOI_QQ_DATA);
}
assert.equal(process.env.NAPCAT_DISABLE_PIPE, '1');
console.log('PASS');
''', encoding='utf-8')
    script = '''
process.env.MOMOI_NAPCAT_RUNTIME = process.argv[1];
process.env.MOMOI_QQ_DATA = process.argv[2];
const util = { getNTUserDataInfoConfig: () => 'system' };
process.dlopen = module => {
  const nativeUtil = Object.freeze({ getNTUserDataInfoConfig: () => 'system' });
  module.exports = Object.freeze({ NodeQQNTWrapperUtil: nativeUtil });
};
require(process.argv[3]);
import(require('node:url').pathToFileURL(require('node:path').join(process.argv[1], 'napcat', 'napcat.mjs')).href);
'''
    result = subprocess.run([node, '-e', script, str(runtime), str(account_data), str(ROOT / 'desktop/python/momoi_desktop/napcat_entry.cjs')], capture_output=True, text=True, encoding='utf-8', check=True)
    assert 'PASS' in result.stdout
    assert (account_data / ".native-data-ready").read_text(encoding="utf-8") == str(account_data)
    assert list(runtime.iterdir()) == [runtime / 'napcat']


@pytest.mark.parametrize('access_token,bot_qq,reported_qq,ready', [('secret', '12345', '12345', True), ('', '', '12345', True), ('secret', '12345', '99999', False)])
def test_qq_websocket_auth_and_account_identity(tmp_path, access_token, bot_qq, reported_qq, ready):
    async def check():
        received = asyncio.Event()
        allow_close = asyncio.Event()
        headers = []
    
        async def handler(request):
            headers.append(request.headers.get('Authorization'))
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_json({'post_type': 'meta_event', 'meta_event_type': 'lifecycle', 'self_id': reported_qq})
            received.set()
            await allow_close.wait()
            await ws.close()
            return ws
    
        app = web.Application()
        app.router.add_get('/', handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        config = NapCatConfig(f'ws://127.0.0.1:{port}', '54321', 1, 60, 30, 30, 1, access_token=access_token, bot_qq=bot_qq)
        channel = NapCatChannel(config)
        stop = asyncio.Event()
        async def on_event(event):
            pass
        task = asyncio.create_task(channel.run(on_event, stop))
        try:
            await asyncio.wait_for(received.wait(), 3)
            for _ in range(100):
                if channel._ready.is_set() == ready and (ready or channel._ws is None):
                    break
                await asyncio.sleep(.01)
            assert channel._ready.is_set() is ready
            assert channel.connected is ready
            assert headers[0] == (f'Bearer {access_token}' if access_token else None)
        finally:
            stop.set()
            allow_close.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await runner.cleanup()
    asyncio.run(check())


def test_qq_config_validates_bot_identity_and_masks_token(tmp_path):
    from momoi.config.manager import ConfigurationManager
    from momoi.config.workspace import bootstrap
    bootstrap(tmp_path / 'config.json')
    manager = ConfigurationManager(tmp_path / 'config.json')
    document = json.loads(manager.path.read_text())
    document['channels'] = {'primary': 'napcat', 'enabled': {'napcat': {'url': 'ws://127.0.0.1:3001', 'owner_qq': '54321', 'bot_qq': '12345', 'access_token': 'secret'}}}
    manager.path.write_text(json.dumps(document))
    assert manager.snapshot()['app']['channels']['enabled']['napcat']['access_token'] == {'$secret': 'keep'}
    parsed = NapCatConfig.from_mapping(document['channels']['enabled']['napcat'])
    assert parsed.bot_qq == '12345' and parsed.access_token == 'secret'
    with pytest.raises(ValueError, match='bot_qq'):
        NapCatConfig.from_mapping({'owner_qq': '54321', 'url': 'ws://localhost', 'bot_qq': '１２３４５'})


def test_runtime_reports_actual_qq_connection(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from momoi.runtime.supervisor import RuntimeSupervisor
    supervisor = RuntimeSupervisor(SimpleNamespace(revision=lambda: "saved"), factory=lambda _: None)
    assert supervisor.status()["qq_connected"] is False
    channel = NapCatChannel(NapCatConfig("ws://localhost", "12345", 1, 60, 30, 30, 1))
    supervisor.daemon = SimpleNamespace(channels={"napcat": channel})
    assert supervisor.status()["qq_connected"] is False
    channel._ws = Mock(closed=False)
    channel._ready.set()
    assert supervisor.status()["qq_connected"] is True
    channel._ws.closed = True
    assert supervisor.status()["qq_connected"] is False


def test_managed_call_loader_preserves_native_module_location(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('node is not installed')
    runtime = tmp_path / 'runtime'
    (runtime / 'napcat').mkdir(parents=True)
    data = tmp_path / 'data'
    plugin = tmp_path / 'plugin.mjs'
    plugin.write_text('export async function plugin_init(ctx) { console.log("PLUGIN",ctx.core.id); }')
    target = runtime / 'napcat/napcat.mjs'
    target.write_text('''
const a = false;
const V = {core:{id:42},async InitNapCat(){console.log("INIT");}};
const e = {log:console.log,logWarn:console.warn,logError:console.error};
for (let i=0;i<2;i++) {
a && (V.core.dbPassphrase = a), await V.InitNapCat();
}
console.log("LOCATION", import.meta.url);
''')
    script = '''
process.env.MOMOI_NAPCAT_RUNTIME=process.argv[1];
process.env.MOMOI_QQ_DATA=process.argv[2];
process.env.MOMOI_QQ_CALL_PLUGIN=process.argv[3];
require(process.argv[4]);
import(require('node:url').pathToFileURL(require('node:path').join(process.argv[1],'napcat','napcat.mjs')).href);
'''
    linked = tmp_path / 'linked-runtime'
    try:
        linked.symlink_to(runtime, target_is_directory=True)
    except OSError:
        # Windows runners may not grant symlink creation; still exercise the loader.
        linked = runtime
    result = subprocess.run([node, '-e', script, str(linked), str(data), str(plugin),
                             str(ROOT / 'desktop/python/momoi_desktop/napcat_entry.cjs')],
                            capture_output=True, text=True, check=True)
    assert result.stdout.index('INIT') < result.stdout.index('PLUGIN 42')
    assert result.stdout.count('PLUGIN 42') == 1
    assert target.as_uri() in result.stdout
    assert 'PLUGIN' not in target.read_text()
