"""HTTP API + dashboard for the background task runner."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse
from urllib.parse import urlparse
from fastapi.staticfiles import StaticFiles
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from . import oscompat
from .activity import pending_question, read_events, write_answer
from .runner import Runner
from .store import Store
from .vault import Vault, VaultUnavailable

# Tasks live in the app's own folder (next to .env), wherever it was started from.
DATA_DIR = Path(os.environ.get("TASKRUNNER_DATA", Path(__file__).resolve().parent.parent / "data"))
CONCURRENCY = int(os.environ.get("TASKRUNNER_CONCURRENCY", "2"))
STATIC = Path(__file__).parent / "static"


class JobIn(BaseModel):
    kind: Literal["shell", "agent"] = "shell"
    command: str | None = Field(default=None, description="Shell command to run (kind=shell)")
    task: str | None = Field(default=None, description="Plain-English task for the AI agent (kind=agent)")
    name: str | None = None
    folder_id: int | None = Field(default=None, description="Put the task in this folder (and its file space)")
    cwd: str | None = None
    timeout: float | None = Field(default=None, gt=0, description="Seconds before the job is killed")

    @model_validator(mode="after")
    def _check(self):
        text = self.task if self.kind == "agent" else self.command
        if not text or not text.strip():
            raise ValueError("agent jobs need a 'task'" if self.kind == "agent" else "shell jobs need a 'command'")
        return self

    @property
    def text(self) -> str:
        return (self.task if self.kind == "agent" else self.command).strip()


def is_local_host(host: str) -> bool:
    """True for addresses that can only mean this computer: localhost, 127.0.0.1, ::1, *.localhost."""
    host = (host or "").strip().lower()
    if host.startswith("["):  # [::1]:80
        name = host[1:host.find("]")] if "]" in host else host
    else:
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return (name in ("localhost", "127.0.0.1", "::1", "testserver")  # testserver: the test client
            or name.endswith(".localhost"))


class FolderIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class LoginIn(BaseModel):
    site: str = Field(min_length=1, max_length=300)
    username: str = Field(min_length=1, max_length=300)
    password: str = Field(min_length=1, max_length=1000)


class SettingIn(BaseModel):
    enabled: bool


class AnswerIn(BaseModel):
    answer: str = Field(min_length=1, max_length=5000)


def create_app(data_dir: Path = DATA_DIR, concurrency: int = CONCURRENCY, vault: Vault | None = None) -> FastAPI:
    store = Store(data_dir / "jobs.db")
    runner = Runner(store, data_dir / "logs", concurrency)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        import asyncio
        if oscompat.IS_WINDOWS and not isinstance(asyncio.get_running_loop(), asyncio.ProactorEventLoop):
            raise RuntimeError("On Windows the server must run on the Proactor event loop to start jobs. "
                               "Start it with: python -m taskrunner.cli serve")
        await runner.start()
        yield
        await runner.stop()

    app = FastAPI(title="Task Runner", lifespan=lifespan)

    @app.middleware("http")
    async def only_this_computer(request: Request, call_next):
        """Other websites must not be able to drive B.E.R.B through your browser.
        - Host check: blocks "DNS rebinding", where a site makes the browser treat this app as part of it.
        - Origin check: blocks pages on other sites from pressing buttons (stop, retry, delete, answer...)."""
        if not is_local_host(request.headers.get("host", "")):
            return JSONResponse({"detail": "B.E.R.B only answers to http://berb.localhost or http://localhost"}, 403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and origin != "null" and not is_local_host(urlparse(origin).netloc):
                return JSONResponse({"detail": "requests from other websites aren't allowed"}, 403)
            if origin == "null":
                return JSONResponse({"detail": "requests from other websites aren't allowed"}, 403)
        return await call_next(request)
    app.state.store, app.state.runner = store, runner

    def _job_or_404(job_id: int) -> dict:
        job = store.get(job_id)
        if not job:
            raise HTTPException(404, "job not found")
        return job

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    # Installable-app files. The service worker must be served from the root so it covers the whole app.
    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest():
        return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/sw.js", include_in_schema=False)
    def service_worker():
        return FileResponse(STATIC / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return FileResponse(STATIC / "icons" / "favicon-32.png", media_type="image/png")

    vault = vault or Vault(data_dir)
    app.state.vault = vault

    @app.get("/api/logins")
    def list_logins():
        """Saved logins - sites and usernames only, never passwords."""
        return {"enabled": vault.enabled(), "available": vault.available(), "logins": vault.logins()}

    @app.put("/api/logins/settings")
    def set_remember(body: SettingIn):
        vault.set_enabled(body.enabled)
        return {"enabled": vault.enabled()}

    @app.post("/api/logins", status_code=201)
    def save_login(body: LoginIn):
        if not vault.enabled():
            raise HTTPException(409, "turn on Remember passwords first")
        try:
            return vault.save(body.site, body.username, body.password)
        except VaultUnavailable as e:
            raise HTTPException(503, f"Can't save passwords: {e}")
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.delete("/api/logins", status_code=204)
    def delete_login(site: str, username: str):
        if not vault.delete(site, username):
            raise HTTPException(404, "no such saved login")

    @app.post("/api/quit")
    async def quit_app():
        """The dashboard's "Turn off" button. Running tasks are stopped."""
        server = getattr(app.state, "server", None)
        if server is None:
            raise HTTPException(409, "B.E.R.B wasn't started in a way that can be turned off from here")
        server.should_exit = True
        return {"ok": True}

    @app.get("/api/status")
    def status():
        counts: dict[str, int] = {}
        for j in store.list(limit=10_000):
            counts[j["status"]] = counts.get(j["status"], 0) + 1
        return {"concurrency": concurrency, "running": runner.running, "counts": counts,
                "shell": oscompat.SHELL_NAME}

    def _folder(job_id: int) -> Path:
        return runner.agent_dir / str(job_id)

    def _with_question(job: dict) -> dict:
        """Adds `question`: what the AI is waiting for the user to answer, if anything."""
        job["question"] = (pending_question(_folder(job["id"]))
                           if job["kind"] == "agent" and job["status"] == "running" else None)
        return job

    @app.post("/api/jobs", status_code=201)
    async def create_job(body: JobIn):
        if body.cwd and not Path(body.cwd).is_dir():
            raise HTTPException(400, f"cwd does not exist: {body.cwd}")
        name = body.name or body.text.splitlines()[0][:60]
        cwd = body.cwd
        if body.folder_id is not None:
            if not store.get_folder(body.folder_id):
                raise HTTPException(404, "no such folder")
            if not cwd:  # tasks in a folder share its file space, so they can build on each other's work
                cwd = str(_folder_files(body.folder_id))
        job = store.create(name, body.text, cwd, body.timeout, body.kind, folder_id=body.folder_id)
        runner.submit(job["id"])
        return job

    @app.get("/api/jobs")
    def list_jobs(status: str | None = None, limit: int = Query(200, le=1000), folder: str | None = None):
        if folder not in (None, "", "none") and not folder.isdigit():
            raise HTTPException(400, "folder must be a folder id or 'none'")
        return [_with_question(j) for j in store.list(status, limit, folder or None)]

    # ---------------------------------------------------------------- task folders
    def _folder_files(folder_id: int) -> Path:
        path = data_dir / "folders" / str(folder_id) / "files"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @app.get("/api/folders")
    def list_folders():
        return store.folders()

    @app.post("/api/folders", status_code=201)
    def create_folder(body: FolderIn):
        folder = store.create_folder(body.name.strip())
        _folder_files(folder["id"])
        return folder

    @app.patch("/api/folders/{folder_id}")
    def rename_folder(folder_id: int, body: FolderIn):
        if not store.get_folder(folder_id):
            raise HTTPException(404, "no such folder")
        store.rename_folder(folder_id, body.name.strip())
        return store.get_folder(folder_id)

    @app.delete("/api/folders/{folder_id}", status_code=204)
    def delete_folder(folder_id: int):
        """The folder goes; its tasks stay (in no folder), and its files stay on disk."""
        if not store.get_folder(folder_id):
            raise HTTPException(404, "no such folder")
        store.delete_folder(folder_id)

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: int):
        return _with_question(_job_or_404(job_id))

    @app.get("/api/jobs/{job_id}/events")
    def get_events(job_id: int, after: int = Query(0, ge=0)):
        """The plain-English activity feed. Poll with `after` = the `next` from the last call."""
        job = _with_question(_job_or_404(job_id))
        events = read_events(_folder(job_id) / "events.jsonl", after) if job["kind"] == "agent" else []
        return {"events": events, "next": after + len(events), "status": job["status"],
                "question": job["question"]}

    @app.post("/api/jobs/{job_id}/answer")
    def answer(job_id: int, body: AnswerIn):
        job = _with_question(_job_or_404(job_id))
        if not job["question"]:
            raise HTTPException(409, "this task isn't waiting for an answer")
        write_answer(_folder(job_id), body.answer.strip())
        return {"ok": True}

    def _workspace(job: dict) -> Path:
        if job["kind"] != "agent":
            raise HTTPException(400, "only agent jobs have a workspace")
        return runner.workspace_for(job).resolve()

    @app.get("/api/jobs/{job_id}/files")
    def list_files(job_id: int):
        """Files in an agent job's workspace."""
        root = _workspace(_job_or_404(job_id))
        if not root.is_dir():
            return {"workspace": str(root), "files": []}
        files = [
            {"path": str(p.relative_to(root)), "size": p.stat().st_size}
            for p in sorted(root.rglob("*"))
            if p.is_file() and not any(part.startswith(".") for part in p.relative_to(root).parts)
        ][:500]
        return {"workspace": str(root), "files": files}

    @app.get("/api/jobs/{job_id}/files/{path:path}")
    def get_file(job_id: int, path: str):
        root = _workspace(_job_or_404(job_id))
        target = (root / path).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise HTTPException(404, "file not found")
        return FileResponse(target, filename=target.name)

    @app.get("/api/jobs/{job_id}/logs")
    def get_logs(job_id: int, offset: int = Query(0, ge=0)):
        """Returns log text from byte `offset`; poll with the returned `next_offset` to tail."""
        job = _job_or_404(job_id)
        path = runner.log_path(job_id)
        if not path.exists():
            return {"text": "", "next_offset": 0, "status": job["status"]}
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read(1_000_000)
        return {
            "text": data.decode(errors="replace"),
            "next_offset": offset + len(data),
            "status": job["status"],
        }

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: int):
        _job_or_404(job_id)
        if not runner.cancel(job_id):
            raise HTTPException(409, "job is not queued or running")
        return store.get(job_id)

    @app.post("/api/jobs/{job_id}/retry")
    async def retry_job(job_id: int):
        _job_or_404(job_id)
        if not store.requeue(job_id):
            raise HTTPException(409, "job is still active")
        runner.submit(job_id)
        return store.get(job_id)

    @app.delete("/api/jobs/{job_id}", status_code=204)
    def delete_job(job_id: int):
        job = _job_or_404(job_id)
        if job["status"] in ("queued", "running"):
            raise HTTPException(409, "cancel the job before deleting it")
        store.delete(job_id)
        runner.log_path(job_id).unlink(missing_ok=True)
        for name in ("events.jsonl", "question.json", "answer.json"):
            (_folder(job_id) / name).unlink(missing_ok=True)

    return app


app = create_app()
