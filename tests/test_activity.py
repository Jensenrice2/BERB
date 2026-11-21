import io
import json
import threading
import time
from types import SimpleNamespace as NS

from fastapi.testclient import TestClient

from taskrunner.activity import Asker, Events, describe_step, pending_question, read_events
from taskrunner.agent import run_agent
from taskrunner.app import create_app
from tests.test_agent import FakeClient, text, tool_use


def test_steps_are_plain_english():
    labels = {3: "Search", 5: "Email"}
    assert describe_step("bash", {"command": "rm -rf build && make"}) == "Ran a command"
    assert describe_step("powershell", {"command": "Get-ChildItem"}) == "Ran a command"
    assert describe_step("str_replace_based_edit_tool", {"command": "create", "path": "notes/todo.md"}) == "Saved notes/todo.md"
    assert describe_step("str_replace_based_edit_tool", {"command": "view", "path": "/tmp/x/report.md"}) == "Looked at report.md"
    assert describe_step("browser", {"action": "open", "url": "https://www.wikipedia.org/wiki/X"}) == "Went to wikipedia.org"
    assert describe_step("browser", {"action": "click", "ref": 3}, labels) == "Clicked “Search”"
    assert describe_step("browser", {"action": "type", "ref": 5, "text": "secret"}, labels) == "Typed into “Email”"
    assert describe_step("browser", {"action": "click", "ref": 99}, labels) == "Clicked an item"
    assert describe_step("browser", {"action": "read"}) is None
    assert describe_step("open_in_browser", {"url": "https://python.org/about"}) == "Opened python.org in a tab for you"
    assert describe_step("web_search", {"query": "Chicago weather"}) == "Searched the web for “Chicago weather”"
    # nothing code-like ever leaks into a step
    assert "rm" not in describe_step("bash", {"command": "rm -rf /"})


def test_events_file_and_repeats(tmp_path):
    ev = Events(tmp_path / "events.jsonl")
    ev.emit("step", "Ran a command")
    ev.emit("step", "Ran a command")  # repeat squeezed
    ev.emit("message", "Looking now.")
    ev.emit("step", "Ran a command")  # not a repeat any more
    ev.emit("message", "   ")          # empty ignored
    got = read_events(tmp_path / "events.jsonl")
    assert [(e["type"], e["text"]) for e in got] == [
        ("step", "Ran a command"), ("message", "Looking now."), ("step", "Ran a command")]
    assert read_events(tmp_path / "events.jsonl", after=2)[0]["text"] == "Ran a command"
    Events().emit("step", "no file, no error")


def test_asker_waits_for_answer_and_times_out(tmp_path):
    ev = Events(tmp_path / "events.jsonl")
    asker = Asker(tmp_path, ev, timeout=10, poll=0.05)

    def reply():
        while not (tmp_path / "question.json").exists():
            time.sleep(0.02)
        assert pending_question(tmp_path) == "Which size?"
        (tmp_path / "answer.json").write_text(json.dumps({"answer": "Medium"}))

    threading.Thread(target=reply).start()
    assert asker.ask("Which size?") == "The user answered: Medium"
    assert not (tmp_path / "question.json").exists() and pending_question(tmp_path) is None
    assert [e["type"] for e in read_events(tmp_path / "events.jsonl")] == ["question", "answer"]

    quick = Asker(tmp_path, ev, timeout=0.2, poll=0.05)
    assert "didn't answer" in quick.ask("Anyone there?")
    assert not (tmp_path / "question.json").exists()


def test_agent_feed_question_and_report(tmp_path):
    folder = tmp_path / "job"
    folder.mkdir()
    ev = Events(folder / "events.jsonl")
    asker = Asker(folder, ev, timeout=10, poll=0.05)
    client = FakeClient([
        NS(stop_reason="tool_use", content=[
            text("I'll make the list."),
            NS(type="server_tool_use", id="s1", name="web_search", input={"query": "best tents"}),
            tool_use("e1", "str_replace_based_edit_tool", command="create", path="tents.md", file_text="# Tents\n"),
            tool_use("q1", "ask_user", question="Do you want 2-person or 4-person tents?"),
        ]),
        NS(stop_reason="end_turn", content=[text("**Done.** I saved 3 tents for 4 people to tents.md.")]),
    ])
    threading.Thread(target=lambda: (
        [time.sleep(0.02) for _ in iter(lambda: not (folder / "question.json").exists(), False)],
        (folder / "answer.json").write_text(json.dumps({"answer": "4-person"})))).start()

    assert run_agent("find tents", tmp_path / "ws", client=client, out=io.StringIO(),
                     browser=False, events=ev, asker=asker) == 0
    feed = [(e["type"], e["text"]) for e in read_events(folder / "events.jsonl")]
    assert feed == [
        ("message", "I'll make the list."),
        ("step", "Searched the web for “best tents”"),
        ("step", "Saved tents.md"),
        ("question", "Do you want 2-person or 4-person tents?"),
        ("answer", "4-person"),
        ("report", "**Done.** I saved 3 tents for 4 people to tents.md."),
    ]
    tools = {t["name"] for t in client.requests[0]["tools"]}
    assert "ask_user" in tools and "ask_user" in client.requests[0]["system"]
    result = client.requests[1]["messages"][-1]["content"]
    assert any(r["content"] == "The user answered: 4-person" for r in result)


def test_agent_errors_are_friendly(tmp_path):
    ev = Events(tmp_path / "events.jsonl")
    client = FakeClient([NS(stop_reason="refusal", content=[])])
    assert run_agent("x", tmp_path / "ws", client=client, out=io.StringIO(), browser=False, events=ev) == 1
    assert read_events(tmp_path / "events.jsonl") == [
        {"t": read_events(tmp_path / "events.jsonl")[0]["t"], "type": "error", "text": "The AI declined this task."}]
    # without an asker, there's no ask_user tool and the prompt doesn't mention it
    assert "ask_user" not in {t["name"] for t in client.requests[0]["tools"]}


def test_api_question_answer_and_feed(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path)); monkeypatch.setenv("USERPROFILE", str(tmp_path))
    app = create_app(tmp_path / "data")
    with TestClient(app) as c:
        j = c.post("/api/jobs", json={"kind": "agent", "task": "Plan a trip"}).json()
        end = time.time() + 30
        while c.get(f"/api/jobs/{j['id']}").json()["status"] in ("queued", "running"):
            assert time.time() < end
            time.sleep(0.1)
        feed = c.get(f"/api/jobs/{j['id']}/events").json()
        assert feed["events"][0]["type"] == "error" and "API key" in feed["events"][0]["text"]
        assert feed["next"] == 1 and c.get(f"/api/jobs/{j['id']}/events?after=1").json()["events"] == []
        assert c.post(f"/api/jobs/{j['id']}/answer", json={"answer": "yes"}).status_code == 409

        # pretend the agent is running and has asked something
        folder = tmp_path / "data" / "agent" / str(j["id"])
        app.state.store.update(j["id"], status="running")
        (folder / "question.json").write_text(json.dumps({"question": "Window or aisle?"}))
        assert c.get("/api/jobs").json()[0]["question"] == "Window or aisle?"
        assert c.post(f"/api/jobs/{j['id']}/answer", json={"answer": ""}).status_code == 422
        assert c.post(f"/api/jobs/{j['id']}/answer", json={"answer": " Window "}).json() == {"ok": True}
        assert json.loads((folder / "answer.json").read_text()) == {"answer": "Window"}
        app.state.store.update(j["id"], status="failed")
        assert c.get(f"/api/jobs/{j['id']}").json()["question"] is None  # only asked while running
