import sqlite3
import time

from fastapi.testclient import TestClient

from taskrunner.app import create_app
from taskrunner.store import Store


def wait_done(c, job_id, t=30):
    end = time.time() + t
    while c.get(f"/api/jobs/{job_id}").json()["status"] in ("queued", "running"):
        assert time.time() < end
        time.sleep(0.05)


def test_folders_group_tasks_and_share_files(tmp_path):
    with TestClient(create_app(tmp_path)) as c:
        trip = c.post("/api/folders", json={"name": "Trip planning"}).json()
        work = c.post("/api/folders", json={"name": "  Job search "}).json()
        assert work["name"] == "Job search"
        assert c.post("/api/folders", json={"name": ""}).status_code == 422

        # two tasks in the same folder work in the same file space
        a = c.post("/api/jobs", json={"command": "echo flights > flights.txt", "folder_id": trip["id"]}).json()
        wait_done(c, a["id"])
        b = c.post("/api/jobs", json={"command": "cat flights.txt", "folder_id": trip["id"]}).json()
        wait_done(c, b["id"])
        assert "flights" in c.get(f"/api/jobs/{b['id']}/logs").json()["text"]
        assert a["cwd"] == b["cwd"] and a["cwd"].endswith("files")
        loose = c.post("/api/jobs", json={"command": "true"}).json()
        wait_done(c, loose["id"])

        assert [j["id"] for j in c.get(f"/api/jobs?folder={trip['id']}").json()] == [b["id"], a["id"]]
        assert [j["id"] for j in c.get("/api/jobs?folder=none").json()] == [loose["id"]]
        assert len(c.get("/api/jobs").json()) == 3
        assert c.get("/api/jobs?folder=abc").status_code == 400
        assert c.post("/api/jobs", json={"command": "true", "folder_id": 999}).status_code == 404

        folders = {f["name"]: f for f in c.get("/api/folders").json()}
        assert list(folders) == ["Job search", "Trip planning"]           # alphabetical
        assert folders["Trip planning"]["tasks"] == 2 and folders["Job search"]["tasks"] == 0

        assert c.patch(f"/api/folders/{trip['id']}", json={"name": "Italy trip"}).json()["name"] == "Italy trip"
        assert c.delete(f"/api/folders/{trip['id']}").status_code == 204
        assert c.get(f"/api/jobs/{a['id']}").json()["folder_id"] is None     # the tasks stay
        assert c.delete(f"/api/folders/{trip['id']}").status_code == 404
        # other websites can't change folders
        assert c.post("/api/folders", json={"name": "x"}, headers={"origin": "https://evil.example"}).status_code == 403


def test_old_database_gets_folders(tmp_path):
    db = tmp_path / "jobs.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, command TEXT NOT NULL, "
                "cwd TEXT, timeout REAL, status TEXT NOT NULL DEFAULT 'queued', exit_code INTEGER, error TEXT, "
                "attempts INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, started_at REAL, finished_at REAL)")
    con.execute("INSERT INTO jobs (name, command, created_at) VALUES ('old', 'true', 1)")
    con.commit(); con.close()
    s = Store(db)
    assert s.get(1)["folder_id"] is None and s.get(1)["kind"] == "shell"
    f = s.create_folder("New")
    s.update(1, folder_id=f["id"])
    assert s.folders()[0]["tasks"] == 1
