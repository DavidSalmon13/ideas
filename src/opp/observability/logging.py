"""structlog setup. Logs go to stdout; run/stage/agent context is bound per task."""

import logging

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

_SECRET_MARKERS = ("key", "secret", "token", "password")


def configure_logging(level: str = "INFO", json: bool = True) -> None:
    renderer: Processor = (
        structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_secrets,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )


def bind_context(
    *, run_id: str | None = None, stage: str | None = None, agent: str | None = None
) -> None:
    values = {"run_id": run_id, "stage": stage, "agent": agent}
    structlog.contextvars.bind_contextvars(**{k: v for k, v in values.items() if v is not None})


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()


def redact_secrets(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    for name in event_dict:
        if any(marker in name.lower() for marker in _SECRET_MARKERS):
            event_dict[name] = "***"
    return event_dict
