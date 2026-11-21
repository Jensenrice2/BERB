"""OpenAI agent: the same job as the Claude agent (and the same tools and safety rules), driven by an
OpenAI model through the Chat Completions API, which older models such as gpt-4o support.

The model is chosen per task in the dashboard; the default comes from TASKRUNNER_OPENAI_MODEL.
Needs OPENAI_API_KEY. Differences from the Claude agent: no built-in web search/fetch (that is an
Anthropic feature) - it uses the browser tool to visit sites instead.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .agent import EDITOR_TOOL, OPEN_TAB_TOOL, Toolbox
from .browser import BROWSER_TOOL

DEFAULT_MODEL = os.environ.get("TASKRUNNER_OPENAI_MODEL", "gpt-4o")

BASH_FUNCTION = {
    "name": "bash",
    "description": ("Run a bash command and get its combined output. The session persists between "
                    "calls (cd, variables). Never run anything that waits for input; commands are "
                    "stopped after 300 seconds. Pass restart=true to start a fresh shell."),
    "parameters": {
        "type": "object",
        "properties": {"command": {"type": "string"}, "restart": {"type": "boolean"}},
    },
}
EDITOR_FUNCTION = {
    "name": EDITOR_TOOL["name"],
    "description": (
        "View, create and edit files in the workspace. command=view: show a file (optional "
        "view_range [first, last] line numbers) or list a folder. command=create: write file_text "
        "to path. command=str_replace: replace old_str (must match exactly once) with new_str. "
        "command=insert: insert insert_text after line insert_line (0 = top of file)."),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "enum": ["view", "create", "str_replace", "insert"]},
            "path": {"type": "string"},
            "file_text": {"type": "string"},
            "old_str": {"type": "string"},
            "new_str": {"type": "string"},
            "insert_line": {"type": "integer"},
            "insert_text": {"type": "string"},
            "view_range": {"type": "array", "items": {"type": "integer"}},
        },
        "required": ["command", "path"],
    },
}


def _function(tool: dict) -> dict:
    """Our Anthropic-style custom tool definition -> an OpenAI function tool."""
    return {"type": "function", "function": {
        "name": tool["name"], "description": tool["description"], "parameters": tool["input_schema"]}}


def openai_tools(box: Toolbox) -> list[dict]:
    shell = (_function(box.shell_tool) if box.shell == "powershell"
             else {"type": "function", "function": BASH_FUNCTION})
    tools = [shell, {"type": "function", "function": EDITOR_FUNCTION}, _function(OPEN_TAB_TOOL)]
    if box.browser:
        tools.append(_function(BROWSER_TOOL))
    return tools


def run_agent_openai(task: str, workspace: Path, max_turns: int = 60, client=None, out=sys.stdout,
                     model: str | None = None, shell: str | None = None, tabs=None, browser=None) -> int:
    """Returns a process exit code: 0 done, 1 failed."""
    import openai

    def say(*a):
        print(*a, file=out, flush=True)

    model = (model or "").strip() or DEFAULT_MODEL
    workspace.mkdir(parents=True, exist_ok=True)
    client = client or openai.OpenAI()
    box = Toolbox(workspace, say, shell=shell, tabs=tabs, browser=browser)
    tools = openai_tools(box)
    messages: list = [
        {"role": "system", "content": box.system_prompt(workspace, web=False)},
        {"role": "user", "content": task},
    ]
    images_ok = True  # turned off if the model turns out not to accept images
    say(f"Agent: OpenAI {model} · workspace: {workspace}\nTask:\n{task}\n")

    try:
        turn = 0
        while turn < max_turns:
            turn += 1
            say(f"\n── turn {turn} ──")
            try:
                resp = client.chat.completions.create(model=model, messages=_strip_private(messages),
                                                      tools=tools)
            except openai.BadRequestError as e:
                if images_ok and "image" in str(e).lower():
                    # This model can't look at pictures: swap screenshots for a note and retry.
                    images_ok = False
                    for m in messages:
                        if m.get("_screenshot"):
                            m["content"] = "(A screenshot was taken, but this model can't view images.)"
                    say("  (this model can't view screenshots; continuing with text only)")
                    turn -= 1
                    continue
                raise
            choice = resp.choices[0]
            msg = choice.message
            if msg.content:
                say(msg.content)

            assistant = {"role": "assistant", "content": msg.content or ""}
            if msg.tool_calls:
                assistant["tool_calls"] = [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in msg.tool_calls]
            messages.append(assistant)

            if not msg.tool_calls:
                if choice.finish_reason == "length":
                    messages.append({"role": "user", "content": "You hit the output limit; continue, in smaller steps."})
                    continue
                if choice.finish_reason == "content_filter":
                    say("✖ The model declined this task.")
                    return 1
                say(f"✔ Agent finished after {turn} turn(s).")
                return 0

            screenshots = []
            for tc in msg.tool_calls:
                try:
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        raise ValueError("the tool arguments weren't valid JSON (maybe cut off); try again")
                    if not isinstance(args, dict):
                        raise ValueError("tool arguments must be a JSON object")
                    result = box.run(tc.function.name, args)
                    if isinstance(result, list):  # screenshot: OpenAI tool results can't hold images
                        text = next((b["text"] for b in result if b["type"] == "text"), "")
                        img = next((b for b in result if b["type"] == "image"), None)
                        result = text + "\n(The screenshot follows in the next message.)"
                        if img:
                            screenshots.append(img["source"]["data"])
                except Exception as e:
                    say(f"  ✖ {e}")
                    result = f"Error: {e}"
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

            for data in screenshots:
                if images_ok:
                    messages.append({"role": "user", "_screenshot": True, "content": [
                        {"type": "text", "text": "Screenshot from the browser tool:"},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{data}"}},
                    ]})
                else:
                    messages.append({"role": "user", "_screenshot": True,
                                     "content": "(A screenshot was taken, but this model can't view images.)"})

        say(f"✖ Stopped: reached the limit of {max_turns} turns.")
        return 1
    except openai.AuthenticationError:
        from .agent import key_hint
        say("✖ OpenAI rejected the API key (it's wrong, incomplete, or was deleted).")
        say("  " + key_hint("OPENAI_API_KEY", "sk-"))
        say("  Fix: create a new key at https://platform.openai.com/api-keys, put it after OPENAI_API_KEY= in "
            ".env on one line, save, restart the app, and retry.")
        return 1
    except openai.NotFoundError as e:
        say(f"✖ OpenAI couldn't find the model {model!r} (or your account can't use it): {e.message}")
        return 1
    except openai.APIStatusError as e:
        say(f"✖ OpenAI API error {e.status_code}: {e.message}")
        return 1
    except openai.APIConnectionError as e:
        say(f"✖ Could not reach the OpenAI API: {e}")
        return 1
    finally:
        box.close()


def _strip_private(messages: list) -> list:
    return [{k: v for k, v in m.items() if not k.startswith("_")} for m in messages]
