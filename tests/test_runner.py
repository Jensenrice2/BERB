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
