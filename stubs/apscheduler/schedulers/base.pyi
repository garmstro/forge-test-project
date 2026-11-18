from collections.abc import Callable
from datetime import datetime, tzinfo
from typing import Any

from apscheduler.job import Job
from apscheduler.jobstores.base import BaseJobStore
from apscheduler.triggers.base import BaseTrigger

STATE_STOPPED: int
STATE_RUNNING: int
STATE_PAUSED: int

class BaseScheduler:
    state: int
    timezone: tzinfo
    def __init__(
        self,
        gconfig: dict[str, Any] = ...,
        *,
        jobstores: dict[str, BaseJobStore] | None = ...,
        timezone: tzinfo | str | None = ...,
        job_defaults: dict[str, Any] | None = ...,
        **options: Any,
    ) -> None: ...
    @property
    def running(self) -> bool: ...
    def start(self, paused: bool = ...) -> None: ...
    def shutdown(self, wait: bool = ...) -> None: ...
    def pause(self) -> None: ...
    def resume(self) -> None: ...
    def add_jobstore(
        self, jobstore: BaseJobStore | str, alias: str = ..., **jobstore_opts: Any
    ) -> None: ...
    def add_job(
        self,
        func: Callable[..., Any] | str,
        trigger: BaseTrigger | str | None = ...,
        args: list[Any] | tuple[Any, ...] | None = ...,
        kwargs: dict[str, Any] | None = ...,
        id: str | None = ...,
        name: str | None = ...,
        misfire_grace_time: int | None = ...,
        coalesce: bool = ...,
        max_instances: int = ...,
        next_run_time: datetime | None = ...,
        jobstore: str = ...,
        executor: str = ...,
        replace_existing: bool = ...,
        **trigger_args: Any,
    ) -> Job: ...
    def get_job(self, job_id: str, jobstore: str | None = ...) -> Job | None: ...
    def get_jobs(
        self, jobstore: str | None = ..., pending: bool | None = ...
    ) -> list[Job]: ...
    def remove_job(self, job_id: str, jobstore: str | None = ...) -> None: ...
