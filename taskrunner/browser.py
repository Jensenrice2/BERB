"""A real browser the AI agent can drive: open pages, click, type, choose options, scroll.

It's a separate browser window with its own profile (not the user's everyday browser), shown on
screen so the user can watch. Logins made in that window are remembered between tasks.

Safety rails enforced here (not just asked of the model):
  * it never types into password or payment-card fields - the user does those themselves;
  * browser pop-up dialogs ("Are you sure?") are dismissed, never accepted;
  * only http(s) pages can be opened.
"""
from __future__ import annotations

import base64
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

HEADLESS = os.environ.get("TASKRUNNER_BROWSER_HEADLESS", "0") == "1"
BROWSER_PATH = os.environ.get("TASKRUNNER_BROWSER_PATH")  # optional: a specific Chrome/Edge executable
ACTION_TIMEOUT_MS = 15_000

BROWSER_TOOL = {
    "name": "browser",
    "description": (
        "Control a real web browser (a separate window the user can watch) to use websites: open a "
        "page, click buttons and links, type into fields, choose options, scroll, go back, and look "
        "at the page. After every action you get the page's title, URL, a numbered list of the "
        "elements you can interact with ([ref] kind \"label\"), and its visible text. Use the ref "
        "numbers from the LATEST result: they change whenever the page does. Use 'screenshot' to see "
        "the page when the element list isn't enough. You cannot type passwords or payment-card "
        "details; if a site needs those, stop and tell the user."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["open", "click", "type", "select", "check", "press", "scroll",
                         "back", "read", "screenshot"],
                "description": (
                    "open: go to url. click: click element ref. type: replace the text in field ref "
                    "with text (submit=true presses Enter after). select: choose option text in "
                    "dropdown ref. check: tick (text='on') or untick (text='off') checkbox ref. "
                    "press: press a key such as Enter, Tab or Escape. scroll: text='down' or 'up'. "
                    "back: previous page. read: page state again. screenshot: an image of the page."
                ),
            },
            "url": {"type": "string", "description": "for open: full http(s) URL"},
            "ref": {"type": "integer", "description": "element number from the latest page state"},
            "text": {"type": "string", "description": "for type/select/check/press/scroll"},
            "submit": {"type": "boolean", "description": "for type: press Enter afterwards"},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
    "strict": True,
    "eager_input_streaming": True,
}

# Numbers every visible interactive element and reports what's on the page.
_SNAPSHOT_JS = r"""
() => {
  document.querySelectorAll('[data-tr-ref]').forEach(e => e.removeAttribute('data-tr-ref'));
  const sel = 'a[href], button, input:not([type=hidden]), select, textarea, summary, ' +
    '[role=button], [role=link], [role=checkbox], [role=radio], [role=tab], [role=menuitem], ' +
    '[role=option], [role=switch], [role=combobox], [contenteditable=""], [contenteditable=true]';
  const clean = s => (s || '').replace(/\s+/g, ' ').trim().slice(0, 80);
  const out = [];
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect(), st = getComputedStyle(el);
    if (r.width < 1 || r.height < 1 || st.visibility === 'hidden' || st.display === 'none') continue;
    if (el.disabled) continue;
    const ref = out.length + 1;
    el.setAttribute('data-tr-ref', String(ref));
    const tag = el.tagName.toLowerCase(), type = (el.getAttribute('type') || '').toLowerCase();
    const role = el.getAttribute('role');
    let kind = role || tag;
    if (tag === 'input') kind = type && type !== 'text' ? `input[${type}]` : 'input';
    if (tag === 'a') kind = 'link';
    const label = clean(el.getAttribute('aria-label') || (el.labels && el.labels[0] && el.labels[0].innerText)
      || el.getAttribute('placeholder') || el.innerText || el.getAttribute('title')
      || el.getAttribute('alt') || el.getAttribute('name') || (tag === 'input' && ['submit','button'].includes(type) ? el.value : ''));
    const item = { ref, kind, label };
    if (tag === 'a') item.href = el.getAttribute('href');
    if (['input', 'textarea'].includes(tag) && !['checkbox','radio','submit','button'].includes(type))
      item.value = type === 'password' ? (el.value ? '(filled)' : '') : clean(el.value);
    if (['checkbox', 'radio'].includes(type) || ['checkbox','radio','switch'].includes(role))
      item.checked = el.checked ?? el.getAttribute('aria-checked') === 'true';
    if (tag === 'select') {
      item.value = clean(el.selectedOptions[0] && el.selectedOptions[0].text);
      item.options = [...el.options].slice(0, 15).map(o => clean(o.text));
    }
    out.push(item);
    if (out.length >= 250) break;
  }
  return { title: document.title, url: location.href, elements: out,
           text: (document.body ? document.body.innerText : '').replace(/\n{3,}/g, '\n\n').slice(0, 6000) };
}
"""

