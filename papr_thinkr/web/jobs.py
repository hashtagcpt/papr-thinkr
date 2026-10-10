"""Background jobs for long-running work (a thorough review on a local model
can take an hour). The server starts a job and returns immediately; the UI
polls /api/jobs/<id> for progress and can cancel it."""
from __future__ import annotations

import threading
import time
import traceback
import uuid

from ..llm import CallContext, LLMCancelled, use_context


class Job:
    def __init__(self, kind: str, slug: str | None, title: str):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.slug = slug
        self.title = title
        self.status = "queued"        # queued | running | done | error | cancelled
        self.progress: float | None = None
        self.log: list[dict] = []
        self.result = None
        self.error: str | None = None
        self.created_at = time.time()
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.cancel_event = threading.Event()
        self.ctx = CallContext(self.cancel_event)
        self._lock = threading.Lock()

    def say(self, message: str, progress: float | None = None) -> None:
        with self._lock:
            self.log.append({"t": time.time(), "msg": message})
            if len(self.log) > 500:
                self.log = self.log[:20] + self.log[-400:]
            if progress is not None:
                self.progress = max(0.0, min(1.0, progress))

    def as_dict(self, log_since: int = 0) -> dict:
        with self._lock:
            return {
                "id": self.id, "kind": self.kind, "slug": self.slug, "title": self.title,
                "status": self.status, "progress": self.progress, "error": self.error,
                "log": self.log[log_since:], "log_len": len(self.log),
                "created_at": self.created_at, "started_at": self.started_at,
                "finished_at": self.finished_at, "usage": self.ctx.as_dict(),
                "result": self.result if self.status == "done" else None,
            }


class JobManager:
    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def start(self, kind: str, slug: str | None, title: str, fn, exclusive: bool = True) -> Job:
        """fn(job) -> result (JSON-serialisable). With exclusive=True, only one
        job of this kind may run per project at a time."""
        with self._lock:
            if exclusive and slug:
                for j in self._jobs.values():
                    if j.slug == slug and j.kind == kind and j.status in ("queued", "running"):
                        raise RuntimeError(f"A '{kind}' job is already running for this project ({j.id}).")
            job = Job(kind, slug, title)
            self._jobs[job.id] = job
            self._prune()

        def runner():
            use_context(job.ctx)
            job.status = "running"
            job.started_at = time.time()
            job.say(f"Started: {title}", 0.0)
            try:
                job.result = fn(job)
                job.status = "done"
                job.say("Done.", 1.0)
            except LLMCancelled:
                job.status = "cancelled"
                job.say("Cancelled.")
            except Exception as e:  # report every failure to the UI
                traceback.print_exc()
                job.status = "error"
                job.error = f"{type(e).__name__}: {e}" if not isinstance(e, RuntimeError) else str(e)
                job.say(f"Error: {job.error}")
            finally:
                job.finished_at = time.time()
                use_context(None)

        threading.Thread(target=runner, name=f"job-{kind}-{job.id}", daemon=True).start()
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def for_project(self, slug: str) -> list[Job]:
        return sorted((j for j in self._jobs.values() if j.slug == slug), key=lambda j: j.created_at, reverse=True)

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job or job.status not in ("queued", "running"):
            return False
        job.cancel_event.set()
        job.say("Cancellation requested; stopping after the current model call...")
        return True

    def _prune(self) -> None:
        finished = sorted((j for j in self._jobs.values() if j.status in ("done", "error", "cancelled")),
                          key=lambda j: j.created_at)
        for j in finished[:-50]:
            self._jobs.pop(j.id, None)
