"""Background job runner so long encodes don't block HTTP requests or the assistant."""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from . import ops
from .config import get_settings
from .storage import FileInfo


class Job(BaseModel):
    id: str
    operation: str
    params: dict[str, Any]
    status: Literal["queued", "running", "succeeded", "failed"] = "queued"
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    outputs: list[FileInfo] = []
    data: dict[str, Any] = {}
    error: str | None = None


class JobQueue:
    def __init__(self, workers: int | None = None, max_history: int = 500):
        self._pool = ThreadPoolExecutor(max_workers=workers or get_settings().workers, thread_name_prefix="media-job")
        self._jobs: dict[str, Job] = {}
        self._futures: dict[str, Future] = {}
        self._lock = threading.Lock()
        self._max_history = max_history

    def submit(self, operation: str, params: dict[str, Any]) -> Job:
        op = ops.REGISTRY.get(operation)
        if op is None:
            raise ops.OpError(f"Unknown operation '{operation}'. Available: {', '.join(sorted(ops.REGISTRY))}")
        parsed = op.params.model_validate(params)  # fail fast on bad params
        job = Job(id=uuid.uuid4().hex[:12], operation=operation, params=parsed.model_dump(), created_at=time.time())
        with self._lock:
            self._jobs[job.id] = job
            self._trim()
            self._futures[job.id] = self._pool.submit(self._run, job.id, parsed)
        return job

    def _run(self, job_id: str, parsed: BaseModel) -> None:
        job = self._jobs[job_id]
        job.status, job.started_at = "running", time.time()
        try:
            result = ops.run_operation(job.operation, parsed)
            job.outputs, job.data, job.status = result.outputs, result.data, "succeeded"
        except (ops.OpError, ValidationError, ValueError) as e:
            job.error, job.status = str(e), "failed"
        except Exception as e:  # noqa: BLE001 - report anything else on the job instead of losing it
            job.error, job.status = f"{type(e).__name__}: {e}", "failed"
        finally:
            job.finished_at = time.time()

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def wait(self, job_id: str, timeout: float | None = None) -> Job | None:
        fut = self._futures.get(job_id)
        if fut is not None:
            try:
                fut.result(timeout=timeout)
            except Exception:
                pass
        return self._jobs.get(job_id)

    def list(self, limit: int = 50) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)[:limit]

    def run_and_wait(self, operation: str, params: dict[str, Any], timeout: float | None = None) -> Job:
        job = self.submit(operation, params)
        return self.wait(job.id, timeout) or job

    def _trim(self) -> None:
        done = [j for j in self._jobs.values() if j.status in ("succeeded", "failed")]
        excess = len(self._jobs) - self._max_history
        for j in sorted(done, key=lambda j: j.created_at)[: max(0, excess)]:
            self._jobs.pop(j.id, None)
            self._futures.pop(j.id, None)


_queue: JobQueue | None = None


def get_queue() -> JobQueue:
    global _queue
    if _queue is None:
        _queue = JobQueue()
    return _queue
