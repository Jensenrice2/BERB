"""Real-world form layouts: questions written next to boxes, choice groups, errors, embedded forms."""
import http.server
import threading
from urllib.parse import parse_qs, urlparse

import pytest

from taskrunner.activity import describe_step
from taskrunner.browser import BrowserSession

SURVEY = """<!doctype html><title>Volunteer sign-up</title>
<h1>Volunteer sign-up</h1>
<p>Please answer every question marked with *.</p>
<form id="f" action="/thanks" method="get" novalidate>
  <div class="q"><div class="title">What is your full name? *</div>
    <div><input name="who" required aria-describedby="who-err"></div><div id="who-err" class="err"></div></div>
  <div class="q"><div class="title">Which email should we use?</div><input name="mail" type="email"></div>
  <fieldset><legend>Which day can you help?</legend>
    <label><input type="radio" name="day" value="sat"> Saturday</label>
    <label><input type="radio" name="day" value="sun"> Sunday</label></fieldset>
  <div id="sizeq">T-shirt size</div>
  <div role="radiogroup" aria-labelledby="sizeq">
    <label><input type="radio" name="size" value="M"> Medium</label>
    <label><input type="radio" name="size" value="L"> Large</label></div>
  <label for="team">Team</label>
  <select id="team" name="team"><option>Kitchen</option><option>Garden</option><option>Front desk</option></select>
  <label><input type="checkbox" name="news"> Email me updates</label>
  <div class="q"><div class="title">Anything else we should know?</div>
    <div contenteditable="true" id="notes" style="min-height:20px;border:1px solid"></div></div>
  <div class="q"><div class="title">Choose a password</div><input type="password" name="pw"></div>
  <div id="host"></div>
  <button type="submit">Send</button>
</form>
<script>
  // a box inside a web component (shadow DOM)
  const root = document.getElementById('host').attachShadow({mode: 'open'});
  root.innerHTML = '<label>Referral code <input id="code"></label>';
  // the page's own error messages, like many real sites
  document.getElementById('f').addEventListener('submit', e => {
    const who = document.querySelector('[name=who]');
    if (!who.value.trim()) {
      e.preventDefault();
      who.setAttribute('aria-invalid', 'true');
      document.getElementById('who-err').textContent = 'Please enter your name';
    }
  });
</script>"""

EMBED = """<!doctype html><title>Contact us</title><h1>Contact us</h1>
<p>Use the form below.</p>
<iframe src="/inner" style="width:600px;height:300px"></iframe>"""

