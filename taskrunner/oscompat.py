"""The few places where Windows and macOS/Linux differ: which shell runs jobs, and how a
job's whole process tree is started and stopped."""
from __future__ import annotations

import base64
import os
import shutil
import signal
import subprocess
import sys

IS_WINDOWS = sys.platform == "win32"
SHELL_NAME = "PowerShell" if IS_WINDOWS else "bash"


def powershell_exe() -> str:
    """PowerShell 7 (`pwsh`) if installed, else the Windows PowerShell that ships with Windows."""
    return shutil.which("pwsh") or shutil.which("powershell") or "powershell"


def powershell_argv(script: str) -> list[str]:
    """Run a PowerShell script. -EncodedCommand avoids every quoting problem; output is UTF-8."""
    # UTF-8 without a byte-order mark, for both PowerShell's own output and programs it runs
    # (can fail when no console is attached; output is then still readable, just not forced to UTF-8)
    script = ("try { $OutputEncoding = [Console]::OutputEncoding = "
              "New-Object System.Text.UTF8Encoding $false } catch {}\n" + script)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    return [powershell_exe(), "-NoLogo", "-NoProfile", "-NonInteractive", "-OutputFormat", "Text",
            "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded]


def powershell_wrap(command: str) -> str:
    """Wrap a command so everything it reports comes out as plain text on stdout.

    Without this PowerShell writes errors to stderr as CLIXML (XML with colour codes), which is
    unreadable in a log. Afterwards $__trCode holds the exit status: the last native program's
    exit code, else 1 if any error was reported, else 0. `exit N` in the command still exits."""
    return (
        "$ProgressPreference = 'SilentlyContinue'\n"
        "if ($PSStyle) { $PSStyle.OutputRendering = 'PlainText' }\n"
        "$global:__trErr = $false\n"
        "$global:LASTEXITCODE = 0\n"
        "try {\n"
        "& {\n" + command + "\n} *>&1 | ForEach-Object {\n"
        "  if ($_ -is [System.Management.Automation.ErrorRecord]) { $global:__trErr = $true; \"ERROR: $_\" }\n"
        "  elseif ($_ -is [System.Management.Automation.WarningRecord]) { \"WARNING: $($_.Message)\" }\n"
        "  else { $_ }\n"
        "} | Out-String -Stream -Width 4096\n"
        "} catch { $global:__trErr = $true; \"ERROR: $_\" }\n"
        "$__trCode = if ($LASTEXITCODE) { $LASTEXITCODE } elseif ($global:__trErr) { 1 } else { 0 }\n"
    )


def powershell_job_argv(command: str) -> list[str]:
    """A shell job on Windows: run the command, exit with its status."""
    return powershell_argv(powershell_wrap(command) + "exit $__trCode\n")


def new_group_kwargs() -> dict:
    """Popen kwargs that put the child in its own process group, so we can stop its whole tree."""
    if IS_WINDOWS:  # own group, and no console window popping up (B.E.R.B may run without one)
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def kill_tree(pid: int, force: bool = False) -> None:
    """Stop a process and everything it started. On macOS/Linux, force=False asks politely (SIGTERM)."""
    if IS_WINDOWS:
        # Windows has no polite signal for console programs, so always force; /T takes the children too.
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        return
    try:
        os.killpg(pid, signal.SIGKILL if force else signal.SIGTERM)
    except ProcessLookupError:
        pass