# Is this field one we must never type into? (passwords, card numbers, security codes)
_SENSITIVE_JS = r"""
el => {
  const type = (el.getAttribute('type') || '').toLowerCase();
  const ac = (el.getAttribute('autocomplete') || '').toLowerCase();
  const id = [el.name, el.id, el.getAttribute('aria-label'), el.getAttribute('placeholder')].join(' ').toLowerCase();
  if (type === 'password' || ac.includes('password')) return 'password';
  if (ac.startsWith('cc-') || /card.?num|cc.?num|cvv|cvc|security.?code|expir/.test(id)) return 'payment card';
  return '';
}
"""


class BrowserSession:
    def __init__(self, profile_dir: Path, headless: bool = HEADLESS, executable_path: str | None = BROWSER_PATH):
        self.profile_dir = profile_dir
        self.headless = headless
        self.executable_path = executable_path
        self._pw = self._context = self.page = None
        self._seen: set = set()  # tabs already known, to spot new ones
        self.labels: dict[int, str] = {}  # element number -> label, for the activity feed
        self.events: list[str] = []  # dialogs dismissed, new tabs, etc. - reported with the next result

    # ------------------------------------------------------------ lifecycle

    def _start(self) -> None:
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        kwargs = dict(headless=self.headless, viewport={"width": 1280, "height": 800},
                      accept_downloads=False)
        if self.executable_path:
            attempts = [dict(executable_path=self.executable_path)]
        elif sys.platform == "win32":
            attempts = [dict(channel="msedge"), dict(channel="chrome"), {}]  # Edge ships with Windows
        elif sys.platform == "darwin":
            attempts = [dict(channel="chrome"), dict(channel="msedge"), {}]
        else:
            attempts = [{}, dict(channel="chrome")]

        errors = []
        profile = self.profile_dir
        profile.mkdir(parents=True, exist_ok=True)
        for extra in attempts:
            try:
                self._context = self._pw.chromium.launch_persistent_context(str(profile), **kwargs, **extra)
                break
            except Exception as e:
                msg = str(e)
                if "lock" in msg.lower() or "already in use" in msg.lower() or "ProcessSingleton" in msg:
                    # Another agent task has the shared profile open; use a throwaway one.
                    profile = Path(tempfile.mkdtemp(prefix="taskrunner-browser-"))
                    try:
                        self._context = self._pw.chromium.launch_persistent_context(str(profile), **kwargs, **extra)
                        self.events.append("note: another task is using the browser profile, so this "
                                           "window starts logged out")
                        break
                    except Exception as e2:
                        msg = str(e2)
                errors.append(f"{extra.get('channel') or extra.get('executable_path') or 'bundled chromium'}: "
                              f"{msg.splitlines()[0][:200]}")
        if self._context is None:
            self.close()
            raise RuntimeError(
                "Couldn't start a browser. Install Microsoft Edge or Google Chrome, or run "
                "`python -m playwright install chromium` in the app folder. Details: " + " | ".join(errors))

        self._context.on("page", self._on_new_page)
        self.page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._watch(self.page)
        self._seen = set(self._context.pages)

    def _watch(self, page) -> None:
        page.on("dialog", self._on_dialog)

    def _on_dialog(self, dialog) -> None:
        self.events.append(f"a pop-up dialog said {dialog.message[:200]!r}; it was dismissed (not accepted)")
        try:
            dialog.dismiss()
        except Exception:
            pass

    def _on_new_page(self, page) -> None:
        self._watch(page)  # switching to it happens in _follow_tabs, after each action

    def close(self) -> None:
        for thing in (self._context, self._pw):
            try:
                if thing is self._pw and thing:
                    thing.stop()
                elif thing:
                    thing.close()
            except Exception:
                pass
        self._pw = self._context = self.page = None

    # ------------------------------------------------------------ actions

    def run(self, inp: dict):
        """Returns either a string, or a list of content blocks (for screenshots)."""
        action = inp.get("action")
        if action not in BROWSER_TOOL["input_schema"]["properties"]["action"]["enum"]:
            raise ValueError(f"unknown action {action!r}")
        if self._context is None:
            self._start()
        page = self.page

        if action == "open":
            url = (inp.get("url") or "").strip()
            parts = urlparse(url)
            if parts.scheme not in ("http", "https") or not parts.netloc:
                raise ValueError(f"only full http:// or https:// URLs can be opened, not {url!r}")
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        elif action == "click":
            self._el(inp).click(timeout=ACTION_TIMEOUT_MS)
        elif action == "type":
            el = self._el(inp)
            sensitive = el.evaluate(_SENSITIVE_JS)
            if sensitive:
                raise PermissionError(
                    f"refused: that is a {sensitive} field. For safety the agent never types passwords "
                    "or payment details. Stop here and tell the user to do this step themselves "
                    "(they can log in once in the agent's browser window and it will be remembered).")
            el.fill(inp.get("text") or "", timeout=ACTION_TIMEOUT_MS)
            if inp.get("submit"):
                el.press("Enter", timeout=ACTION_TIMEOUT_MS)
        elif action == "select":
            el, choice = self._el(inp), inp.get("text") or ""
            try:
                el.select_option(label=choice, timeout=ACTION_TIMEOUT_MS)
            except Exception:
                el.select_option(value=choice, timeout=ACTION_TIMEOUT_MS)
        elif action == "check":
            el = self._el(inp)
            (el.uncheck if (inp.get("text") or "on").lower() in ("off", "false", "no", "uncheck")
             else el.check)(timeout=ACTION_TIMEOUT_MS)
        elif action == "press":
            page.keyboard.press(inp.get("text") or "Enter")
        elif action == "scroll":
            page.mouse.wheel(0, -700 if (inp.get("text") or "down").lower() == "up" else 700)
        elif action == "back":
            page.go_back(wait_until="domcontentloaded", timeout=30_000)

        self._settle()
        if action == "screenshot":
            img = self.page.screenshot(type="jpeg", quality=60)
            return [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                             "data": base64.b64encode(img).decode()}},
                {"type": "text", "text": self._describe(short=True)},
            ]
        return self._describe()

    def _el(self, inp: dict):
        ref = inp.get("ref")
        if not isinstance(ref, int):
            raise ValueError("this action needs 'ref', an element number from the latest page state")
        loc = self.page.locator(f'[data-tr-ref="{ref}"]')
        if loc.count() == 0:
            raise LookupError(f"element [{ref}] isn't on the page any more; use the numbers from the "
                              "latest page state (action 'read' refreshes them)")
        return loc.first

    def _settle(self) -> None:
        # Let scripts react to a click and new tabs appear. (Playwright's own wait, not time.sleep:
        # it only processes browser events, like a new tab opening, while one of its calls runs.)
        self.page.wait_for_timeout(500)
        self._follow_tabs()
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=10_000)
        except Exception:
            pass
        self._follow_tabs()

    def _follow_tabs(self) -> None:
        """Switch to a tab the page just opened, or away from one that just closed."""
        pages = [p for p in self._context.pages if not p.is_closed()]
        if not pages:
            self.page = self._context.new_page()
            self._watch(self.page)
        elif self.page.is_closed() or pages[-1] is not self.page and pages[-1] not in self._seen:
            if not self.page.is_closed():
                self.events.append("the page opened a new tab; you're now on that tab")
            self.page = pages[-1]
        self._seen = set(pages)

    def _describe(self, short: bool = False) -> str:
        try:
            snap = self.page.evaluate(_SNAPSHOT_JS)
        except Exception:  # page mid-navigation
            self.page.wait_for_timeout(1000)
            snap = self.page.evaluate(_SNAPSHOT_JS)
        lines = [f"Title: {snap['title']}", f"URL: {snap['url']}"]
        if self.events:
            lines += [f"Note: {e}" for e in self.events]
            self.events.clear()
        self.labels = {e["ref"]: e["label"] for e in snap["elements"] if e.get("label")}
        lines.append(f"\nInteractive elements ({len(snap['elements'])}):")
        for e in snap["elements"]:
            line = f"[{e['ref']}] {e['kind']} \"{e['label']}\""
            if e.get("href") and not e["href"].startswith("javascript"):
                line += f" -> {e['href'][:100]}"
            if "value" in e and e["value"] is not None:
                line += f" value=\"{e['value']}\""
            if "checked" in e:
                line += " [x]" if e["checked"] else " [ ]"
            if e.get("options"):
                line += " options: " + " | ".join(e["options"])
            lines.append(line)
        if not short:
            text = re.sub(r"[ \t]+", " ", snap["text"]).strip()
            lines.append("\nVisible text:\n" + (text or "(none)"))
        return "\n".join(lines)
