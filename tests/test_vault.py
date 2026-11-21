"""Saved logins: off by default, passwords only in the password store, only typed on their own site."""
import http.server
import io
import json
import threading
from types import SimpleNamespace as NS
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from taskrunner.app import create_app
from taskrunner.browser import BrowserSession
from taskrunner.vault import MemoryBackend, Vault, VaultUnavailable, secure_page, site_matches, site_of

SECRET = "correct-horse-battery-staple"


def test_site_rules():
    assert site_of("https://www.Amazon.com/ap/signin?x=1") == "amazon.com"
    assert site_of("amazon.com") == "amazon.com"
    assert site_matches("amazon.com", "https://www.amazon.com/login")
    assert site_matches("amazon.com", "https://smile.amazon.com/")
    assert not site_matches("amazon.com", "https://amazon.com.evil.example/login")   # look-alike
    assert not site_matches("amazon.com", "https://notamazon.com/")
    assert secure_page("https://x.com") and not secure_page("http://x.com") and secure_page("http://127.0.0.1:8000/")


def test_vault_keeps_passwords_out_of_files(tmp_path):
    v = Vault(tmp_path, backend=MemoryBackend())
    assert v.enabled() is False                      # off by default
    v.set_enabled(True)
    v.save("https://www.example.com/login", "ada@example.com", SECRET)
    v.save("accounts.example.com", "ada2", "other")
    assert v.logins() == [{"site": "accounts.example.com", "username": "ada2"},
                          {"site": "example.com", "username": "ada@example.com"}]
    for f in tmp_path.iterdir():
        assert SECRET not in f.read_text()           # only in the password store
    assert v.for_url("https://example.com/signin") == ("ada@example.com", SECRET)
    assert v.for_url("https://accounts.example.com/") == ("ada2", "other")   # most specific wins
    assert v.for_url("http://example.com/signin") is None                    # not secure
    assert v.for_url("https://example.com.phish.test/") is None              # look-alike
    v.set_enabled(False)
    assert v.for_url("https://example.com/signin") is None and v.sites() == []   # turned off: unused
    v.set_enabled(True)
    assert v.delete("example.com", "ada@example.com") and not v.delete("example.com", "nobody")
    assert v.for_url("https://example.com/") is None
    with pytest.raises(ValueError):
        v.save("example.com", "", "x")


# ---------------------------------------------------------------- a practice sign-in site

