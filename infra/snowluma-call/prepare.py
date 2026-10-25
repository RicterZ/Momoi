"""Prepare the pinned QQ entrypoint without modifying SnowLuma's runtime."""
import importlib.util
import json
from pathlib import Path, PurePosixPath


def prepare(root=Path('/opt/qq-call'), qq=Path('/opt/QQ/resources/app')):
    spec = importlib.util.spec_from_file_location('bridge_patch', root / 'napcat-call/prepare.py')
    patch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(patch)
    package_path = qq / 'package.json'
    package = json.loads(package_path.read_text())
    original = package['main']
    main = PurePosixPath(original)
    # Python cannot stat Electron's virtual paths inside application.asar.
    exists = (qq / main).is_file() or (
        str(main) == 'application.asar/app_launcher/index.js' and
        (qq / 'application.asar').is_file())
    if (package.get('type') == 'module' or main.is_absolute() or '..' in main.parts
            or original == './momoi-call.cjs' or not exists):
        raise ValueError('Unsupported QQ main entry; review the pinned image')
    plugin = root / 'upstream/bridge/napcat-plugin/index.mjs'
    source = patch.patch_bridge(plugin.read_text())
    failure = '    logger.error(`[MaiBotQQCall] bridge startup failed: ${state.listenerError}`);'
    if source.count(failure) != 1:
        raise ValueError('Upstream bridge startup handling changed')
    source = source.replace(failure, failure + '\n    throw error;')
    plugin.write_text(source)
    (qq / 'momoi-call.cjs').write_text(
        'require("/opt/qq-call/infra/loader.cjs").start();\n'
        'if (process.env.MAIBOT_QQ_CALL_AV_HOST !== "1") require('
        + json.dumps('./' + str(main)) + ');\n')
    package['main'] = './momoi-call.cjs'
    package_path.write_text(json.dumps(package, indent=2) + '\n')


if __name__ == '__main__':
    prepare()
