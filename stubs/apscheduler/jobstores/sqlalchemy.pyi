from typing import Any

from apscheduler.jobstores.base import BaseJobStore

class SQLAlchemyJobStore(BaseJobStore):
    def __init__(
        self,
        url: str | None = ...,
        engine: Any = ...,
        tablename: str = ...,
        metadata: Any = ...,
        pickle_protocol: int = ...,
        tableschema: str | None = ...,
        engine_options: dict[str, Any] | None = ...,
    ) -> None: ...
