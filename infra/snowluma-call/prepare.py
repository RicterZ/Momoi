"""Prepare the pinned QQ entrypoint without modifying SnowLuma's runtime."""
import json
from pathlib import Path, PurePosixPath
import sys


QQ_UPDATE_BLOCK = '''# Freeze the bundled QQ version. QQ has two update paths: the "立即更新" full-update
# dialog (click-gated -> never fires headless) and a SILENT hot-update channel
# (hotUpdateApi -> qqpatch.gtimg.cn) that swaps QQ files in the background. Only
# the silent one can change bytes without us, which would break the version-pinned
# nnphook. Black-hole that single host so the hot-update download can never land.
# QQ's main-process hot-update resolves via getaddrinfo, which honours /etc/hosts;
# Docker writes /etc/hosts once at container start and never rewrites it after, so
# this append is durable for the container's lifetime. Runs as root before QQ.
if ! grep -q 'qqpatch\\.gtimg\\.cn' /etc/hosts 2>/dev/null; then
  printf '0.0.0.0 qqpatch.gtimg.cn\\n' >> /etc/hosts
fi
'''


def remove_qq_update_block(startup=Path('/root/start.sh')):
    if not startup.exists():
        return
    source = startup.read_text()
    if QQ_UPDATE_BLOCK in source:
        startup.write_text(source.replace(QQ_UPDATE_BLOCK, '', 1))
    elif 'qqpatch.gtimg.cn' in source:
        raise ValueError('SnowLuma QQ update block changed; review before rebuilding')


def prepare(root=Path('/opt/qq-call'), qq=Path('/opt/QQ/resources/app'),
            startup=Path('/root/start.sh')):
    remove_qq_update_block(startup)
    sys.path.insert(0, str(root / 'qq-call'))
    from bridge_patch import patch_bridge
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
    source = patch_bridge(plugin.read_text())
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
