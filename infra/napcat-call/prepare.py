"""Apply version-checked upstream additions; preserve its original entrypoint."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'qq-call'))
from bridge_patch import patch_bridge


def prepare():
    root = Path('/opt/qq-call')
    plugin = root / 'upstream/bridge/napcat-plugin/index.mjs'
    plugin.write_text(patch_bridge(plugin.read_text()))
    entry = Path('/app/entrypoint.sh')
    s = entry.read_text()
    needle = 'cd /app/napcat'
    assert needle in s
    s = s.replace(needle, 'bash /opt/qq-call/infra/bootstrap.sh\n' + needle, 1)
    (root / 'entrypoint-upstream.sh').write_text(s)


if __name__ == '__main__':
    prepare()
