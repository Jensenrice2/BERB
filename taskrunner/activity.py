"""What the dashboard shows people: a plain-English activity feed (no code), questions the AI asks
the user, and its final report.

Each agent task gets a folder (data/agent/<job id>/) holding:
  events.jsonl   one JSON object per line: {"t": time, "type": ..., "text": ...}
                 types: step, message, question, answer, report, error
  question.json  present while the AI is waiting for the user to answer
  answer.json    written by the server when the user answers; the agent picks it up
The full technical log (commands, outputs) is still written to the job's log file.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse

ANSWER_TIMEOUT = int(os.environ.get("TASKRUNNER_ANSWER_TIMEOUT", "1800"))  # seconds to wait for a reply

ASK_TOOL = {
    "name": "ask_user",
    "description": (
        "Ask the user a question and wait for their answer (they see it in the app). Use it when "
        "you need something only they know - a preference, a missing detail, which option to pick - "
        "or their go-ahead before anything that spends money, sends, posts, deletes or can't be "
        "undone, or to have them log in to a site in your browser window. Ask one clear, short "
        "question. Don't ask about things you can reasonably decide yourself."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"question": {"type": "string", "description": "The question, in plain words"}},
        "required": ["question"],
        "additionalProperties": False,
    },
    "strict": True,
    "eager_input_streaming": True,
}


class Events:
    """Appends activity events for the dashboard. Without a path it does nothing (tests, CLI)."""

    def __init__(self, path: Path | None = None):
        self.path = path
        self._last_step = None
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, type: str, text: str) -> None:
        text = (text or "").strip()
        if not self.path or not text:
            return
        if type == "step" and text == self._last_step:
            return  # don't repeat "Ran a command" five times in a row
        self._last_step = text if type == "step" else None
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.time(), "type": type, "text": text[:20_000]}, ensure_ascii=False) + "\n")


def read_events(path: Path, after: int = 0) -> list[dict]:
    if not path.exists():
        return []
    events = []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i < after:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                break  # a line still being written
    return events


def _site(url: str | None) -> str:
    host = urlparse(url or "").netloc
    return host[4:] if host.startswith("www.") else host or (url or "a page")


def describe_step(name: str, inp: dict, labels: dict | None = None, shell: str = "bash") -> str | None:
    """One short, code-free line for a tool call (None = not worth showing)."""
    labels = labels or {}
    if name in ("bash", "powershell"):
        return None if inp.get("restart") else "Ran a command"
    if name == "str_replace_based_edit_tool":
        path = inp.get("path") or "a file"
        path = Path(path).name if os.path.isabs(path) else path
        return {"view": f"Looked at {path}", "create": f"Saved {path}",
                "str_replace": f"Edited {path}", "insert": f"Edited {path}"}.get(inp.get("command"), f"Worked on {path}")
    if name == "open_in_browser":
        return f"Opened {_site(inp.get('url'))} in a tab for you"
    if name == "browser":
        act, ref = inp.get("action"), inp.get("ref")
        label = labels.get(ref) or "an item"
        label = f"“{label}”" if label != "an item" else label
        return {
            "open": f"Went to {_site(inp.get('url'))}",
            "click": f"Clicked {label}",
            "type": f"Typed into {label}",
            "select": f"Chose “{inp.get('text', '')}” in {label}",
            "check": f"{'Unticked' if str(inp.get('text', 'on')).lower() in ('off', 'false', 'no') else 'Ticked'} {label}",
            "press": f"Pressed {inp.get('text') or 'Enter'}",
            "scroll": "Scrolled the page",
            "back": "Went back a page",
            "screenshot": "Looked at the page",
        }.get(act)
    if name == "web_search":
        return f"Searched the web for “{inp.get('query', '')}”"
    if name == "web_fetch":
        return f"Read {_site(inp.get('url'))}"
    return None


class Asker:
    """ask_user: shows the question in the app and waits for the answer file."""

    def __init__(self, folder: Path, events: Events, timeout: int = ANSWER_TIMEOUT, poll: float = 1.0):
        self.folder, self.events, self.timeout, self.poll = folder, events, timeout, poll

    def ask(self, question) -> str:
        if not isinstance(question, str) or not question.strip():
            raise ValueError("missing 'question'; try again")
        q, a = self.folder / "question.json", self.folder / "answer.json"
        a.unlink(missing_ok=True)
        q.write_text(json.dumps({"question": question.strip(), "t": time.time()}), encoding="utf-8")
        self.events.emit("question", question)
        deadline = time.time() + self.timeout
        try:
            while time.time() < deadline:
                if a.exists():
                    try:
                        answer = json.loads(a.read_text(encoding="utf-8"))["answer"]
                    except (ValueError, KeyError):
                        time.sleep(0.2)  # still being written
                        continue
                    a.unlink(missing_ok=True)
                    self.events.emit("answer", answer)
                    return f"The user answered: {answer}"
                time.sleep(self.poll)
        finally:
            q.unlink(missing_ok=True)
        self.events.emit("step", "No answer came, so it carried on without one")
        return (f"The user didn't answer within {self.timeout // 60} minutes. Continue as best you can "
                "without it, or stop and say in your report what you still need from them.")


def pending_question(folder: Path) -> str | None:
    try:
        return json.loads((folder / "question.json").read_text(encoding="utf-8"))["question"]
    except (OSError, ValueError, KeyError):
        return None


def write_answer(folder: Path, answer: str) -> None:
    tmp = folder / "answer.json.tmp"
    tmp.write_text(json.dumps({"answer": answer}), encoding="utf-8")
    os.replace(tmp, folder / "answer.json")  # appears all at once
