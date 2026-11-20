import io
import os
import shutil
import sys
import time
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from taskrunner import agent
from taskrunner.agent import PowerShellSession, run_agent
from taskrunner.app import create_app
from taskrunner.oscompat import IS_WINDOWS

HAVE_POWERSHELL = bool(shutil.which("pwsh") or shutil.which("powershell"))
DEFAULT_SHELL = "powershell" if IS_WINDOWS else "bash"
TOOL_NAME = {"bash": "bash", "powershell": "powershell"}

# Each scenario, written for each shell.
CMDS = {
    "bash": {
        "make": "mkdir -p out && cd out && echo 42 > answer.txt && pwd",
        "read": "cat answer.txt",
        "noop": "true",
    },
    "powershell": {
        "make": "New-Item -ItemType Directory out | Out-Null; Set-Location out; "
                "Set-Content -Path answer.txt -Value 42; Get-Location",
        "read": "Get-Content answer.txt",
        "noop": "$null",
    },
}

shells = []
if not IS_WINDOWS:
    shells.append("bash")
if HAVE_POWERSHELL:
    shells.append("powershell")


def tool_use(id, name, **inp):
    return NS(type="tool_use", id=id, name=name, input=inp)


def text(t):
    return NS(type="text", text=t)


class FakeStream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        for b in self.msg.content:
            if b.type == "text":
                yield NS(type="content_block_delta", delta=NS(type="text_delta", text=b.text))
        yield NS(type="message_stop")

    def get_final_message(self):
        return self.msg


class FakeClient:
    """Replays scripted assistant turns and records every request."""

    def __init__(self, turns):
        self.turns, self.requests = list(turns), []
        self.beta = NS(messages=NS(stream=self._stream))

    def _stream(self, **kw):
        self.requests.append({**kw, "messages": list(kw["messages"])})
        return FakeStream(self.turns.pop(0))


@pytest.mark.parametrize("shell", shells)
def test_agent_uses_tools_and_finishes(tmp_path, shell):
    sh, c = TOOL_NAME[shell], CMDS[shell]
    client = FakeClient([
        NS(stop_reason="tool_use", content=[
            text("Setting up."),
            tool_use("t1", sh, command=c["make"]),
            tool_use("t2", "str_replace_based_edit_tool", command="create", path="notes.md", file_text="# Notes ✓\n"),
        ]),
        NS(stop_reason="tool_use", content=[
            tool_use("t3", sh, command=c["read"]),  # current directory carried over into out/
            tool_use("t4", "str_replace_based_edit_tool", command="view", path=str(tmp_path.parent / "elsewhere.txt")),
        ]),
        NS(stop_reason="end_turn", content=[text("Done: wrote out/answer.txt and notes.md")]),
    ])
    log = io.StringIO()
    assert run_agent("make files", tmp_path, client=client, out=log, shell=shell) == 0

    assert (tmp_path / "out" / "answer.txt").read_text().strip() == "42"
    assert (tmp_path / "notes.md").read_text(encoding="utf-8") == "# Notes ✓\n"

    # Turn 3's request carries both results from turn 2, each matched to its tool_use id.
    results = client.requests[2]["messages"][-1]["content"]
    by_id = {r["tool_use_id"]: r for r in results}
    assert by_id["t3"]["content"].strip() == "42"
    assert by_id["t4"]["is_error"] and "outside the workspace" in by_id["t4"]["content"]

    req = client.requests[0]
    assert req["model"] == "claude-opus-5-5"
    assert {sh, "str_replace_based_edit_tool", "open_in_browser"} <= {t["name"] for t in req["tools"]}
    assert str(tmp_path) in req["system"]
    assert ("PowerShell" if shell == "powershell" else "bash") in req["system"]
    assert "Done: wrote" in log.getvalue() and f"▶ {shell}" in log.getvalue()


def test_default_shell_matches_os(tmp_path):
    client = FakeClient([NS(stop_reason="end_turn", content=[text("ok")])])
    run_agent("x", tmp_path, client=client, out=io.StringIO())
    names = {t["name"] for t in client.requests[0]["tools"]}
    assert TOOL_NAME[DEFAULT_SHELL] in names


def test_agent_rejects_cut_off_tool_input(tmp_path):
    client = FakeClient([
        NS(stop_reason="tool_use", content=[tool_use("t1", TOOL_NAME[DEFAULT_SHELL])]),  # no command
        NS(stop_reason="end_turn", content=[text("gave up")]),
    ])
    assert run_agent("x", tmp_path, client=client, out=io.StringIO()) == 0
    result = client.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] and "missing 'command'" in result["content"]


def test_agent_refusal_fails(tmp_path):
    client = FakeClient([NS(stop_reason="refusal", content=[])])
    assert run_agent("x", tmp_path, client=client, out=io.StringIO()) == 1


@pytest.mark.skipif(IS_WINDOWS and not HAVE_POWERSHELL, reason="no shell available")
def test_agent_turn_limit(tmp_path):
    sh = TOOL_NAME[DEFAULT_SHELL]
    loop = [NS(stop_reason="tool_use", content=[tool_use(f"t{i}", sh, command=CMDS[DEFAULT_SHELL]["noop"])])
            for i in range(3)]
    log = io.StringIO()
    assert run_agent("x", tmp_path, max_turns=3, client=FakeClient(loop), out=log) == 1
    assert "limit of 3 turns" in log.getvalue()


