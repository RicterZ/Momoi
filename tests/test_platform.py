import asyncio
import base64
import sys
from unittest.mock import patch

from momoi.platform.shell import shell_argv
from momoi.platform.signals import shutdown_signals
from momoi.tools.process import run_process


def test_windows_shell_preserves_unicode_and_quoting():
    command = 'Write-Output "桃井 $env:TEMP"; exit 7'
    with patch("momoi.platform.shell.os.name", "nt"), patch("momoi.platform.shell.Path", __import__('pathlib').PureWindowsPath):
        argv = shell_argv(command)
    assert argv[-2] == "-EncodedCommand"
    decoded = base64.b64decode(argv[-1]).decode("utf-16-le")
    assert command in decoded
    assert "UTF8Encoding" in decoded
    assert "-NoProfile" in argv


def test_windows_signal_fallback_restores_previous_handlers():
    async def exercise():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        with patch.object(loop, "add_signal_handler", side_effect=NotImplementedError), patch("momoi.platform.signals.signal.signal") as register:
            with shutdown_signals(stop):
                callback = register.call_args_list[0].args[1]
                callback(2, None)
                await asyncio.sleep(0)
                assert stop.is_set()
            assert register.call_count == 4
    asyncio.run(exercise())


def test_utf8_process_output_and_argv():
    result = asyncio.run(run_process([sys.executable, "-X", "utf8", "-c", "import sys; print(sys.argv[1]); print('错误', file=sys.stderr)", "桃井 $(literal)"], timeout=5))
    assert result["exit_code"] == 0
    assert result["stdout_tail"].strip() == "桃井 $(literal)"
    assert result["stderr_tail"].strip() == "错误"