LOGIN = """<!doctype html><title>Sign in</title><form action="/account" method="get">
<label>Email <input name="email" type="email" autocomplete="username"></label>
<label>Password <input name="pw" type="password" id="pw"></label>
<button type="button" onclick="document.getElementById('pw').type='text'">Show password</button>
<button type="submit">Sign in</button></form>"""
STEP1 = """<!doctype html><title>Sign in</title><form action="/step2" method="get">
<label>Email or phone <input name="email" type="email"></label><button>Next</button></form>"""
STEP2 = """<!doctype html><title>Enter password</title><form action="/account" method="get">
<input type="hidden" name="email" value="{email}">
<label>Password <input name="pw" type="password"></label><button>Sign in</button></form>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/login":
            body = LOGIN
        elif u.path == "/step1":
            body = STEP1
        elif u.path == "/step2":
            body = STEP2.replace("{email}", q.get("email", ""))
        elif u.path == "/account":
            ok = q.get("pw") == SECRET
            body = f"<title>Account</title><p>{'Welcome back, ' + q.get('email', '') if ok else 'Wrong password'}</p>"
        else:
            self.send_error(404); return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def site():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def vault(tmp_path):
    v = Vault(tmp_path / "data", backend=MemoryBackend())
    v.set_enabled(True)
    v.save("127.0.0.1", "ada@example.com", SECRET)
    return v


def test_log_in_fills_and_submits_without_revealing(tmp_path, site, vault):
    b = BrowserSession(tmp_path / "profile", headless=True, vault=vault)
    try:
        b.run({"action": "open", "url": site + "/login"})
        b.run({"action": "click", "ref": 3})  # "Show password" turns the box into plain text...
        out = b.run({"action": "log_in"})
        assert "Welcome back, ada@example.com" in out and "signed in to 127.0.0.1" in out
        assert SECRET not in out
        # ...and even shown in plain text, a filled-in saved password stays hidden from the AI
        out = b.run({"action": "back"})
        assert SECRET not in out
        shot = b.run({"action": "screenshot"})
        assert SECRET not in json.dumps(shot[1])
    finally:
        b.close()


def test_two_step_sign_in(tmp_path, site, vault):
    b = BrowserSession(tmp_path / "profile", headless=True, vault=vault)
    try:
        b.run({"action": "open", "url": site + "/step1"})
        out = b.run({"action": "log_in"})
        assert "Title: Enter password" in out and "use log_in again" in out
        out = b.run({"action": "log_in"})
        assert "Welcome back, ada@example.com" in out and SECRET not in out
    finally:
        b.close()


def test_log_in_refuses_when_off_or_not_saved(tmp_path, site):
    off = Vault(tmp_path / "off", backend=MemoryBackend())
    off.save  # exists, but the switch is off
    b = BrowserSession(tmp_path / "p1", headless=True, vault=off)
    try:
        b.run({"action": "open", "url": site + "/login"})
        with pytest.raises(PermissionError, match="turned off"):
            b.run({"action": "log_in"})
    finally:
        b.close()
    other = Vault(tmp_path / "other", backend=MemoryBackend())
    other.set_enabled(True)
    other.save("example.com", "ada", SECRET)  # saved for a different site
    b = BrowserSession(tmp_path / "p2", headless=True, vault=other)
    try:
        b.run({"action": "open", "url": site + "/login"})
        with pytest.raises(LookupError, match="hasn't saved a login for 127.0.0.1"):
            b.run({"action": "log_in"})
        state = b.run({"action": "read"})
        assert 'value=""' in [l for l in state.splitlines() if "Password" in l and "input" in l][0]
    finally:
        b.close()


def test_agent_knows_saved_sites_and_feed_hides_details(tmp_path, site, vault):
    from taskrunner.activity import Events, read_events
    from taskrunner.agent import run_agent
    from tests.test_agent import FakeClient, text, tool_use

    b = BrowserSession(tmp_path / "profile", headless=True, vault=vault)
    ev = Events(tmp_path / "events.jsonl")
    client = FakeClient([
        NS(stop_reason="tool_use", content=[tool_use("b1", "browser", action="open", url=site + "/login")]),
        NS(stop_reason="tool_use", content=[tool_use("b2", "browser", action="log_in")]),
        NS(stop_reason="end_turn", content=[text("You're signed in.")]),
    ])
    log = io.StringIO()
    assert run_agent("sign in", tmp_path / "ws", client=client, out=log, browser=b, web=False, events=ev) == 0
    assert "saved logins for: 127.0.0.1" in client.requests[0]["system"]
    sent = json.dumps([r["messages"] for r in client.requests], default=str)
    assert SECRET not in sent and SECRET not in log.getvalue()          # never reaches the AI or the log
    assert "Welcome back" in sent
    steps = [e["text"] for e in read_events(tmp_path / "events.jsonl")]
    assert "Signed in with your saved login" in steps


def test_logins_api(tmp_path):
    v = Vault(tmp_path / "data", backend=MemoryBackend())
    with TestClient(create_app(tmp_path / "data", vault=v)) as c:
        assert c.get("/api/logins").json() == {"enabled": False, "available": True, "logins": []}
        new = {"site": "https://www.example.com/login", "username": "ada", "password": SECRET}
        assert c.post("/api/logins", json=new).status_code == 409            # off: can't save
        assert c.put("/api/logins/settings", json={"enabled": True}).json() == {"enabled": True}
        r = c.post("/api/logins", json=new)
        assert r.status_code == 201 and r.json() == {"site": "example.com", "username": "ada"}
        listed = c.get("/api/logins").json()
        assert listed["logins"] == [{"site": "example.com", "username": "ada"}] and SECRET not in json.dumps(listed)
        assert c.post("/api/logins", json={**new, "site": "nonsense"}).status_code == 400
        # another website can't add or delete logins through your browser
        assert c.post("/api/logins", json=new, headers={"origin": "https://evil.example"}).status_code == 403
        assert c.delete("/api/logins?site=example.com&username=ada",
                        headers={"origin": "https://evil.example"}).status_code == 403
        assert c.delete("/api/logins?site=example.com&username=ada").status_code == 204
        assert c.delete("/api/logins?site=example.com&username=ada").status_code == 404
        assert c.get("/api/logins").json()["logins"] == []


def test_real_password_store_roundtrip(tmp_path):
    """Windows Credential Manager / macOS Keychain, where the test machine has one."""
    v = Vault(tmp_path)
    if not v.available():
        pytest.skip("no password store on this machine")
    v.set_enabled(True)
    try:
        v.save("berb-test.example", "tester", SECRET)
    except Exception as e:  # e.g. a locked keychain on a headless Mac
        pytest.skip(f"password store refused: {e}")
    try:
        assert v.for_url("https://berb-test.example/login") == ("tester", SECRET)
    finally:
        v.delete("berb-test.example", "tester")
    assert v.for_url("https://berb-test.example/login") is None