@pytest.mark.skipif(not HAVE_POWERSHELL, reason="PowerShell not installed")
def test_powershell_session(tmp_path, monkeypatch):
    ps = PowerShellSession(tmp_path)
    assert ps.run("Write-Output 'héllo ✓'").strip() == "héllo ✓"  # UTF-8 round trip
    ps.run("New-Item -ItemType Directory sub | Out-Null; Set-Location sub")
    assert ps.run("(Get-Location).ProviderPath").strip() == str(tmp_path / "sub")  # cwd persists
    assert "[exit code 3]" in ps.run("exit 3")
    out = ps.run("Write-Output before; Write-Error 'boom'; Write-Output after")  # errors are readable text
    assert "before" in out and "ERROR: boom" in out and "after" in out and "[exit code 1]" in out
    assert "CLIXML" not in out and "\x1b" not in out
    out = ps.run("throw 'fatal'")  # terminating errors too
    assert "ERROR: fatal" in out and "[exit code 1]" in out and "CLIXML" not in out
    assert "WARNING: careful" in ps.run("Write-Warning careful")
    py = f"& '{sys.executable}' -c \"import sys; sys.exit(5)\""
    assert "[exit code 5]" in ps.run(py)  # native program exit codes
    ps.close()
    assert ps.run("(Get-Location).ProviderPath").strip() == str(tmp_path)  # restart resets cwd

    monkeypatch.setattr(agent, "CMD_TIMEOUT", 2)
    t0 = time.time()
    assert "timed out" in ps.run("Start-Sleep -Seconds 60")
    assert time.time() - t0 < 30


def test_agent_job_via_api_without_credentials(tmp_path, monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # no ~/.config/anthropic profile
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # (Windows home)
    with TestClient(create_app(tmp_path / "data")) as c:
        assert c.post("/api/jobs", json={"kind": "agent"}).status_code == 422
        j = c.post("/api/jobs", json={"kind": "agent", "task": "Write hello.txt"}).json()
        assert j["kind"] == "agent" and j["name"] == "Write hello.txt"
        end = time.time() + 30
        while c.get(f"/api/jobs/{j['id']}").json()["status"] in ("queued", "running"):
            assert time.time() < end
            time.sleep(0.1)
        assert c.get(f"/api/jobs/{j['id']}").json()["status"] == "failed"
        assert "ANTHROPIC_API_KEY" in c.get(f"/api/jobs/{j['id']}/logs").json()["text"]

        files = c.get(f"/api/jobs/{j['id']}/files").json()
        ws = files["workspace"]
        assert os.path.isdir(ws) and files["files"] == []
        with open(os.path.join(ws, "r.txt"), "w") as f:
            f.write("hi")
        assert c.get(f"/api/jobs/{j['id']}/files").json()["files"][0]["path"] == "r.txt"
        assert c.get(f"/api/jobs/{j['id']}/files/r.txt").text == "hi"
        assert c.get(f"/api/jobs/{j['id']}/files/..%2F..%2Fjobs.db").status_code == 404


# ---------------------------------------------------------------- web + tabs

from taskrunner.agent import TabOpener


def test_open_in_browser_tool(tmp_path):
    opened = []
    tabs = TabOpener(limit=2, opener=lambda url: opened.append(url) or True)
    client = FakeClient([
        NS(stop_reason="tool_use", content=[
            tool_use("a", "open_in_browser", url="https://example.com/one"),
            tool_use("b", "open_in_browser", url="file:///etc/passwd"),          # not a web page
            tool_use("c", "open_in_browser", url="javascript:alert(1)"),         # not a web page
            tool_use("d", "open_in_browser", url="http://example.com/two"),
            tool_use("e", "open_in_browser", url="https://example.com/three"),   # over the limit
        ]),
        NS(stop_reason="end_turn", content=[text("opened what I could")]),
    ])
    log = io.StringIO()
    assert run_agent("open links", tmp_path, client=client, out=log, tabs=tabs, web=False) == 0
    assert opened == ["https://example.com/one", "http://example.com/two"]
    res = {r["tool_use_id"]: r for r in client.requests[1]["messages"][-1]["content"]}
    assert not res["a"].get("is_error") and not res["d"].get("is_error")
    assert res["b"]["is_error"] and res["c"]["is_error"] and "http" in res["b"]["content"]
    assert res["e"]["is_error"] and "tab limit" in res["e"]["content"]
    assert "🗂 open tab: https://example.com/one" in log.getvalue()


def test_web_tools_offered_and_logged(tmp_path):
    client = FakeClient([
        NS(stop_reason="end_turn", content=[
            NS(type="server_tool_use", id="s1", name="web_search", input={"query": "best hiking boots"}),
            NS(type="web_search_tool_result", tool_use_id="s1", content=[]),
            NS(type="server_tool_use", id="s2", name="web_fetch", input={"url": "https://example.com/boots"}),
            NS(type="web_fetch_tool_result", tool_use_id="s2",
               content=NS(type="web_fetch_tool_error", error_code="url_not_accessible")),
            text("Here is what I found."),
        ]),
    ])
    log = io.StringIO()
    assert run_agent("research boots", tmp_path, client=client, out=log, web=True) == 0
    types = {t.get("type") for t in client.requests[0]["tools"]}
    assert {"web_search_20260209", "web_fetch_20260209"} <= types
    assert "web_fetch" in client.requests[0]["system"]
    out = log.getvalue()
    assert "🌐 web_search: best hiking boots" in out and "🌐 web_fetch: https://example.com/boots" in out
    assert "could not fetch page (url_not_accessible)" in out


def test_web_tools_can_be_turned_off(tmp_path):
    client = FakeClient([NS(stop_reason="end_turn", content=[text("ok")])])
    run_agent("x", tmp_path, client=client, out=io.StringIO(), web=False)
    names = {t["name"] for t in client.requests[0]["tools"]}
    assert "web_search" not in names and "web_fetch" not in names and "open_in_browser" in names
