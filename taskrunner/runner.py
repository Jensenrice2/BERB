"""Background worker pool that executes jobs as shell subprocesses."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

from . import oscompat
from .store import Store

log = logging.getLogger("taskrunner")


class Runner:
    def __init__(self, store: Store, logs_dir: Path, concurrency: int = 2):
        self.store = store
        self.logs_dir = logs_dir
        self.agent_dir = logs_dir.parent / "agent"  # task files + default workspaces
        self.concurrency = concurrency
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._procs: dict[int, asyncio.subprocess.Process] = {}
        self._cancelled: set[int] = set()
        self._workers: list[asyncio.Task] = []

    def log_path(self, job_id: int) -> Path:
        return self.logs_dir / f"{job_id}.log"

    async def start(self) -> None:
        n = self.store.recover_interrupted()
        if n:
            log.warning("marked %d interrupted job(s) as failed", n)
        for job_id in self.store.queued_ids():
            self._queue.put_nowait(job_id)
        self._workers = [
            asyncio.create_task(self._worker(i)) for i in range(self.concurrency)
        ]

    async def stop(self) -> None:
        for job_id in list(self._procs):
            self.cancel(job_id)
        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)

    def submit(self, job_id: int) -> None:
        self._queue.put_nowait(job_id)

    def cancel(self, job_id: int) -> bool:
        job = self.store.get(job_id)
        if not job or job["status"] not in ("queued", "running"):
            return False
        if job["status"] == "queued":
            self.store.update(job_id, status="cancelled", finished_at=time.time())
            return True
        self._cancelled.add(job_id)
        proc = self._procs.get(job_id)
        if proc:
            _kill_group(proc)
        return True

    @property
    def running(self) -> int:
        return len(self._procs)

    async def _worker(self, idx: int) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                if self.store.claim(job_id):
                    await self._run(job_id)
            except Exception as e:  # never let a worker die
                log.exception("worker %d crashed on job %d", idx, job_id)
                self.store.update(job_id, status="failed", error=str(e), finished_at=time.time())
            finally:
                self._queue.task_done()

    async def _run(self, job_id: int) -> None:
        job = self.store.get(job_id)
        path = self.log_path(job_id)
        with open(path, "ab") as out:
            header = f"--- attempt {job['attempts']} started {time.ctime()} ---\n"
            if job["kind"] != "agent":
                header += f"$ {job['command']}\n"
            out.write(header.encode())
            out.flush()
            try:
                if job["kind"] == "agent":
                    proc = await self._spawn_agent(job, out)
                else:
                    proc = await self._spawn_shell(job, out)
            except OSError as e:
                self.store.update(job_id, status="failed", error=str(e), finished_at=time.time())
                return

            self._procs[job_id] = proc
            status, error = None, None
            try:
                await asyncio.wait_for(proc.wait(), timeout=job["timeout"])
            except asyncio.TimeoutError:
                _kill_group(proc)
                await proc.wait()
                status, error = "timed_out", f"exceeded timeout of {job['timeout']}s"
            finally:
                self._procs.pop(job_id, None)

            if job_id in self._cancelled:
                self._cancelled.discard(job_id)
                status, error = "cancelled", "cancelled by user"
            elif status is None:
                status = "succeeded" if proc.returncode == 0 else "failed"

            out.write(f"\n--- finished: {status} (exit {proc.returncode}) ---\n".encode())

        self.store.update(
            job_id, status=status, exit_code=proc.returncode, error=error, finished_at=time.time()
        )

    async def _spawn_shell(self, job: dict, out) -> asyncio.subprocess.Process:
        # Own process group, so cancel/timeout can stop everything the job started.
        common = dict(cwd=job["cwd"] or None, stdout=out, stderr=asyncio.subprocess.STDOUT,
                      stdin=asyncio.subprocess.DEVNULL, **oscompat.new_group_kwargs())
        if oscompat.IS_WINDOWS:
            return await asyncio.create_subprocess_exec(*oscompat.powershell_job_argv(job["command"]), **common)
        return await asyncio.create_subprocess_shell(job["command"], **common)

    def workspace_for(self, job: dict) -> Path:
        return Path(job["cwd"]) if job["cwd"] else self.agent_dir / str(job["id"]) / "workspace"

    async def _spawn_agent(self, job: dict, out) -> asyncio.subprocess.Process:
        task_file = self.agent_dir / str(job["id"]) / "task.md"
        task_file.parent.mkdir(parents=True, exist_ok=True)
        task_file.write_text(job["command"], encoding="utf-8")
        workspace = self.workspace_for(job)
        workspace.mkdir(parents=True, exist_ok=True)
        pkg_root = str(Path(__file__).resolve().parent.parent)
        env = {**os.environ, "PYTHONIOENCODING": "utf-8",
               "PYTHONPATH": pkg_root + os.pathsep + os.environ.get("PYTHONPATH", "")}
        return await asyncio.create_subprocess_exec(
            sys.executable, "-u", "-m", "taskrunner.agent",
            "--workspace", str(workspace), "--task-file", str(task_file),
            cwd=workspace, env=env, stdout=out, stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL, **oscompat.new_group_kwargs(),
        )


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    """Stop a job's whole process tree: politely first on macOS/Linux, forcefully after 5s."""
    if proc.returncode is not None:
        return
    oscompat.kill_tree(proc.pid)
    if oscompat.IS_WINDOWS:
        return  # taskkill /F is already final

    async def _escalate():
        await asyncio.sleep(5)
        if proc.returncode is None:
            oscompat.kill_tree(proc.pid, force=True)

    asyncio.get_running_loop().create_task(_escalate())
