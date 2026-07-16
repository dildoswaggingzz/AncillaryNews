"""
Shared structured (JSON) logging setup for every service (Phase 6 production
readiness).

Each of the four `services/*/main.py` entrypoints calls `configure_logging()`
once, at import time, in place of the plain-text `logging.basicConfig(...)`
each used to call individually. One log record in -> one JSON line out on
stdout, so log aggregation (Loki/CloudWatch/whatever a given deployment
uses) never has to parse free-text messages.

Deliberately dependency-light: stdlib `logging` plus one custom
`logging.Formatter` subclass. No third-party structured-logging framework --
the whole ask fits in ~40 lines and pulling in `structlog` or similar for
this would be scope creep.
"""

import json
import logging
import sys
from datetime import UTC, datetime

# Every attribute a plain `logging.LogRecord` carries by default. Anything
# else present on a record (i.e. passed via `logger.info(..., extra={...})`)
# is "extra" structured data the caller wants surfaced in the JSON line.
_RESERVED_RECORD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys())


class JSONFormatter(logging.Formatter):
    """Formats a `LogRecord` as one JSON line.

    Always includes `timestamp`, `level`, `logger`, `message`; includes
    `exception` when the record carries exception info (e.g. `logger.exception`
    / `exc_info=True`); passes through any `extra={...}` fields the caller
    supplied, verbatim.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key not in _RESERVED_RECORD_ATTRS and key not in payload:
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        # `default=str` covers anything an `extra={...}` field passes that
        # isn't natively JSON-serializable (datetimes, etc.) rather than
        # raising and losing the log line entirely.
        return json.dumps(payload, default=str)


# `services/api/main.py` runs under uvicorn, which -- unlike every other
# service's plain `python services/x/main.py` entrypoint -- installs its own
# handlers directly on these three loggers before the app module (and this
# function) ever gets imported, so simply configuring the root logger isn't
# enough to make uvicorn's own request/access logs come out as JSON too.
_UVICORN_LOGGER_NAMES = ("uvicorn", "uvicorn.error", "uvicorn.access")


def configure_logging(level: int = logging.INFO) -> None:
    """
    Replaces the root logger's handlers with a single stdout stream handler
    emitting `JSONFormatter` lines, at `level`. Idempotent -- safe to call
    more than once (e.g. from tests) without accumulating duplicate handlers.

    Also strips uvicorn's own handlers off its three loggers and lets them
    propagate to root instead, so `services/api/main.py`'s access/error logs
    are JSON lines too, not uvicorn's own default text format.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    for name in _UVICORN_LOGGER_NAMES:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
