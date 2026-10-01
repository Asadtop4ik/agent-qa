"""Bounded, lazily started in-memory background job runner."""

from __future__ import annotations

from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
import logging
import math
import threading
import time
from typing import Any, Callable

from agent_qa.errors import ApiError

LOGGER = logging.getLogger(__name__)
JOB_TYPES = ("sleep", "orders_summary", "stock_report", "fail")
JOB_STATUSES = (
    "queued",
    "running",
    "cancelling",
    "succeeded",
    "failed",
    "cancelled",
)
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
_MAX_JOB_ID = (1 << 63) - 1


def _validation_error(fields: list[tuple[str, str]]) -> ApiError:
    return ApiError(
        400,
        "validation_error",
        "Request validation failed",
        [{"field": field, "message": message} for field, message in sorted(fields)],
    )


def validate_job(job_type: Any, params: Any = None) -> tuple[str, dict[str, Any]]:
    """Validate a type-specific parameter object, filling documented defaults."""
    errors: list[tuple[str, str]] = []
    if not isinstance(job_type, str) or job_type not in JOB_TYPES:
        raise _validation_error([("type", "Must be a supported job type")])
    if params is None:
        params = {}
    if not isinstance(params, dict) or len(params) > 8:
        raise _validation_error([("params", "Must be an object with at most 8 fields")])
    allowed = {
        "sleep": {"duration_ms"},
        "orders_summary": set(),
        "stock_report": {"threshold"},
        "fail": {"message"},
    }[job_type]
    for key in params:
        if not isinstance(key, str) or key not in allowed:
            errors.append((f"params.{str(key)[:128]}", "Unknown parameter"))
    normalized: dict[str, Any] = {}
    if job_type == "sleep":
        value = params.get("duration_ms", 100)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= 5000
        ):
            errors.append(("params.duration_ms", "Must be an integer from 0 to 5000"))
        else:
            normalized["duration_ms"] = value
    elif job_type == "stock_report":
        value = params.get("threshold", 5)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= 1_000_000
        ):
            errors.append(("params.threshold", "Must be an integer from 0 to 1000000"))
        else:
            normalized["threshold"] = value
    elif job_type == "fail":
        value = params.get("message", "boom")
        if not isinstance(value, str) or not 1 <= len(value) <= 100:
            errors.append(("params.message", "Must contain 1 to 100 characters"))
        else:
            normalized["message"] = value
    if errors:
        raise _validation_error(errors)
    return job_type, normalized


Handler = Callable[[dict[str, Any], Callable[[int], None], threading.Event], Any]


def make_builtin_handlers(orders: Any, products: Any) -> dict[str, Handler]:
    """Create handlers backed by the supplied thread-safe stores."""

    def sleep_job(
        params: dict[str, Any],
        progress: Callable[[int], None],
        cancel: threading.Event,
    ) -> dict[str, int] | None:
        duration = params["duration_ms"]
        completed = 0
        while completed < duration:
            if cancel.wait(min(25, duration - completed) / 1000):
                return None
            completed += min(25, duration - completed)
            progress(int(completed * 100 / duration))
        if duration == 0:
            progress(100)
        return {"slept_ms": completed}

    def summary_job(
        _params: dict[str, Any],
        _progress: Callable[[int], None],
        _cancel: threading.Event,
    ) -> dict[str, Any]:
        return orders.summary_snapshot()

    def stock_job(
        params: dict[str, Any],
        _progress: Callable[[int], None],
        _cancel: threading.Event,
    ) -> dict[str, Any]:
        return {
            "threshold": params["threshold"],
            "low_stock": products.low_stock_snapshot(params["threshold"]),
        }

    def fail_job(
        params: dict[str, Any],
        _progress: Callable[[int], None],
        _cancel: threading.Event,
    ) -> None:
        raise _RequestedFailure(params["message"])

    return {
        "sleep": sleep_job,
        "orders_summary": summary_job,
        "stock_report": stock_job,
        "fail": fail_job,
    }


class _RequestedFailure(Exception):
    pass


