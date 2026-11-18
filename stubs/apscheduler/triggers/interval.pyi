from datetime import datetime, timedelta, tzinfo

from apscheduler.triggers.base import BaseTrigger

class IntervalTrigger(BaseTrigger):
    interval: timedelta
    def __init__(
        self,
        weeks: int = ...,
        days: int = ...,
        hours: int = ...,
        minutes: int = ...,
        seconds: int = ...,
        start_date: datetime | str | None = ...,
        end_date: datetime | str | None = ...,
        timezone: tzinfo | str | None = ...,
        jitter: int | None = ...,
    ) -> None: ...
