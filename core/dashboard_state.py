"""Process-wide runtime state shared between the bot and the diagnostic cogs.

Everything in this module is:

*   **thread-safe** — ``logging`` can fire from any thread, so the log buffer
    takes a ``threading.Lock`` even though its readers are coroutines.
*   **lock-free on the hot path** — mutations are plain attribute assignments
    on a single object; there is no contention to speak of and no awaits, so
    the gateway task is never blocked by a reader.
*   **snapshot-oriented** — readers never touch internal collections directly,
    they always read an immutable copy via :meth:`RuntimeState.snapshot`.

If this bot is ever scaled to multiple processes, replace :class:`RuntimeState`
with a Redis-backed implementation of the same interface; nothing else changes.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

__all__ = [
    "AUTOMOD_ALERT_HISTORY_SIZE",
    "LATENCY_HISTORY_SIZE",
    "LATENCY_SAMPLE_SECONDS",
    "BotStatus",
    "HealthProbe",
    "LogBuffer",
    "LogRecordView",
    "RuntimeState",
    "runtime_state",
]

#: How often ``main.py`` samples gateway latency into the history ring.
LATENCY_SAMPLE_SECONDS = 5.0
#: Number of samples retained for the latency sparkline (~10 minutes).
LATENCY_HISTORY_SIZE = 120
#: AutoMod actions kept in memory for ``/status`` and the log channel.
AUTOMOD_ALERT_HISTORY_SIZE = 50


def _utc_now() -> datetime:
    return datetime.now(UTC)


class BotStatus(StrEnum):
    """Lifecycle phases surfaced by ``/status`` and consumed by ``/test``."""

    OFFLINE = "offline"
    STARTING = "starting"
    CONNECTING = "connecting"
    ONLINE = "online"
    DEGRADED = "degraded"
    SHUTTING_DOWN = "shutting_down"

    @property
    def is_live(self) -> bool:
        """True when the gateway session is usable."""
        return self in {BotStatus.ONLINE, BotStatus.DEGRADED}

    @property
    def label(self) -> str:
        return self.value.replace("_", " ").title()


@dataclass(frozen=True, slots=True)
class LogRecordView:
    """Immutable projection of a ``logging.LogRecord`` for the ``/status`` tail."""

    timestamp: str
    level: str
    levelno: int
    logger: str
    message: str
    source: str
    exc_text: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "timestamp": self.timestamp,
            "level": self.level,
            "levelno": self.levelno,
            "logger": self.logger,
            "message": self.message,
            "source": self.source,
        }
        if self.exc_text:
            payload["exc_text"] = self.exc_text
        return payload


def _coerce_text(value: Any) -> str:
    """Render non-string log args without ever raising inside a log handler."""
    try:
        if isinstance(value, str):
            return value
        return str(value)
    except Exception:  # noqa: BLE001 - a __str__ that explodes must not kill logging
        return "<unprintable log value>"


class LogBuffer(logging.Handler):
    """A bounded, thread-safe ring buffer wired into the logging tree.

    Used as a ``logging.Handler``, so every record emitted anywhere in the
    process (including from inside a library) lands here automatically — no
    call sites to remember, and no risk of the feed silently going stale.
    """

    def __init__(self, maxlen: int = 400) -> None:
        super().__init__(level=logging.INFO)
        self._records: deque[LogRecordView] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._dropped = 0
        self.setFormatter(logging.Formatter("%(message)s"))

    # -- logging.Handler contract -----------------------------------------
    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            view = LogRecordView(
                timestamp=datetime.fromtimestamp(
                    record.created, tz=UTC
                ).isoformat(timespec="seconds"),
                level=record.levelname,
                levelno=record.levelno,
                logger=record.name,
                message=message,
                source=f"{record.module}:{record.lineno}",
                exc_text=_coerce_text(record.exc_info) if record.exc_info else None,
            )
        except Exception:  # noqa: BLE001 - logging must never raise
            self.handleError(record)
            return

        with self._lock:
            if len(self._records) == self._records.maxlen:
                self._dropped += 1
            self._records.append(view)

    # -- read API ---------------------------------------------------------
    def snapshot(self, limit: int | None = None) -> list[LogRecordView]:
        """Return up to ``limit`` of the newest records, oldest first."""
        with self._lock:
            items = list(self._records)
        if limit is not None and limit >= 0:
            items = items[-limit:]
        return items

    def clear(self) -> None:
        with self._lock:
            self._records.clear()
            self._dropped = 0

    @property
    def dropped(self) -> int:
        """Records evicted by the ring bound since the last :meth:`clear`."""
        with self._lock:
            return self._dropped

    @property
    def maxlen(self) -> int:
        """Ring capacity — how many records ``/status`` can hold."""
        return self._records.maxlen or 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)


@dataclass(slots=True)
class HealthProbe:
    """Self-report for one subsystem, written by the ``/test`` diagnostic.

    ``/test`` performs a real round trip against the subsystem (a ``SELECT 1``
    for the database, a gateway copy for the cache), then records the result
    here so ``/status`` can prove the checks and the panel read the same state.
    """

    url: str = ""
    checked_at: float | None = None
    ok: bool | None = None
    latency_ms: float | None = None
    status_code: int | None = None
    error: str | None = None

    def record(
        self,
        *,
        ok: bool,
        latency_ms: float,
        status_code: int | None = None,
        error: str | None = None,
        url: str | None = None,
    ) -> None:
        self.checked_at = time.time()
        self.ok = ok
        self.latency_ms = latency_ms
        self.status_code = status_code
        self.error = error
        if url:
            self.url = url

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "checked_at": self.checked_at,
            "ok": self.ok,
            "latency_ms": self.latency_ms,
            "status_code": self.status_code,
            "error": self.error,
            "checked_at_iso": (
                datetime.fromtimestamp(self.checked_at, tz=UTC).isoformat(
                    timespec="seconds"
                )
                if self.checked_at
                else None
            ),
        }


class RuntimeState:
    """Mutable singleton holding everything ``/status`` and ``/test`` render."""

    __slots__ = (
        "_automod_alerts",
        "_lock",
        "_log_buffer",
        "channel_count",
        "cog_errors",
        "cogs_failed",
        "cogs_loaded",
        "command_count",
        "connected_at",
        "database",
        "error_count",
        "guild_count",
        "last_error",
        "last_error_at",
        "latency_history",
        "latency_ms",
        "moderation_action_count",
        "rest_latency_ms",
        "started_at",
        "status",
        "user_count",
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._log_buffer = LogBuffer()
        self._automod_alerts: deque[dict[str, Any]] = deque(
            maxlen=AUTOMOD_ALERT_HISTORY_SIZE
        )

        self.status: BotStatus = BotStatus.OFFLINE
        self.started_at: float = time.time()
        self.connected_at: float | None = None

        # Metrics
        self.latency_ms: float | None = None
        self.rest_latency_ms: float | None = None
        self.latency_history: deque[float] = deque(maxlen=LATENCY_HISTORY_SIZE)

        # Discord cache totals
        self.guild_count: int = 0
        self.user_count: int = 0
        self.channel_count: int = 0
        self.command_count: int = 0

        #: Ledger rows written this session (bans, kicks, mutes, warnings...).
        self.moderation_action_count: int = 0

        # Extension bookkeeping
        self.cogs_loaded: list[str] = []
        self.cogs_failed: list[str] = []
        self.cog_errors: dict[str, str] = {}

        # Error telemetry
        self.last_error: str | None = None
        self.last_error_at: float | None = None
        self.error_count: int = 0

        # Subsystem health
        self.database: HealthProbe = HealthProbe()

    # -- logs -------------------------------------------------------------
    @property
    def logs(self) -> LogBuffer:
        return self._log_buffer

    def install_log_capture(
        self, maxlen: int = 400, level: int = logging.INFO
    ) -> None:
        """Attach the ring buffer to the root logger exactly once."""
        if self._log_buffer in logging.getLogger().handlers:
            return
        self._log_buffer.setLevel(level)
        logging.getLogger().addHandler(self._log_buffer)

    # -- lifecycle --------------------------------------------------------
    def set_status(self, status: BotStatus) -> None:
        with self._lock:
            self.status = status
            if status is BotStatus.ONLINE and self.connected_at is None:
                self.connected_at = time.time()

    def set_latency(self, gateway_ms: float, rest_ms: float | None = None) -> None:
        with self._lock:
            self.latency_ms = gateway_ms
            self.latency_history.append(gateway_ms)
            if rest_ms is not None:
                self.rest_latency_ms = rest_ms

    def set_cache_totals(
        self, guilds: int, users: int, channels: int
    ) -> None:
        with self._lock:
            self.guild_count = guilds
            self.user_count = users
            self.channel_count = channels

    def set_command_count(self, count: int) -> None:
        with self._lock:
            self.command_count = count

    def record_moderation_action(self) -> None:
        """Count one persisted moderation case for the session metrics."""
        with self._lock:
            self.moderation_action_count += 1

    def record_automod(
        self,
        *,
        guild_id: int,
        user_id: int,
        rule_id: int | None,
        keyword: str,
        action: str,
        channel_id: int | None = None,
    ) -> None:
        """Remember one native AutoMod action for ``/status``."""
        entry: dict[str, Any] = {
            "at": _utc_now().isoformat(timespec="seconds"),
            "guild_id": guild_id,
            "user_id": user_id,
            "rule_id": rule_id,
            "keyword": keyword,
            "action": action,
            "channel_id": channel_id,
        }
        with self._lock:
            self._automod_alerts.append(entry)

    @property
    def automod_alerts(self) -> list[dict[str, Any]]:
        """Newest-first copy of the recent AutoMod actions."""
        with self._lock:
            return list(reversed(self._automod_alerts))

    def set_cog_inventory(
        self, loaded: Iterable[str], failed: dict[str, str]
    ) -> None:
        with self._lock:
            self.cogs_loaded = sorted(loaded)
            self.cogs_failed = sorted(failed)
            self.cog_errors = dict(failed)

    def record_error(self, context: str, exc: BaseException | str) -> None:
        """Store the most recent failure for display on ``/status``."""
        with self._lock:
            self.error_count += 1
            self.last_error = f"{context}: {exc}"
            self.last_error_at = time.time()

    # -- projections ------------------------------------------------------
    @property
    def uptime_seconds(self) -> float:
        anchor = self.connected_at or self.started_at
        return max(0.0, time.time() - anchor)

    def snapshot(self) -> dict[str, Any]:
        """Immutable, JSON-ready projection consumed by ``/status`` and ``/test``."""
        with self._lock:
            payload: dict[str, Any] = {
                "status": self.status.value,
                "status_label": self.status.label,
                "online": self.status.is_live,
                "uptime_seconds": round(self.uptime_seconds, 1),
                "started_at": self.started_at,
                "connected_at": self.connected_at,
                "latency_ms": (
                    round(self.latency_ms, 1) if self.latency_ms is not None else None
                ),
                "rest_latency_ms": (
                    round(self.rest_latency_ms, 1)
                    if self.rest_latency_ms is not None
                    else None
                ),
                "latency_history": [round(v, 1) for v in self.latency_history],
                "guild_count": self.guild_count,
                "user_count": self.user_count,
                "channel_count": self.channel_count,
                "command_count": self.command_count,
                "moderation_action_count": self.moderation_action_count,
                "cogs_loaded": list(self.cogs_loaded),
                "cogs_failed": list(self.cogs_failed),
                "cog_errors": dict(self.cog_errors),
                "error_count": self.error_count,
                "last_error": self.last_error,
                "last_error_at": self.last_error_at,
                "database": self.database.to_dict(),
                "automod_alerts": list(reversed(self._automod_alerts)),
                "log_buffer_size": len(self._log_buffer),
                "logs_dropped": self._log_buffer.dropped,
            }
        payload["uptime_human"] = humanize_duration(payload["uptime_seconds"])
        return payload


def humanize_duration(seconds: float) -> str:
    """Render a duration as ``3d 04h 17m 09s``."""
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if days or hours:
        parts.append(f"{hours:02d}h")
    if days or hours or minutes:
        parts.append(f"{minutes:02d}m")
    parts.append(f"{secs:02d}s")
    return " ".join(parts)


#: The single instance shared by the bot, the cogs and the diagnostic commands.
runtime_state = RuntimeState()
