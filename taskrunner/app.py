"""HTTP API + dashboard for the background task runner."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .runner import Runner
from .store import Store

DATA_DIR = Path(os.environ.get("TASKRUNNER_DATA", Path.cwd() / "data"))
CONCURRENCY = int(os.environ.get("TASKRUNNER_CONCURRENCY", "2"))
STATIC = Path(__file__).parent / "static"


class JobIn(BaseModel):
    kind: Literal["shell", "agent"] = "shell"
    command: str | None = Field(default=None, description="Shell command to run (kind=shell)")
    task: str | None = Field(default=None, description="Plain-English task for the AI agent (kind=agent)")
    name: str | None = None
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


def create_app(data_dir: Path = DATA_DIR, concurrency: int = CONCURRENCY) -> FastAPI:
    store = Store(data_dir / "jobs.db")
    runner = Runner(store, data_dir / "logs", concurrency)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await runner.start()
        yield
        await runner.stop()

    app = FastAPI(title="Task Runner", lifespan=lifespan)
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

    @app.get("/api/status")
    def status():
        counts: dict[str, int] = {}
        for j in store.list(limit=10_000):
            counts[j["status"]] = counts.get(j["status"], 0) + 1
        return {"concurrency": concurrency, "running": runner.running, "counts": counts}

    @app.post("/api/jobs", status_code=201)
    async def create_job(body: JobIn):
        if body.cwd and not Path(body.cwd).is_dir():
            raise HTTPException(400, f"cwd does not exist: {body.cwd}")
        name = body.name or body.text.splitlines()[0][:60]
        job = store.create(name, body.text, body.cwd, body.timeout, body.kind)
        runner.submit(job["id"])
        return job

    @app.get("/api/jobs")
    def list_jobs(status: str | None = None, limit: int = Query(200, le=1000)):
        return store.list(status, limit)

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: int):
        return _job_or_404(job_id)

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

    return app


app = create_app()
