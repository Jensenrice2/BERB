import time

import pytest
from fastapi.testclient import TestClient

from taskrunner.app import create_app
from taskrunner.oscompat import IS_WINDOWS

# The same job, written for the shell each OS uses (bash on macOS/Linux, PowerShell on Windows).
OUT_AND_ERR = ("Write-Output hello; [Console]::Error.WriteLine('oops')" if IS_WINDOWS
               else "echo hello; echo oops >&2")


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path, concurrency=2)) as c:
        yield c


def wait(client, job_id, until=("succeeded", "failed", "cancelled", "timed_out"), t=30):
    end = time.time() + t
    while time.time() < end:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] in until:
            return j
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} stuck in {j['status']}")


def test_success_and_logs(client):
    j = client.post("/api/jobs", json={"command": OUT_AND_ERR}).json()
    j = wait(client, j["id"])
    assert j["status"] == "succeeded" and j["exit_code"] == 0
    text = client.get(f"/api/jobs/{j['id']}/logs").json()["text"]
    assert "hello" in text and "oops" in text


@pytest.mark.skipif(not IS_WINDOWS, reason="PowerShell error formatting is Windows-only")
def test_powershell_errors_are_readable(client):
    j = wait(client, client.post("/api/jobs", json={"command": "Write-Error 'boom'"}).json()["id"])
    text = client.get(f"/api/jobs/{j['id']}/logs").json()["text"]
    assert j["status"] == "failed" and "ERROR: boom" in text and "CLIXML" not in text


def test_failure(client):
    j = wait(client, client.post("/api/jobs", json={"command": "exit 3"}).json()["id"])
    assert j["status"] == "failed" and j["exit_code"] == 3


def test_timeout(client):
    j = client.post("/api/jobs", json={"command": "sleep 30", "timeout": 0.3}).json()
    assert wait(client, j["id"])["status"] == "timed_out"


def test_cancel_running_and_retry(client):
    j = client.post("/api/jobs", json={"command": "sleep 30"}).json()
    wait(client, j["id"], until=("running",))
    assert client.post(f"/api/jobs/{j['id']}/cancel").status_code == 200
    assert wait(client, j["id"])["status"] == "cancelled"
    client.post(f"/api/jobs/{j['id']}/retry")
    client.post(f"/api/jobs/{j['id']}/cancel")
    j = wait(client, j["id"])
    assert j["status"] == "cancelled" and j["attempts"] >= 1


def test_concurrency_limit_and_queue_cancel(client):
    ids = [client.post("/api/jobs", json={"command": "sleep 30"}).json()["id"] for _ in range(3)]
    wait(client, ids[0], until=("running",)); wait(client, ids[1], until=("running",))
    assert client.get(f"/api/jobs/{ids[2]}").json()["status"] == "queued"
    client.post(f"/api/jobs/{ids[2]}/cancel")
    assert client.get(f"/api/jobs/{ids[2]}").json()["status"] == "cancelled"
    for i in ids[:2]:
        client.post(f"/api/jobs/{i}/cancel")


def test_restart_recovers_queue(tmp_path):
    app = create_app(tmp_path, concurrency=1)
    app.state.store.create("pending", "echo resumed", None, None)  # queued, never submitted
    with TestClient(app) as c:
        j = wait(c, 1)
    assert j["status"] == "succeeded"


def test_delete_requires_inactive(client):
    j = client.post("/api/jobs", json={"command": "sleep 30"}).json()
    wait(client, j["id"], until=("running",))
    assert client.delete(f"/api/jobs/{j['id']}").status_code == 409
    client.post(f"/api/jobs/{j['id']}/cancel")
    wait(client, j["id"])
    assert client.delete(f"/api/jobs/{j['id']}").status_code == 204


def test_concurrent_requests_never_mix_up_jobs(tmp_path):
    """Regression: the shared database connection used to hand one request another's result."""
    from concurrent.futures import ThreadPoolExecutor
    from taskrunner.store import Store
    store = Store(tmp_path / "jobs.db")
    ids = [store.create(f"job {i}", "true", None, None)["id"] for i in range(20)]

    def check(n):
        job_id = ids[n % len(ids)]
        for _ in range(50):
            j = store.get(job_id)
            assert j is not None and j["id"] == job_id and j["name"] == f"job {job_id - 1}"
            assert len(store.list(limit=5)) == 5
        return True

    with ThreadPoolExecutor(16) as pool:
        assert all(pool.map(check, range(64)))


def test_other_websites_cannot_drive_the_app(tmp_path):
    from taskrunner.app import is_local_host
    for ok in ("berb.localhost", "berb.localhost:8765", "localhost", "127.0.0.1:80", "[::1]:8765"):
        assert is_local_host(ok), ok
    for bad in ("evil.example", "127.0.0.1.evil.example", "localhost.evil.example", "berb.localhost.evil.com:80", ""):
        assert not is_local_host(bad), bad

    with TestClient(create_app(tmp_path)) as c:
        j = c.post("/api/jobs", json={"command": "sleep 30"}).json()
        # DNS rebinding: a request addressed to some other site's name
        assert c.get("/api/jobs", headers={"host": "attacker.example"}).status_code == 403
        # a page on another site pressing buttons
        for path in (f"/api/jobs/{j['id']}/cancel", f"/api/jobs/{j['id']}/retry"):
            assert c.post(path, headers={"origin": "https://evil.example"}).status_code == 403
        assert c.post(f"/api/jobs/{j['id']}/cancel", headers={"origin": "null"}).status_code == 403
        assert c.delete(f"/api/jobs/{j['id']}", headers={"origin": "https://evil.example"}).status_code == 403
        # the app's own page (same origin) and the command line (no origin) still work
        assert c.get("/api/jobs", headers={"host": "berb.localhost"}).status_code == 200
        assert c.post(f"/api/jobs/{j['id']}/cancel", headers={"origin": "http://berb.localhost"}).status_code == 200