INNER = """<!doctype html><title>inner</title>
<form action="/sent" method="get">
  <label>Your message <textarea name="msg"></textarea></label>
  <button type="submit">Send message</button>
</form>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        pages = {"/survey": SURVEY, "/embed": EMBED, "/inner": INNER,
                 "/thanks": f"<title>Thanks</title><p>Signed up: {q}</p>",
                 "/sent": f"<title>Sent</title><p>Message: {q.get('msg')}</p>"}
        body = pages.get(u.path)
        if body is None:
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
def browser(tmp_path):
    b = BrowserSession(tmp_path / "profile", headless=True)
    try:
        yield b
    finally:
        b.close()


def line_of(state: str, needle: str) -> str:
    for line in state.splitlines():
        if line.startswith("[") and needle in line:
            return line
    raise AssertionError(f"{needle!r} not found in:\n{state}")


def ref_of(state: str, needle: str) -> int:
    line = line_of(state, needle)
    return int(line[1:line.index("]")])


def test_reads_the_questions_on_a_form(browser, site):
    s = browser.run({"action": "open", "url": site + "/survey"})
    # boxes with no <label> get the question written above them
    assert 'input "What is your full name? *" *required value=""' in line_of(s, "full name")
    assert 'input[email] "Which email should we use?"' in line_of(s, "Which email")
    # each choice knows which question it answers
    assert '(q: Which day can you help?)' in line_of(s, '"Saturday"')
    assert '(q: T-shirt size)' in line_of(s, '"Medium"')
    assert "options: Kitchen | Garden | Front desk" in line_of(s, '"Team"')
    assert 'textbox "Anything else we should know?"' in line_of(s, "Anything else")
    assert 'input "Referral code"' in line_of(s, "Referral code")  # inside a web component


def test_fill_form_then_fix_errors_and_submit(browser, site):
    s = browser.run({"action": "open", "url": site + "/survey"})
    # submitting with the required name empty: the page's own error message is reported
    s = browser.run({"action": "click", "ref": ref_of(s, '"Send"')})
    assert "(error: Please enter your name)" in line_of(s, "full name")

    s = browser.run({"action": "fill_form", "fields": [
        {"ref": ref_of(s, "full name"), "value": "Ada Lovelace"},
        {"ref": ref_of(s, "Which email"), "value": "ada@example.com"},
        {"ref": ref_of(s, '"Sunday"'), "value": "yes"},
        {"ref": ref_of(s, '"Large"'), "value": ""},
        {"ref": ref_of(s, '"Team"'), "value": "Garden"},
        {"ref": ref_of(s, "Email me updates"), "value": "yes"},
        {"ref": ref_of(s, "Anything else"), "value": "I can bring tools."},
        {"ref": ref_of(s, "Referral code"), "value": "FRIEND10"},
        {"ref": ref_of(s, "Choose a password"), "value": "hunter2"},  # must be refused
    ]})
    assert "filled 8 of 9 fields" in s and "Choose a password: skipped" in s and "password field" in s
    assert 'value="Ada Lovelace"' in line_of(s, "full name")
    assert line_of(s, '"Sunday"').endswith("[x]") and line_of(s, '"Large"').endswith("[x]")
    assert 'value="Garden"' in line_of(s, '"Team"')
    assert 'value="I can bring tools."' in line_of(s, "Anything else")
    assert 'value="FRIEND10"' in line_of(s, "Referral code")
    assert 'value=""' in line_of(s, "Choose a password")  # nothing typed

    s = browser.run({"action": "click", "ref": ref_of(s, '"Send"')})
    assert "Title: Thanks" in s
    for part in ("'who': 'Ada Lovelace'", "'mail': 'ada@example.com'", "'day': 'sun'", "'size': 'L'",
                 "'team': 'Garden'", "'news': 'on'"):
        assert part in s, part
    assert "'pw'" not in s or "'pw': ''" in s


def test_form_inside_an_embedded_frame(browser, site):
    s = browser.run({"action": "open", "url": site + "/embed"})
    line = line_of(s, "Your message")
    assert "[in embedded frame]" in line
    s = browser.run({"action": "type", "ref": ref_of(s, "Your message"), "text": "Hello there"})
    assert 'value="Hello there"' in line_of(s, "Your message")
    s = browser.run({"action": "click", "ref": ref_of(s, '"Send message"')})
    assert "Message: Hello there" in s  # the frame's page changed; its text is shown


def test_fill_form_input_checks(browser, site):
    browser.run({"action": "open", "url": site + "/survey"})
    with pytest.raises(ValueError, match="fields"):
        browser.run({"action": "fill_form", "fields": []})
    s = browser.run({"action": "fill_form", "fields": [{"ref": 9999, "value": "x"}]})
    assert "filled 0 of 1 fields" in s and "isn't on the page any more" in s


def test_activity_wording_for_forms():
    labels = {1: "Full name", 2: "Email", 3: "Phone", 4: "Team"}
    two = {"action": "fill_form", "fields": [{"ref": 1, "value": "a"}, {"ref": 2, "value": "b"}]}
    four = {"action": "fill_form", "fields": [{"ref": i, "value": "a"} for i in (1, 2, 3, 4)]}
    assert describe_step("browser", two, labels) == "Filled in “Full name” and “Email”"
    assert describe_step("browser", four, labels) == "Filled in “Full name”, “Email” and 2 more"
    # the feed never shows what was typed
    assert "a" not in describe_step("browser", two, labels).replace("Filled in", "").replace("Full name", "").replace("Email", "").replace("and", "")


def test_agent_fills_a_form_and_feed_stays_plain(tmp_path, site):
    import io
    from types import SimpleNamespace as NS
    from taskrunner.activity import Events, read_events
    from taskrunner.agent import run_agent
    from tests.test_agent import FakeClient, text, tool_use

    b = BrowserSession(tmp_path / "profile", headless=True)
    first = b.run({"action": "open", "url": site + "/survey"})  # so the scripted refs are known
    name, mail = ref_of(first, "full name"), ref_of(first, "Which email")
    ev = Events(tmp_path / "events.jsonl")
    client = FakeClient([
        NS(stop_reason="tool_use", content=[tool_use("b1", "browser", action="read")]),
        NS(stop_reason="tool_use", content=[tool_use("b2", "browser", action="fill_form", fields=[
            {"ref": name, "value": "Ada Lovelace"}, {"ref": mail, "value": "ada@example.com"}])]),
        NS(stop_reason="end_turn", content=[text("I filled in your name and email. Want me to send it?")]),
    ])
    assert run_agent("sign me up", tmp_path / "ws", client=client, out=io.StringIO(),
                     browser=b, web=False, events=ev) == 0
    steps = [e["text"] for e in read_events(tmp_path / "events.jsonl") if e["type"] == "step"]
    assert "Filled in “What is your full name? *” and “Which email should we use?”" in steps
    assert not any("Ada" in s or "example.com" in s for s in steps)  # values never shown in the feed
    rules = client.requests[0]["system"]
    assert "Never make up personal details" in rules and "ask the user" in rules
