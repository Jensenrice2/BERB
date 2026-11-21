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
        "page, click buttons and links, fill in forms, choose options, scroll, go back, and look "
        "at the page. After every action you get the page's title, URL, a numbered list of the "
        "elements you can interact with, and its visible text. Each element line is "
        "[ref] kind \"label\", plus for form fields: the question it belongs to (q: ...), "
        "*required, its current value, its options, and any error message the page shows (error: ...). "
        "Forms inside frames are included. Use the ref numbers from the LATEST result: they change "
        "whenever the page does. To fill several fields at once use fill_form. Use 'screenshot' when "
        "the element list isn't enough. You cannot type passwords or payment-card details."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["open", "click", "type", "fill_form", "log_in", "select", "check", "press",
                         "scroll", "back", "read", "screenshot"],
                "description": (
                    "open: go to url. click: click element ref. type: replace the text in field ref "
                    "with text (submit=true presses Enter after). fill_form: fill many fields in one "
                    "go - fields=[{ref, value}]; text boxes get the value, dropdowns choose the option "
                    "with that text, checkboxes are ticked for yes/on/true and unticked for no/off/false, "
                    "radio buttons are picked (value ignored). log_in: on a sign-in page of a site the "
                    "user saved a login for, B.E.R.B fills in their username and password itself (you "
                    "never see the password) and submits; for two-step sign-ins use it again on the "
                    "password page. select: choose option text in dropdown "
                    "ref. check: tick (text='on') or untick (text='off') checkbox ref. press: press a "
                    "key such as Enter, Tab or Escape. scroll: text='down' or 'up'. back: previous page. "
                    "read: page state again. screenshot: an image of the page."
                ),
            },
            "url": {"type": "string", "description": "for open: full http(s) URL"},
            "ref": {"type": "integer", "description": "element number from the latest page state"},
            "text": {"type": "string", "description": "for type/select/check/press/scroll"},
            "submit": {"type": "boolean", "description": "for type: press Enter afterwards"},
            "fields": {
                "type": "array",
                "description": "for fill_form: the fields to fill",
                "items": {
                    "type": "object",
                    "properties": {"ref": {"type": "integer"}, "value": {"type": "string"}},
                    "required": ["ref", "value"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    },
    "strict": True,
    "eager_input_streaming": True,
}

# Numbers every visible interactive element (including inside open shadow roots) and works out
# what each form field is asking: its label, or failing that the question text next to it.
_SNAPSHOT_JS = r"""
(start) => {
  const sel = 'a[href], button, input:not([type=hidden]), select, textarea, summary, ' +
    '[role=button], [role=link], [role=checkbox], [role=radio], [role=tab], [role=menuitem], ' +
    '[role=option], [role=switch], [role=combobox], [role=textbox], [contenteditable=""], [contenteditable=true]';
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const short = (s, n = 80) => { s = clean(s); return s.length > n ? s.slice(0, n - 1) + '…' : s; };
  const textOf = n => n ? clean(n.innerText !== undefined ? n.innerText : n.textContent) : '';
  const byId = (el, id) => { const r = el.getRootNode(); return (r.getElementById ? r.getElementById(id) : null) || document.getElementById(id); };
  const idsText = (el, attr) => (el.getAttribute(attr) || '').split(/\s+/).filter(Boolean).map(id => textOf(byId(el, id))).join(' ');
  const isField = el => /^(input|select|textarea)$/i.test(el.tagName) || el.isContentEditable ||
                        ['textbox', 'combobox', 'checkbox', 'radio', 'switch'].includes(el.getAttribute('role'));

  // all matching elements, also inside open shadow roots
  const found = [];
  const walk = root => {
    for (const el of root.querySelectorAll('*')) {
      if (el.matches(sel)) found.push(el);
      if (el.shadowRoot) walk(el.shadowRoot);
    }
  };
  walk(document);
  for (const el of found) el.removeAttribute('data-tr-ref');

  // text written just before a field (e.g. a question above a box that has no <label>)
  function nearbyText(el) {
    let n = el;
    for (let depth = 0; depth < 4 && n; depth++) {
      let sib = n.previousElementSibling;
      for (let hops = 0; sib && hops < 3; hops++, sib = sib.previousElementSibling) {
        if (sib.querySelector && sib.querySelector('input, select, textarea')) break;
        const t = textOf(sib);
        if (t) return t;
      }
      n = n.parentElement;
      if (n && n.querySelectorAll('input, select, textarea, [contenteditable=true]').length > 3) break;
    }
    return '';
  }
  function labelOf(el, tag, type) {
    return el.getAttribute('aria-label') || idsText(el, 'aria-labelledby')
      || (el.labels && el.labels.length ? textOf(el.labels[0]) : '')
      || (isField(el) && !['checkbox', 'radio'].includes(type) ? (el.getAttribute('placeholder') || nearbyText(el)) : '')
      || ((tag === 'button' || tag === 'a' || el.getAttribute('role')) ? textOf(el) : '')
      || el.getAttribute('title') || el.getAttribute('alt')
      || (tag === 'input' && ['submit', 'button'].includes(type) ? el.value : '')
      || el.getAttribute('name') || textOf(el);
  }
  // the question a radio button / checkbox belongs to
  function questionOf(el, type) {
    const fs = el.closest('fieldset');
    if (fs && fs.querySelector('legend')) return textOf(fs.querySelector('legend'));
    const grp = el.closest('[role=radiogroup], [role=group]');
    if (grp) return grp.getAttribute('aria-label') || idsText(grp, 'aria-labelledby') || nearbyText(grp);
    if (type === 'radio' && el.name) {
      let box = el.parentElement;
      while (box && box.querySelectorAll(`input[type=radio][name="${CSS.escape(el.name)}"]`).length < 2) box = box.parentElement;
      if (box) return box.getAttribute('aria-label') || nearbyText(box);
    }
    return '';
  }

  const out = [];
  for (const el of found) {
    const r = el.getBoundingClientRect(), st = getComputedStyle(el);
    if (r.width < 1 || r.height < 1 || st.visibility === 'hidden' || st.display === 'none') continue;
    if (el.disabled) continue;
    const ref = start + out.length + 1;
    el.setAttribute('data-tr-ref', String(ref));
    const tag = el.tagName.toLowerCase(), type = (el.getAttribute('type') || '').toLowerCase();
    const role = el.getAttribute('role');
    if (type === 'password') el.setAttribute('data-tr-was-password', '');
    let kind = role || tag;
    if (tag === 'input') kind = type && type !== 'text' ? `input[${type}]` : 'input';
    if (tag === 'a') kind = 'link';
    if (el.isContentEditable && !role) kind = 'textbox';
    const item = { ref, kind, label: short(labelOf(el, tag, type)) };
    if (tag === 'a') item.href = el.getAttribute('href');
    if (['checkbox', 'radio'].includes(type) || ['checkbox', 'radio', 'switch'].includes(role)) {
      item.checked = el.checked ?? el.getAttribute('aria-checked') === 'true';
      const q = questionOf(el, type || role);
      if (q && q !== item.label) item.question = short(q, 120);
    }
    if (['input', 'textarea'].includes(tag) && !['checkbox', 'radio', 'submit', 'button', 'reset', 'image'].includes(type))
      item.value = el.hasAttribute('data-tr-secret') ? '(hidden)' :
                   type === 'password' ? (el.value ? '(filled)' : '') : short(el.value, 120);
    if (el.isContentEditable) item.value = short(textOf(el), 120);
    if (tag === 'select') {
      item.value = short(el.selectedOptions[0] && el.selectedOptions[0].text);
      item.options = [...el.options].slice(0, 25).map(o => short(o.text, 40));
    }
    if (isField(el)) {
      if (el.required || el.getAttribute('aria-required') === 'true') item.required = true;
      let invalid = el.getAttribute('aria-invalid') === 'true';
      try { invalid = invalid || el.matches(':user-invalid'); } catch (e) {}
      const err = idsText(el, 'aria-errormessage') || (invalid ? (idsText(el, 'aria-describedby') || el.validationMessage) : '');
      if (err) item.error = short(err, 120);
      else if (!item.question && el.getAttribute('aria-describedby')) {
        const hint = idsText(el, 'aria-describedby'); if (hint && hint !== item.label) item.hint = short(hint, 100);
      }
    }
    out.push(item);
    if (out.length >= 250) break;
  }
  return { title: document.title, url: location.href, elements: out,
           text: (document.body ? document.body.innerText : '').replace(/\n{3,}/g, '\n\n').slice(0, 6000) };
}
"""

# Finds the username and password boxes of a sign-in form (marks them for log_in).
_LOGIN_FIELDS_JS = r"""
() => {
  const vis = el => { const r = el.getBoundingClientRect(), s = getComputedStyle(el);
                      return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && !el.disabled; };
  document.querySelectorAll('[data-tr-login]').forEach(e => e.removeAttribute('data-tr-login'));
  const pw = [...document.querySelectorAll('input[type=password], input[data-tr-was-password], input[data-tr-secret]')].find(vis);
  const looksUser = el => /user|email|e-mail|login|account|identifier|phone/i.test(
    [el.name, el.id, el.getAttribute('autocomplete'), el.getAttribute('aria-label'), el.getAttribute('placeholder'),
     el.labels && el.labels[0] ? el.labels[0].innerText : ''].join(' '));
  const textish = 'input[type=email]:not([data-tr-was-password]), input[type=text]:not([data-tr-was-password]), ' +
                  'input[type=tel], input:not([type]):not([data-tr-was-password])';
  let user = null;
  const scope = pw ? (pw.form || pw.closest('form, [role=form], main, body')) : document;
  const candidates = [...scope.querySelectorAll(textish)].filter(vis);
  user = candidates.find(el => (el.getAttribute('autocomplete') || '').match(/username|email/)) ||
         candidates.find(looksUser) ||
         (pw ? candidates.filter(el => el.compareDocumentPosition(pw) & Node.DOCUMENT_POSITION_FOLLOWING).pop() : null);
  if (user) user.setAttribute('data-tr-login', 'user');
  if (pw) pw.setAttribute('data-tr-login', 'pw');
  return { user: !!user, pw: !!pw };
}
"""

# Is this field one we must never type into? (passwords, card numbers, security codes)
_SENSITIVE_JS = r"""
el => {
  const type = el.hasAttribute('data-tr-was-password') || el.hasAttribute('data-tr-secret') ? 'password'
             : (el.getAttribute('type') || '').toLowerCase();
  const ac = (el.getAttribute('autocomplete') || '').toLowerCase();
  const id = [el.name, el.id, el.getAttribute('aria-label'), el.getAttribute('placeholder')].join(' ').toLowerCase();
  if (type === 'password' || ac.includes('password')) return 'password';
  if (ac.startsWith('cc-') || /card.?num|cc.?num|cvv|cvc|security.?code|expir/.test(id)) return 'payment card';
  return '';
}
"""


class BrowserSession:
    def __init__(self, profile_dir: Path, headless: bool = HEADLESS, executable_path: str | None = BROWSER_PATH,
                 vault=None):
        self.profile_dir = profile_dir
        self.vault = vault  # saved logins (only used when the user turned "Remember passwords" on)
        self.headless = headless
        self.executable_path = executable_path
        self._pw = self._context = self.page = None
        self._seen: set = set()  # tabs already known, to spot new ones
        self.labels: dict[int, str] = {}  # element number -> label, for the activity feed
        self._ref_frames: dict = {}       # element number -> the frame it's in (forms inside iframes)
        self._secrets: set[str] = set()   # saved passwords typed this session: never shown to the AI
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
        elif action == "log_in":
            self.events.append(self._log_in())
        elif action == "fill_form":
            note = self._fill_form(inp.get("fields"))
            if note:
                self.events.append(note)
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

    def _log_in(self) -> str:
        """Sign in with a saved login. The password goes straight from the computer's password
        store into the page; it is never returned to the AI or written to any log."""
        from .vault import secure_page, site_of
        for frame in self.page.frames:  # the page itself first, then any embedded sign-in frames
            try:
                found = frame.evaluate(_LOGIN_FIELDS_JS)
            except Exception:
                continue
            if not (found["user"] or found["pw"]):
                continue
            site = site_of(frame.url)
            if self.vault is None or not self.vault.enabled():
                raise PermissionError("Remember passwords is turned off in B.E.R.B, so you can't sign in "
                                      "for the user. Ask them to sign in themselves in your browser window.")
            if not secure_page(frame.url):
                raise PermissionError(f"{site} isn't a secure (https) page, so saved passwords aren't used here.")
            saved = self.vault.for_url(frame.url)
            if not saved:
                raise LookupError(f"The user hasn't saved a login for {site}. Ask them to sign in themselves in "
                                  "your browser window, or to save a login for this site in B.E.R.B.")
            username, password = saved
            user = frame.locator('[data-tr-login="user"]').first if found["user"] else None
            pw = frame.locator('[data-tr-login="pw"]').first if found["pw"] else None
            if user is not None:
                user.fill(username, timeout=ACTION_TIMEOUT_MS)
            if pw is not None:
                self._secrets.add(password)
                pw.evaluate("e => e.setAttribute('data-tr-secret', '')")  # stays hidden from the AI
                pw.fill(password, timeout=ACTION_TIMEOUT_MS)
            (pw or user).press("Enter", timeout=ACTION_TIMEOUT_MS)
            what = "username and password" if user is not None and pw is not None else (
                "password" if pw is not None else "username (this site asks for the password on the next page; "
                "use log_in again there)")
            return f"signed in to {site} with the user's saved login: filled the {what} and submitted"
        raise LookupError("No sign-in boxes found on this page. Open the site's sign-in page first.")

    def _fill_form(self, fields) -> str:
        """Fill several fields; keeps going past problems and reports them all at the end."""
        if not isinstance(fields, list) or not fields:
            raise ValueError("fill_form needs fields=[{ref, value}, ...]")
        filled, problems = [], []
        for f in fields:
            ref, value = (f or {}).get("ref"), str((f or {}).get("value", ""))
            label = self.labels.get(ref) or f"[{ref}]"
            try:
                el = self._el({"ref": ref})
                info = el.evaluate("""e => ({tag: e.tagName.toLowerCase(), type: (e.getAttribute('type') || '').toLowerCase(),
                                           role: e.getAttribute('role') || ''})""")
                tag, typ, role = info["tag"], info["type"], info["role"]
                if tag == "select":
                    try:
                        el.select_option(label=value, timeout=ACTION_TIMEOUT_MS)
                    except Exception:
                        el.select_option(value=value, timeout=ACTION_TIMEOUT_MS)
                elif typ == "radio" or role == "radio":
                    el.check(timeout=ACTION_TIMEOUT_MS)
                elif typ == "checkbox" or role in ("checkbox", "switch"):
                    on = value.strip().lower() not in ("no", "off", "false", "0", "unchecked", "uncheck", "")
                    (el.check if on else el.uncheck)(timeout=ACTION_TIMEOUT_MS)
                else:
                    sensitive = el.evaluate(_SENSITIVE_JS)
                    if sensitive:
                        problems.append(f"{label}: skipped - it's a {sensitive} field; the user must fill it in")
                        continue
                    el.fill(value, timeout=ACTION_TIMEOUT_MS)
                filled.append(label)
            except Exception as e:
                problems.append(f"{label}: {str(e).splitlines()[0][:160]}")
        note = f"filled {len(filled)} of {len(fields)} fields"
        return note + ("; problems: " + "; ".join(problems) if problems else "")

    def _el(self, inp: dict):
        ref = inp.get("ref")
        if not isinstance(ref, int):
            raise ValueError("this action needs 'ref', an element number from the latest page state")
        frame = self._ref_frames.get(ref, self.page.main_frame)
        if frame.is_detached():
            frame = self.page.main_frame
        loc = frame.locator(f'[data-tr-ref="{ref}"]')
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

    def _snapshot_frames(self) -> tuple[dict, list[dict], list[str]]:
        """The page plus any frames in it (e.g. an embedded form), numbered as one list."""
        try:
            main = self.page.main_frame.evaluate(_SNAPSHOT_JS, 0)
        except Exception:  # page mid-navigation
            self.page.wait_for_timeout(1000)
            main = self.page.main_frame.evaluate(_SNAPSHOT_JS, 0)
        elements = list(main["elements"])
        self._ref_frames = {e["ref"]: self.page.main_frame for e in elements}
        frame_texts = []
        for frame in self.page.frames[1:]:
            if len(elements) >= 400 or frame.is_detached():
                break
            try:
                if not frame.frame_element().is_visible():
                    continue
                snap = frame.evaluate(_SNAPSHOT_JS, len(elements))
            except Exception:
                continue  # frames come and go (ads, trackers); skip any we can't read
            if not snap["elements"] and not snap["text"].strip():
                continue
            for e in snap["elements"]:
                e["frame"] = True
                self._ref_frames[e["ref"]] = frame
            elements += snap["elements"]
            if snap["text"].strip():
                frame_texts.append(snap["text"][:2500])
        return main, elements, frame_texts

    def _redact(self, text: str) -> str:
        """Some sites echo a submitted password back (e.g. in the address). Blank it out."""
        from urllib.parse import quote, quote_plus
        for secret in self._secrets:
            for form in {secret, quote(secret, safe=""), quote_plus(secret)}:
                if form:
                    text = text.replace(form, "[hidden password]")
        return text

    def _describe(self, short: bool = False) -> str:
        return self._redact(self._describe_raw(short))

    def _describe_raw(self, short: bool = False) -> str:
        snap, elements, frame_texts = self._snapshot_frames()
        lines = [f"Title: {snap['title']}", f"URL: {snap['url']}"]
        if self.events:
            lines += [f"Note: {e}" for e in self.events]
            self.events.clear()
        self.labels = {e["ref"]: e["label"] for e in elements if e.get("label")}
        lines.append(f"\nInteractive elements ({len(elements)}):")
        for e in elements:
            line = f"[{e['ref']}] {e['kind']} \"{e['label']}\""
            if e.get("question"):
                line += f" (q: {e['question']})"
            if e.get("required"):
                line += " *required"
            if e.get("href") and not e["href"].startswith("javascript"):
                line += f" -> {e['href'][:100]}"
            if "value" in e and e["value"] is not None:
                line += f" value=\"{e['value']}\""
            if "checked" in e:
                line += " [x]" if e["checked"] else " [ ]"
            if e.get("options"):
                line += " options: " + " | ".join(e["options"])
            if e.get("hint"):
                line += f" (hint: {e['hint']})"
            if e.get("error"):
                line += f" (error: {e['error']})"
            if e.get("frame"):
                line += " [in embedded frame]"
            lines.append(line)
        if not short:
            text = re.sub(r"[ \t]+", " ", snap["text"]).strip()
            lines.append("\nVisible text:\n" + (text or "(none)"))
            for t in frame_texts:
                lines.append("\nText inside an embedded frame:\n" + re.sub(r"[ \t]+", " ", t).strip())
        return "\n".join(lines)
