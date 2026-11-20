"""Drives a real browser against a small local test website."""
import http.server
import threading
from urllib.parse import parse_qs, urlparse

import pytest

from taskrunner.browser import BrowserSession

FORM = b"""<!doctype html><html><head><title>Sign-up form</title></head><body>
<h1>Join the club</h1>
<form action="/submit" method="get">
  <label for="name">Full name</label> <input id="name" name="name">
  <label for="email">Email</label> <input id="email" name="email" type="email">
  <label for="pw">Password</label> <input id="pw" name="pw" type="password">
  <label for="card">Card number</label> <input id="card" name="card" autocomplete="cc-number">
  <label for="plan">Plan</label>
  <select id="plan" name="plan"><option>Free</option><option>Pro</option><option>Team</option></select>
  <label><input type="checkbox" name="news" id="news"> Send me news</label>
  <button type="submit">Create account</button>
</form>
<button id="danger" onclick="if (confirm('Delete everything?')) document.title='DELETED'">Delete all</button>
<a href="/about">About us</a>
<a href="/popup" target="_blank">Open in new tab</a>
</body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            body = FORM
        elif u.path == "/submit":
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            body = (f"<title>Welcome</title><p>Thanks {q.get('name')} ({q.get('email')}), "
                    f"plan={q.get('plan')}, news={q.get('news', 'off')}</p>").encode()
        elif u.path == "/about":
            body = b"<title>About</title><p>We are a club.</p>"
        elif u.path == "/popup":
            body = b"<title>Popup page</title><p>New tab content</p>"
        else:
            self.send_error(404); return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def site():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def browser(tmp_path):
    b = BrowserSession(tmp_path / "profile", headless=True)
    try:
        yield b
    finally:
        b.close()


def ref_of(state: str, needle: str) -> int:
    for line in state.splitlines():
        if line.startswith("[") and needle in line:
            return int(line[1:line.index("]")])
    raise AssertionError(f"{needle!r} not found in:\n{state}")


def test_fill_and_submit_form(browser, site):
    s = browser.run({"action": "open", "url": site + "/"})
    assert "Title: Sign-up form" in s and "Join the club" in s
    assert 'options: Free | Pro | Team' in s

    s = browser.run({"action": "type", "ref": ref_of(s, '"Full name"'), "text": "Ada Lovelace"})
    assert 'value="Ada Lovelace"' in s
    s = browser.run({"action": "type", "ref": ref_of(s, '"Email"'), "text": "ada@example.com"})
    s = browser.run({"action": "select", "ref": ref_of(s, '"Plan"'), "text": "Pro"})
    assert 'value="Pro"' in s
    s = browser.run({"action": "check", "ref": ref_of(s, '"Send me news"'), "text": "on"})
    assert "Send me news\" [x]" in s
    s = browser.run({"action": "click", "ref": ref_of(s, '"Create account"')})
    assert "Title: Welcome" in s
    assert "Thanks Ada Lovelace (ada@example.com), plan=Pro, news=on" in s


def test_refuses_password_and_card_fields(browser, site):
    s = browser.run({"action": "open", "url": site + "/"})
    with pytest.raises(PermissionError, match="password"):
        browser.run({"action": "type", "ref": ref_of(s, '"Password"'), "text": "hunter2"})
    with pytest.raises(PermissionError, match="payment card"):
        browser.run({"action": "type", "ref": ref_of(s, '"Card number"'), "text": "4111111111111111"})
    s = browser.run({"action": "read"})
    assert 'input[password] "Password" value=""' in s  # nothing was typed


def test_confirm_dialogs_are_dismissed(browser, site):
    s = browser.run({"action": "open", "url": site + "/"})
    s = browser.run({"action": "click", "ref": ref_of(s, '"Delete all"')})
    assert "Delete everything?" in s and "dismissed" in s
    assert "Title: DELETED" not in s


def test_links_back_new_tabs_and_screenshot(browser, site):
    s = browser.run({"action": "open", "url": site + "/"})
    s = browser.run({"action": "click", "ref": ref_of(s, '"About us"')})
    assert "Title: About" in s
    s = browser.run({"action": "back"})
    assert "Title: Sign-up form" in s
    s = browser.run({"action": "click", "ref": ref_of(s, '"Open in new tab"')})
    assert "Title: Popup page" in s and "new tab" in s
    shot = browser.run({"action": "screenshot"})
    assert shot[0]["type"] == "image" and shot[0]["source"]["media_type"] == "image/jpeg"
    assert len(shot[0]["source"]["data"]) > 1000


def test_bad_input_is_explained(browser, site):
    with pytest.raises(ValueError, match="http"):
        browser.run({"action": "open", "url": "file:///etc/passwd"})
    browser.run({"action": "open", "url": site + "/"})
    with pytest.raises(LookupError, match="latest page state"):
        browser.run({"action": "click", "ref": 9999})
    with pytest.raises(ValueError, match="ref"):
        browser.run({"action": "click"})


def test_agent_drives_browser(tmp_path, site):
    import io
    from types import SimpleNamespace as NS
    from tests.test_agent import FakeClient, text, tool_use
    from taskrunner.agent import run_agent

    b = BrowserSession(tmp_path / "profile", headless=True)
    client = FakeClient([
        NS(stop_reason="tool_use", content=[tool_use("b1", "browser", action="open", url=site + "/")]),
        NS(stop_reason="tool_use", content=[tool_use("b2", "browser", action="type", ref=1, text="Grace", submit=True)]),
        NS(stop_reason="tool_use", content=[tool_use("b3", "browser", action="screenshot")]),
        NS(stop_reason="end_turn", content=[text("Submitted the form.")]),
    ])
    log = io.StringIO()
    assert run_agent("sign up as Grace", tmp_path, client=client, out=log, browser=b, web=False) == 0

    req = client.requests[0]
    assert "browser" in {t["name"] for t in req["tools"]}
    assert "never complete a purchase" in req["system"].lower()
    after_type = client.requests[2]["messages"][-1]["content"][0]["content"]
    assert "Title: Welcome" in after_type and "Thanks Grace" in after_type
    shot = client.requests[3]["messages"][-1]["content"][0]["content"]
    assert shot[0]["type"] == "image" and shot[1]["type"] == "text"
    out = log.getvalue()
    assert "🖱 browser: open " + site in out and "🖱 browser: type 1 'Grace' + Enter" in out
    assert "(screenshot taken)" in out
    assert b.page is None  # browser closed when the task ended
