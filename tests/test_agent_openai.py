import io
import json
import time
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from taskrunner.agent_openai import run_agent_openai
from taskrunner.app import create_app
from taskrunner.oscompat import IS_WINDOWS
from tests.test_agent import CMDS, HAVE_POWERSHELL

SHELL = "powershell" if IS_WINDOWS else "bash"


def call(id, name, **args):
    return NS(id=id, function=NS(name=name, arguments=json.dumps(args)))


def reply(content=None, calls=None, finish="tool_calls"):
    return NS(choices=[NS(message=NS(content=content, tool_calls=calls), finish_reason=finish)])


class FakeOpenAI:
    def __init__(self, turns):
        self.turns, self.requests = list(turns), []
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kw):
        self.requests.append(json.loads(json.dumps(kw)))  # deep copy, and proves it's JSON-serializable
        return self.turns.pop(0)


class FakeBrowser:
    def run(self, inp):
        return [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "AAAA"}},
                {"type": "text", "text": "Title: Example"}]

    def close(self):
        pass


@pytest.mark.skipif(IS_WINDOWS and not HAVE_POWERSHELL, reason="no shell")
def test_openai_agent_uses_tools(tmp_path):
    c = CMDS[SHELL]
    client = FakeOpenAI([
        reply("Starting.", [
            call("c1", SHELL, command=c["make"]),
            call("c2", "str_replace_based_edit_tool", command="create", path="notes.md", file_text="hi ✓\n"),
        ]),
        reply(None, [
            call("c3", SHELL, command=c["read"]),
            NS(id="c4", function=NS(name="str_replace_based_edit_tool", arguments='{"command": "vi')),  # cut off
            call("c5", "browser", action="screenshot"),
        ]),
        reply("All done.", None, finish="stop"),
    ])
    log = io.StringIO()
    rc = run_agent_openai("make files", tmp_path, client=client, out=log, model="gpt-4o",
                          tabs=None, browser=FakeBrowser())
    assert rc == 0
    assert (tmp_path / "out" / "answer.txt").read_text().strip() == "42"
    assert (tmp_path / "notes.md").read_text(encoding="utf-8") == "hi ✓\n"

    first = client.requests[0]
    assert first["model"] == "gpt-4o"
    names = {t["function"]["name"] for t in first["tools"]}
    assert {SHELL, "str_replace_based_edit_tool", "open_in_browser", "browser"} <= names
    assert first["messages"][0]["role"] == "system" and "never complete a purchase" in first["messages"][0]["content"].lower()

    msgs = client.requests[2]["messages"]
    tool_msgs = {m["tool_call_id"]: m["content"] for m in msgs if m["role"] == "tool"}
    assert tool_msgs["c3"].strip() == "42"
    assert "valid JSON" in tool_msgs["c4"]
    assert "Title: Example" in tool_msgs["c5"]
    # the screenshot is sent as an image in a user message after the tool results
    assert msgs[-1]["role"] == "user" and msgs[-1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert not any(k.startswith("_") for m in msgs for k in m)  # private markers never sent
    # assistant turns carry their tool calls, so OpenAI can match the results
    asst = [m for m in msgs if m["role"] == "assistant"]
    assert asst[0]["tool_calls"][0]["id"] == "c1"
    out = log.getvalue()
    assert "Agent: OpenAI gpt-4o" in out and "All done." in out and "✔ Agent finished" in out


def test_openai_default_model_and_length(tmp_path):
    client = FakeOpenAI([reply("partial", None, finish="length"), reply("done", None, finish="stop")])
    assert run_agent_openai("x", tmp_path, client=client, out=io.StringIO(), browser=False) == 0
    assert client.requests[0]["model"] == "gpt-4o"
    assert "continue" in client.requests[1]["messages"][-1]["content"]


def test_openai_content_filter_fails(tmp_path):
    client = FakeOpenAI([reply(None, None, finish="content_filter")])
    assert run_agent_openai("x", tmp_path, client=client, out=io.StringIO(), browser=False) == 1


def test_provider_saved_and_missing_openai_key(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with TestClient(create_app(tmp_path / "data")) as c:
        j = c.post("/api/jobs", json={"kind": "agent", "task": "Say hi", "provider": "openai",
                                      "model": " gpt-4o-mini "}).json()
        assert j["provider"] == "openai" and j["model"] == "gpt-4o-mini"
        default = c.post("/api/jobs", json={"kind": "agent", "task": "Say hi"}).json()
        assert default["provider"] == "claude" and default["model"] is None
        assert c.post("/api/jobs", json={"kind": "agent", "task": "x", "provider": "gemini"}).status_code == 422
        assert c.get("/api/status").json()["openai_model"] == "gpt-4o"

        end = time.time() + 30
        while c.get(f"/api/jobs/{j['id']}").json()["status"] in ("queued", "running"):
            assert time.time() < end
            time.sleep(0.1)
        log = c.get(f"/api/jobs/{j['id']}/logs").json()["text"]
        assert "OPENAI_API_KEY" in log  # the OpenAI path ran, and explained the missing key
