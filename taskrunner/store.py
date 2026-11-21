"""SQLite-backed job storage."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'shell',   -- 'shell' | 'agent'
    command     TEXT NOT NULL,                   -- shell command, or the agent's task text
    provider    TEXT,                            -- unused (was a choice of AI)
    model       TEXT,                            -- unused
    cwd         TEXT,
    timeout     REAL,
    status      TEXT NOT NULL DEFAULT 'queued',
    exit_code   INTEGER,
    error       TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    started_at  REAL,
    finished_at REAL,
    folder_id   INTEGER                          -- the task folder it belongs to (or none)
);
CREATE TABLE IF NOT EXISTS folders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    created_at  REAL NOT NULL
);
"""

class _Result:
    """A finished query: its rows, plus how many rows it changed and the new row's id."""

    def __init__(self, rows: list, rowcount: int, lastrowid: int | None):
        self._rows, self.rowcount, self.lastrowid = rows, rowcount, lastrowid

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list:
        return self._rows


# queued -> running -> succeeded | failed | cancelled | timed_out
ACTIVE = ("queued", "running")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(jobs)")}
            if "kind" not in cols:  # upgrade databases created before agent jobs existed
                self._conn.execute("ALTER TABLE jobs ADD COLUMN kind TEXT NOT NULL DEFAULT 'shell'")
            for col in ("provider", "model"):  # (unused columns, kept so old databases still load)
                if col not in cols:
                    self._conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} TEXT")
            if "folder_id" not in cols:  # upgrade databases created before task folders
                self._conn.execute("ALTER TABLE jobs ADD COLUMN folder_id INTEGER")
            self._conn.commit()

    def _exec(self, sql: str, args: tuple = ()) -> "_Result":
        # The connection is shared by the web server's threads and the job runner, so read the
        # results while still holding the lock - reading them afterwards could pick up another
        # thread's query instead.
        with self._lock:
            cur = self._conn.execute(sql, args)
            rows = cur.fetchall()
            self._conn.commit()
            return _Result(rows, cur.rowcount, cur.lastrowid)

    def create(self, name: str, command: str, cwd: str | None, timeout: float | None,
               kind: str = "shell", provider: str | None = None, model: str | None = None,
               folder_id: int | None = None) -> dict:
        cur = self._exec(
            "INSERT INTO jobs (name, kind, command, cwd, timeout, provider, model, folder_id, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (name, kind, command, cwd, timeout, provider, model, folder_id, time.time()),
        )
        return self.get(cur.lastrowid)

    def get(self, job_id: int) -> dict | None:
        row = self._exec("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def list(self, status: str | None = None, limit: int = 200, folder: int | str | None = None) -> list[dict]:
        """folder: a folder id, "none" for tasks in no folder, or None for every task."""
        where, args = [], []
        if status:
            where.append("status=?"); args.append(status)
        if folder == "none":
            where.append("folder_id IS NULL")
        elif folder is not None:
            where.append("folder_id=?"); args.append(int(folder))
        sql = "SELECT * FROM jobs" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
        return [dict(r) for r in self._exec(sql, (*args, limit)).fetchall()]

    # ---------------------------------------------------------------- task folders
    def create_folder(self, name: str) -> dict:
        cur = self._exec("INSERT INTO folders (name, created_at) VALUES (?,?)", (name, time.time()))
        return self.get_folder(cur.lastrowid)

    def get_folder(self, folder_id: int) -> dict | None:
        row = self._exec("SELECT * FROM folders WHERE id=?", (folder_id,)).fetchone()
        return dict(row) if row else None

    def folders(self) -> list[dict]:
        rows = self._exec(
            "SELECT f.*, COUNT(j.id) AS tasks, SUM(j.status IN ('queued','running')) AS active "
            "FROM folders f LEFT JOIN jobs j ON j.folder_id = f.id GROUP BY f.id ORDER BY f.name COLLATE NOCASE"
        ).fetchall()
        return [{**dict(r), "active": dict(r)["active"] or 0} for r in rows]

    def rename_folder(self, folder_id: int, name: str) -> None:
        self._exec("UPDATE folders SET name=? WHERE id=?", (name, folder_id))

    def delete_folder(self, folder_id: int) -> None:
        """Its tasks stay, just no longer in a folder."""
        self._exec("UPDATE jobs SET folder_id=NULL WHERE folder_id=?", (folder_id,))
        self._exec("DELETE FROM folders WHERE id=?", (folder_id,))

    def queued_ids(self) -> list[int]:
        rows = self._exec("SELECT id FROM jobs WHERE status='queued' ORDER BY id").fetchall()
        return [r["id"] for r in rows]

    def update(self, job_id: int, **fields: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))

    def claim(self, job_id: int) -> bool:
        """Atomically move a queued job to running. False if it was cancelled meanwhile."""
        cur = self._exec(
            "UPDATE jobs SET status='running', started_at=?, attempts=attempts+1, "
            "exit_code=NULL, error=NULL, finished_at=NULL WHERE id=? AND status='queued'",
            (time.time(), job_id),
        )
        return cur.rowcount == 1

    def requeue(self, job_id: int) -> bool:
        cur = self._exec(
            "UPDATE jobs SET status='queued', started_at=NULL, finished_at=NULL, "
            "exit_code=NULL, error=NULL WHERE id=? AND status NOT IN ('queued','running')",
            (job_id,),
        )
        return cur.rowcount == 1

    def recover_interrupted(self) -> int:
        """Jobs left 'running' by a previous crash/restart are marked failed."""
        cur = self._exec(
            "UPDATE jobs SET status='failed', error='interrupted by server restart', "
            "finished_at=? WHERE status='running'",
            (time.time(),),
        )
        return cur.rowcount

    def delete(self, job_id: int) -> None:
        self._exec("DELETE FROM jobs WHERE id=?", (job_id,))
