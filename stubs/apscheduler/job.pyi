from datetime import datetime

from apscheduler.triggers.base import BaseTrigger

class Job:
    id: str
    name: str
    func_ref: str
    trigger: BaseTrigger
    next_run_time: datetime | None
    coalesce: bool
    max_instances: int
    misfire_grace_time: int | None
