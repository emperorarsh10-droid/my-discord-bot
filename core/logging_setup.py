"""Logging configuration for The Zagrosian Eye.

Design goals
------------
*   **One call to configure everything.** ``configure_logging()`` builds a
    colourised console handler, two rotating file sinks (``bot.log`` for the
    full stream, ``errors.log`` for ``WARNING`` and above) and installs the
    live log ring that ``/status`` tails.
*   **Idempotent.** Calling it twice — which happens in tests and in
    ``--reload`` runs — replaces handlers instead of duplicating lines.
*   **Contextual.** A ``contextvars`` based filter stamps every record with the
    command or task that produced it, so a traceback can be traced back to the
    exact ``/ban`` that caused it.
*   **Never raises.** A logging failure must not take the process down, so every
    handler installation is individually guarded and degrades to a warning.
"""

from __future__ import annotations

import contextvars
import logging
import logging.handlers
import sys
from contextlib import suppress
from typing import Any

from config import get_settings
from core.dashboard_state import runtime_state

__all__ = [
    "command_context",
    "configure_logging",
    "get_logger",
    "log_context",
    "root_logger",
]

_LOG_CONTEXT: contextvars.ContextVar[str] = contextvars.ContextVar(
    "zagrosian_log_context", default="-"
)

# ANSI palette, dimmed for readability on both light and dark terminals.
_RESET = "\033[0m"
_DIM = "\033[2m"
_BOLD = "\033[1m"
_LEVEL_COLORS: dict[str, str] = {
    "DEBUG": "\033[36m",     # cyan
    "INFO": "\033[32m",      # green
    "WARNING": "\033[33m",   # yellow
    "ERROR": "\033[31m",     # red
    "CRITICAL": "\033[41;97m",  # white on red
}

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)-28s %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_CONSOLE_FORMAT = (
    f"{_DIM}%(asctime)s{_RESET} "
    f"%(levelname_logcolor)-17s "
    f"{_DIM}%(name)-28s%(reset_logcolor)s "
    f"%(context_logcolor)s%(message)s"
)


class _ContextFilter(logging.Filter):
    """Injects ``context`` and the colour helpers the console format expects."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.context = _LOG_CONTEXT.get()  # type: ignore[attr-defined]
        color = _LEVEL_COLORS.get(record.levelname, "")
        record.levelname_logcolor = f"{color}{record.levelname:<8}{_RESET}"
        record.reset_logcolor = _DIM
        record.context_logcolor = f"{_BOLD}{record.context}{_RESET}"
        return True


class _PlainFormatter(logging.Formatter):
    """File formatter that still carries the context tag."""

    def __init__(self) -> None:
        super().__init__(
            "%(asctime)s | %(levelname)-8s | %(name)s | [%(context)s] | %(message)s",
            datefmt=_DATE_FORMAT,
        )

    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "context"):
            record.context = "-"  # type: ignore[attr-defined]
        return super().format(record)


def configure_logging(*, force: bool = False) -> logging.Logger:
    """Install handlers on the root logger and return it.

    Args:
        force: Reconfigure even if handlers are already present.

    Returns:
        The configured root logger.
    """
    settings = get_settings()
    root = logging.getLogger()
    if root.handlers and not force:
        return root

    for handler in list(root.handlers):
        root.removeHandler(handler)
        with suppress(Exception):  # closing a handler must never raise
            handler.close()

    root.setLevel(settings.log_level)
    # Third-party libraries are chatty at DEBUG; keep our own logs authoritative.
    for noisy in ("discord.client", "discord.gateway", "asyncio"):
        logging.getLogger(noisy).setLevel(max(logging.INFO, root.level))
    # SQLAlchemy logs *every* statement at INFO. Left at INFO that is an
    # unbounded firehose (megabytes per minute on a busy bot) which drowns the
    # ``/status`` log tail and pins the CPU; it also applies console
    # backpressure that can stall the event loop. Only DEBUG gets the stream.
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if root.level <= logging.DEBUG else logging.WARNING
    )

    context_filter = _ContextFilter()
    formatter = _PlainFormatter()

    # 1. Console ---------------------------------------------------------
    console = logging.StreamHandler(stream=sys.stdout)
    console.setLevel(settings.log_level)
    console.setFormatter(logging.Formatter(_CONSOLE_FORMAT, datefmt=_DATE_FORMAT))
    console.addFilter(context_filter)
    root.addHandler(console)

    # 2. Rotating file: everything --------------------------------------
    try:
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        all_file = logging.handlers.RotatingFileHandler(
            filename=settings.log_dir / "bot.log",
            maxBytes=settings.log_max_bytes,
            backupCount=settings.log_backup_count,
            encoding="utf-8",
            delay=True,
        )
        all_file.setLevel(logging.DEBUG)
        all_file.setFormatter(formatter)
        root.addHandler(all_file)

        # 3. Rotating file: problems only --------------------------------
        error_file = logging.handlers.RotatingFileHandler(
            filename=settings.log_dir / "errors.log",
            maxBytes=settings.log_max_bytes,
            backupCount=settings.log_backup_count,
            encoding="utf-8",
            delay=True,
        )
        error_file.setLevel(logging.WARNING)
        error_file.setFormatter(formatter)
        root.addHandler(error_file)
    except OSError as exc:
        root.warning("File logging disabled, cannot open log files: %s", exc)

    # 4. Live feed for the ``/status`` log tail ----------------------------
    runtime_state.install_log_capture(
        maxlen=settings.log_buffer, level=logging.INFO
    )

    root.info(
        "Logging initialised | level=%s dir=%s file_logging=%s",
        settings.log_level,
        settings.log_dir,
        any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers),
    )
    return root


def log_context(name: str) -> _ContextManager:
    """Tag every log record emitted inside the block with ``name``."""
    return _ContextManager(name)


class _ContextManager:
    """Minimal context manager so we avoid importing ``contextlib`` eagerly."""

    __slots__ = ("_name", "_token")

    def __init__(self, name: str) -> None:
        self._name = name
        self._token: contextvars.Token[str] | None = None

    def __enter__(self) -> _ContextManager:
        self._token = _LOG_CONTEXT.set(self._name)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._token is not None:
            _LOG_CONTEXT.reset(self._token)
            self._token = None


#: Decorator form for command handlers and long-running tasks.
def command_context(name: str) -> Any:
    """Decorator wrapping a coroutine in a logging context tag."""

    def decorator(func: Any) -> Any:
        import functools
        import inspect

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                with log_context(name):
                    return await func(*args, **kwargs)

            return async_wrapper

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            with log_context(name):
                return func(*args, **kwargs)

        return sync_wrapper

    return decorator


def get_logger(name: str) -> logging.Logger:
    """Return a module logger (thin alias kept for readability at call sites)."""
    return logging.getLogger(name)


#: Convenience handle for modules that just want a logger.
root_logger = logging.getLogger("zagrosian")
