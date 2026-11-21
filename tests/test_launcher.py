import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from taskrunner.cli import PORTS
from taskrunner.launcher import APP_DIR, load_dotenv, running_port


def test_dotenv(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('﻿# my keys\nANTHROPIC_API_KEY="sk-ant-abc"\nexport FOO = bar \nEMPTY=\nnot a line\nKEEP=new\n',
                   encoding="utf-8")
    for k in ("ANTHROPIC_API_KEY", "FOO", "EMPTY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("KEEP", "old")
    load_dotenv(env)
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-abc"  # quotes and BOM handled
    assert os.environ["FOO"] == "bar" and os.environ["EMPTY"] == ""
    assert os.environ["KEEP"] == "old"  # a real environment variable wins
    load_dotenv(tmp_path / "missing.env")  # no file: no error


def _post(port, path):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST",
                                 headers={"Origin": "http://berb.localhost"})
    return json.loads(urllib.request.urlopen(req, timeout=5).read())


@pytest.mark.skipif(running_port(PORTS) is not None, reason="a B.E.R.B is already running on this computer")
def test_open_starts_in_background_once_and_turns_off(tmp_path):
    env = {**os.environ, "TASKRUNNER_DATA": str(tmp_path)}
    run = lambda *a: subprocess.run([sys.executable, "-m", "taskrunner.cli", *a], cwd=APP_DIR, env=env,
                                    capture_output=True, text=True, timeout=90)
    first = run("open", "--no-window")
    assert first.returncode == 0, first.stdout + first.stderr
    port = running_port(PORTS)
    try:
        assert port is not None
        assert "running at" in (tmp_path / "server.log").read_text()
        again = run("open", "--no-window")  # already running: reuses it
        assert again.returncode == 0 and running_port(PORTS) == port
        assert "already running" in run("serve").stdout
    finally:
        if port:
            assert _post(port, "/api/quit") == {"ok": True}
    end = time.time() + 20
    while running_port(PORTS) is not None:
        assert time.time() < end, "B.E.R.B didn't turn off"
        time.sleep(0.3)


@pytest.mark.skipif(os.name != "nt", reason="Windows shortcuts")
def test_windows_shortcuts(tmp_path):
    from taskrunner.launcher import ICON, create_shortcuts
    made = create_shortcuts([tmp_path / "Desktop", tmp_path / "Programs"])
    assert [p.name for p in made] == ["B.E.R.B.lnk", "B.E.R.B.lnk"] and all(p.exists() for p in made)
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:L);"
          "Write-Output $s.TargetPath; Write-Output $s.Arguments; Write-Output $s.IconLocation")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], env={**os.environ, "L": str(made[0])},
                         capture_output=True, text=True).stdout.splitlines()
    assert out[0].lower().endswith("pythonw.exe")  # no console window
    assert out[1] == "-m taskrunner.cli open"
    assert str(ICON).lower() in out[2].lower()
