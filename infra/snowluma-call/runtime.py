"""Seed private media state before SnowLuma's normal UID and display setup."""
import os
from pathlib import Path
import secrets
import shutil


def initialize(runtime):
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    private = runtime / 'runtime'
    private.mkdir(exist_ok=True, mode=0o700)
    private.chmod(0o700)
    (runtime / 'logs').mkdir(exist_ok=True, mode=0o700)
    token = private / 'control.token'
    if not token.exists():
        with token.open('x') as stream:
            stream.write(secrets.token_hex(32) + '\n')
    token.chmod(0o600)
    if len(token.read_text().strip().encode()) < 32:
        raise ValueError('Existing call token is shorter than 32 bytes')
    scripts = runtime / 'scripts'
    scripts.mkdir(exist_ok=True)
    for name in ('audio-control.sh', 'common.sh'):
        target = scripts / name
        source = Path('/opt/qq-call/upstream/bridge/scripts') / name
        content = source.read_text()
        if name == 'audio-control.sh':
            if content.count('--daemonize=yes') != 1:
                raise ValueError('Upstream PulseAudio launcher changed')
            content = content.replace('--daemonize=yes', '--daemonize=no')
        target.write_text(content)
        target.chmod(0o755)
    # A container restart preserves files, not PulseAudio's process.
    for name in ('pid', 'native'):
        (private / 'pulse' / name).unlink(missing_ok=True)


if __name__ == '__main__':
    initialize(Path(os.environ['QQ_CALL_RUNTIME']))
    shutil.copyfile('/opt/qq-call/infra/services.conf',
                    '/etc/supervisor/conf.d/qq-call.conf')