class JobRunner:
    """Threaded job queue with lazy daemon workers and bounded terminal history."""

    def __init__(
        self,
        handlers: dict[str, Handler],
        workers: int = 2,
        queue_limit: int = 20,
        retention: int = 100,
        clock: Callable[[], Any] = time.time,
    ) -> None:
        for name, value, minimum, maximum in (
            ("workers", workers, 1, 3),
            ("queue_limit", queue_limit, 1, 1000),
            ("retention", retention, 1, 1000),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not minimum <= value <= maximum
            ):
                raise ValueError(f"{name} must be between {minimum} and {maximum}")
        if not isinstance(handlers, dict) or any(
            key not in JOB_TYPES or not callable(handler)
            for key, handler in handlers.items()
        ):
            raise ValueError("handlers must map supported job types to callables")
        self._handlers = dict(handlers)
        self._workers_count = workers
        self._queue_limit = queue_limit
        self._retention = retention
        self._clock = clock
        self._condition = threading.Condition(threading.RLock())
        self._jobs: dict[int, dict[str, Any]] = {}
        self._cancel_events: dict[int, threading.Event] = {}
        self._queue: deque[int] = deque()
        self._terminal_order: deque[int] = deque()
        self._threads: list[threading.Thread] = []
        self._next_id = 1
        self._started = False
        self._stopping = False

    def _timestamp(self) -> str:
        value = self._clock()
        if isinstance(value, datetime):
            moment = value
        else:
            moment = datetime.fromtimestamp(float(value), timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _start_locked(self) -> None:
        if self._started:
            return
        self._started = True
        self._stopping = False
        self._threads = [
            threading.Thread(
                target=self._worker,
                name=f"agent-qa-job-{index + 1}",
                daemon=True,
            )
            for index in range(self._workers_count)
        ]
        for thread in self._threads:
            thread.start()

    def start(self) -> None:
        with self._condition:
            self._start_locked()
            self._condition.notify_all()

    def submit(self, job_type: Any, params: Any = None) -> dict[str, Any]:
        name, normalized = validate_job(job_type, params)
        with self._condition:
            if self._stopping:
                raise ApiError(503, "queue_full", "Job queue is unavailable")
            queued = sum(job["status"] == "queued" for job in self._jobs.values())
            if queued >= self._queue_limit:
                raise ApiError(503, "queue_full", "Job queue is full")
            if self._next_id > _MAX_JOB_ID:
                raise ApiError(503, "job_id_exhausted", "Job ID space is exhausted")
            self._start_locked()
            job_id = self._next_id
            self._next_id += 1
            job = {
                "id": job_id,
                "type": name,
                "params": normalized,
                "status": "queued",
                "progress": 0,
                "result": None,
                "error": None,
                "cancel_requested": False,
                "created_at": self._timestamp(),
                "started_at": None,
                "finished_at": None,
            }
            self._jobs[job_id] = job
            self._cancel_events[job_id] = threading.Event()
            self._queue.append(job_id)
            self._condition.notify_all()
            return self._copy(job)

    @staticmethod
    def _copy(job: dict[str, Any]) -> dict[str, Any]:
        return {
            **job,
            "params": deepcopy(job["params"]),
            "result": deepcopy(job["result"]),
            "error": deepcopy(job["error"]),
        }

    def list_jobs(
        self,
        status: str | None = None,
        type: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        if status is not None and status not in JOB_STATUSES:
            raise _validation_error([("status", "Must be a supported job status")])
        if type is not None and type not in JOB_TYPES:
            raise _validation_error([("type", "Must be a supported job type")])
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise _validation_error([("limit", "Must be an integer from 1 to 100")])
        if (
            isinstance(offset, bool)
            or not isinstance(offset, int)
            or not 0 <= offset <= (1 << 63) - 1
        ):
            raise _validation_error([("offset", "Must be a non-negative integer")])
        with self._condition:
            matched = [
                job
                for job in sorted(self._jobs.values(), key=lambda item: item["id"])
                if (status is None or job["status"] == status)
                and (type is None or job["type"] == type)
            ]
            return {
                "items": [self._copy(job) for job in matched[offset : offset + limit]],
                "total": len(matched),
                "limit": limit,
                "offset": offset,
            }

    def get(self, job_id: Any, wait_ms: int = 0) -> dict[str, Any] | None:
        if isinstance(job_id, bool) or not isinstance(job_id, int) or job_id <= 0:
            return None
        if (
            isinstance(wait_ms, bool)
            or not isinstance(wait_ms, int)
            or not 0 <= wait_ms <= 5000
        ):
            raise _validation_error([("wait_ms", "Must be an integer from 0 to 5000")])
        deadline = time.monotonic() + wait_ms / 1000
        with self._condition:
            while True:
                job = self._jobs.get(job_id)
                if job is None:
                    return None
                if job["status"] in TERMINAL_STATUSES or wait_ms == 0:
                    return self._copy(job)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._copy(job)
                self._condition.wait(remaining)

    def cancel(self, job_id: Any) -> dict[str, Any] | None:
        if isinstance(job_id, bool) or not isinstance(job_id, int) or job_id <= 0:
            return None
        with self._condition:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job["status"] in TERMINAL_STATUSES:
                raise ApiError(409, "job_not_cancellable", "Job is already terminal")
            if job["status"] == "queued":
                job["status"] = "cancelled"
                job["cancel_requested"] = True
                job["finished_at"] = self._timestamp()
                self._queue = deque(value for value in self._queue if value != job_id)
                self._terminal_order.append(job_id)
                self._prune_locked()
            elif job["status"] == "running":
                job["status"] = "cancelling"
                job["cancel_requested"] = True
                self._cancel_events[job_id].set()
            self._condition.notify_all()
            return self._copy(job)

    def status_counts(self) -> dict[str, int]:
        with self._condition:
            return {
                status: sum(job["status"] == status for job in self._jobs.values())
                for status in JOB_STATUSES
            }

    def _prune_locked(self) -> None:
        terminal_count = sum(
            job["status"] in TERMINAL_STATUSES for job in self._jobs.values()
        )
        while terminal_count > self._retention and self._terminal_order:
            job_id = self._terminal_order.popleft()
            job = self._jobs.get(job_id)
            if job is not None and job["status"] in TERMINAL_STATUSES:
                del self._jobs[job_id]
                self._cancel_events.pop(job_id, None)
                terminal_count -= 1

    def _worker(self) -> None:
        while True:
            with self._condition:
                while not self._queue and not self._stopping:
                    self._condition.wait()
                if self._stopping:
                    self._threads.remove(threading.current_thread())
                    if not self._threads:
                        self._started = False
                        self._stopping = False
                    self._condition.notify_all()
                    return
                job_id = self._queue.popleft()
                job = self._jobs.get(job_id)
                if job is None or job["status"] != "queued":
                    continue
                job["status"] = "running"
                job["started_at"] = self._timestamp()
                cancel_event = self._cancel_events[job_id]
                self._condition.notify_all()

            def update_progress(value: int, current_id: int = job_id) -> None:
                if isinstance(value, bool) or not isinstance(value, int):
                    return
                with self._condition:
                    current = self._jobs.get(current_id)
                    if current is not None and current["status"] == "running":
                        current["progress"] = max(0, min(100, value))
                        self._condition.notify_all()

            try:
                handler = self._handlers[job["type"]]
                result = handler(dict(job["params"]), update_progress, cancel_event)
            except _RequestedFailure as error:
                with self._condition:
                    current = self._jobs.get(job_id)
                    if current is not None:
                        if cancel_event.is_set() or current["status"] == "cancelling":
                            current.update(
                                status="cancelled",
                                error=None,
                                result=None,
                            )
                        else:
                            current.update(
                                status="failed",
                                error={"code": "job_failed", "message": str(error)},
                                result=None,
                            )
                        current["finished_at"] = self._timestamp()
                        self._terminal_order.append(job_id)
                        self._prune_locked()
                    self._condition.notify_all()
            except Exception:
                LOGGER.exception("Background job handler crashed (job_id=%s)", job_id)
                with self._condition:
                    current = self._jobs.get(job_id)
                    if current is not None:
                        if cancel_event.is_set() or current["status"] == "cancelling":
                            current.update(
                                status="cancelled",
                                error=None,
                                result=None,
                            )
                        else:
                            current.update(
                                status="failed",
                                error={
                                    "code": "job_crashed",
                                    "message": "Job crashed",
                                },
                                result=None,
                            )
                        current["finished_at"] = self._timestamp()
                        self._terminal_order.append(job_id)
                        self._prune_locked()
                    self._condition.notify_all()
            else:
                with self._condition:
                    current = self._jobs.get(job_id)
                    if current is not None:
                        if cancel_event.is_set() or current["status"] == "cancelling":
                            current.update(
                                status="cancelled",
                                result=None,
                                error=None,
                            )
                        else:
                            current.update(
                                status="succeeded",
                                progress=100,
                                result=result,
                                error=None,
                            )
                        current["finished_at"] = self._timestamp()
                        self._terminal_order.append(job_id)
                        self._prune_locked()
                    self._condition.notify_all()

    def stop(self, timeout: float | None = None) -> None:
        if timeout is not None:
            invalid = (
                isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))
                or timeout < 0
                or timeout > 86400
            )
            if isinstance(timeout, float) and not math.isfinite(timeout):
                invalid = True
            if invalid:
                raise ValueError("timeout must be from 0 to 86400 seconds or None")
        with self._condition:
            if not self._started:
                return
            self._stopping = True
            for job_id, job in self._jobs.items():
                if job["status"] == "queued":
                    job.update(
                        status="cancelled",
                        cancel_requested=True,
                        finished_at=self._timestamp(),
                    )
                    self._cancel_events[job_id].set()
                    self._terminal_order.append(job_id)
                elif job["status"] in {"running", "cancelling"}:
                    job["status"] = "cancelling"
                    job["cancel_requested"] = True
                    self._cancel_events[job_id].set()
            self._queue.clear()
            threads = list(self._threads)
            self._condition.notify_all()
        deadline = None if timeout is None else time.monotonic() + timeout
        for thread in threads:
            remaining = (
                None if deadline is None else max(0.0, deadline - time.monotonic())
            )
            thread.join(remaining)
        with self._condition:
            if not any(thread.is_alive() for thread in self._threads):
                self._threads = []
                self._started = False
                self._stopping = False
            self._prune_locked()
            self._condition.notify_all()
