"""Explicit shell selection and Unicode-safe Windows command transport."""

import base64
import os
from pathlib import Path

SHELL_NAME = "Windows PowerShell" if os.name == "nt" else "Bash"


def shell_argv(command: str) -> list[str]:
    if os.name != "nt":
        return ["bash", "-c", command]
    # EncodedCommand avoids command-line quoting and ANSI code-page conversion.
    # Preserve explicit exit codes and propagate native executable failures.
    script = (
        "$ErrorActionPreference = 'Stop'; "
        "$utf8 = New-Object System.Text.UTF8Encoding($false); "
        "[Console]::InputEncoding = $utf8; [Console]::OutputEncoding = $utf8; "
        "$OutputEncoding = $utf8; "
        + command
        + "\nif ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    return [str(executable), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
