"""Single-flight background jobs with a step log the page polls."""

from __future__ import annotations

import itertools
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Callable


class Busy(RuntimeError):
    pass


@dataclass
class Job:
    id: int
    kind: str
    status: str = 'running'  # running | succeeded | failed
    log: list[tuple[float, str]] = field(default_factory=list)
    result: str = ''
    started: float = field(default_factory=time.time)

    def say(self, msg: str) -> None:
        self.log.append((time.time(), msg))

    def to_dict(self) -> dict:
        return {'id': self.id, 'kind': self.kind, 'status': self.status, 'result': self.result,
                'started': self.started,
                'log': [{'t': t, 'msg': m} for t, m in self.log]}


class JobRunner:
    """At most one router-changing job at a time; the last few are kept for the page."""

    def __init__(self, keep: int = 20, audit_log: Callable[[Job], None] | None = None):
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._jobs: dict[int, Job] = {}
        self._running: Job | None = None
        self._keep = keep
        self._audit_log = audit_log

    def start(self, kind: str, fn: Callable[[Job], str], wait: bool = False) -> Job:
        with self._lock:
            if self._running is not None:
                raise Busy(f'{self._running.kind} job #{self._running.id} is still running')
            job = Job(next(self._ids), kind)
            self._running = job
            self._jobs[job.id] = job
            for old in sorted(self._jobs)[:-self._keep]:
                del self._jobs[old]
        t = threading.Thread(target=self._run, args=(job, fn), daemon=True)
        t.start()
        if wait:
            t.join()
        return job

    def _run(self, job: Job, fn: Callable[[Job], str]) -> None:
        try:
            job.result = fn(job) or 'done'
            job.status = 'succeeded'
        except Exception as e:  # noqa: BLE001 - every failure must reach the page
            job.result = str(e) or e.__class__.__name__
            job.say(f'FAILED: {job.result}')
            if not isinstance(e, (ValueError, RuntimeError)):
                job.say(traceback.format_exc(limit=3))
            job.status = 'failed'
        finally:
            with self._lock:
                self._running = None
            if self._audit_log:
                self._audit_log(job)

    def get(self, job_id: int) -> Job | None:
        return self._jobs.get(job_id)

    @property
    def current(self) -> Job | None:
        return self._running

    def recent(self) -> list[Job]:
        return [self._jobs[k] for k in sorted(self._jobs, reverse=True)]
