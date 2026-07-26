"""Minimal JSON logging for application-owned loggers."""

import json
import logging
from datetime import UTC, datetime
from typing import Final

from core_console.request_context import get_request_id

SERVICE_NAME: Final = "core-console-backend"
_APPLICATION_LOGGER_NAME = "core_console"
_STRUCTURED_HANDLER_MARKER = "_core_console_structured_handler"
_STRUCTURED_FIELDS = frozenset({"event", "request_id"})
_STANDARD_FIELDS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
    }
)


class JsonFormatter(logging.Formatter):
    """Render application log records as one safe JSON object per line."""

    def __init__(self, environment: str) -> None:
        super().__init__()
        self.environment = environment

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        request_id = get_request_id()
        record_request_id = record.__dict__.get("request_id")
        if request_id is None and isinstance(record_request_id, str):
            request_id = record_request_id

        fallback: dict[str, object] = {
            "timestamp": timestamp,
            "level": "ERROR",
            "event": "logging.serialization_failed",
            "service": SERVICE_NAME,
            "environment": self.environment,
        }
        if request_id is not None:
            fallback["request_id"] = request_id

        try:
            event = record.__dict__.get("event")
            payload: dict[str, object] = {
                "timestamp": timestamp,
                "level": record.levelname,
                "event": event if event is not None else record.getMessage(),
                "service": SERVICE_NAME,
                "environment": self.environment,
            }
            if request_id is not None:
                payload["request_id"] = request_id

            for name, value in record.__dict__.items():
                if (
                    name not in _STANDARD_FIELDS
                    and name not in _STRUCTURED_FIELDS
                    and name not in payload
                ):
                    payload[name] = value

            if record.exc_info is not None:
                payload["exception"] = self.formatException(record.exc_info)

            return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        except Exception:
            return json.dumps(fallback, separators=(",", ":"))


def configure_logging(environment: str) -> None:
    """Configure application-owned loggers without changing third-party loggers."""

    application_logger = logging.getLogger(_APPLICATION_LOGGER_NAME)
    application_logger.setLevel(logging.INFO)
    application_logger.propagate = False

    for handler in application_logger.handlers:
        if getattr(handler, _STRUCTURED_HANDLER_MARKER, False):
            handler.setFormatter(JsonFormatter(environment))
            return

    handler = logging.StreamHandler()
    handler.setLevel(logging.INFO)
    handler.setFormatter(JsonFormatter(environment))
    setattr(handler, _STRUCTURED_HANDLER_MARKER, True)
    application_logger.addHandler(handler)
