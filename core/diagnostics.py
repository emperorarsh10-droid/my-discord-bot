"""Shared diagnostic helpers for the bot's status surface.

``/status`` and ``/test`` must present the *same* picture of the world, or one
of them is lying. Everything both surfaces agree on lives here: the
configuration checklist, the probe-state vocabulary and the small formatting
helpers. Nothing in this module touches Discord or the network — it turns given
state into text, and nothing more.
"""

from __future__ import annotations

from typing import Any, Final

from config import Settings

__all__ = [
    "CHECK_FAIL",
    "CHECK_OK",
    "CHECK_WARN",
    "config_checklist",
    "probe_state",
    "sparkline",
]

#: Icons used when a boolean gate is rendered into a status line.
CHECK_OK: Final[str] = "✅"
CHECK_WARN: Final[str] = "⚠️"
CHECK_FAIL: Final[str] = "❌"

#: Unicode block ramp from lowest to highest, for the latency sparkline.
_SPARK_LEVELS: Final[str] = "▁▂▃▄▅▆▇█"


def sparkline(values: list[float], width: int = 48) -> str:
    """Render a compact unicode trend line, newest sample on the right.

    A flat series is drawn as a mid-height line rather than pretending to have
    variation; an empty series is the empty string so callers can print their
    own "collecting…" placeholder.
    """
    if not values:
        return ""
    samples = values[-width:]
    low = min(samples)
    high = max(samples)
    if high - low < 1e-9:
        return _SPARK_LEVELS[len(_SPARK_LEVELS) // 2] * len(samples)
    span = high - low
    top = len(_SPARK_LEVELS) - 1
    return "".join(
        _SPARK_LEVELS[min(top, int((value - low) / span * top))] for value in samples
    )


def probe_state(probe: object) -> str:
    """Describe a health probe as ``online`` / ``offline`` / ``unchecked``.

    A probe that has never run has ``checked_at`` falsy; reporting that as
    "offline" would be a lie, so it reads as "unchecked" instead.
    """
    if not getattr(probe, "checked_at", 0):
        return "unchecked"
    return "online" if getattr(probe, "ok", False) else "offline"


def config_checklist(settings: Settings) -> list[dict[str, Any]]:
    """A go/no-go deployment checklist for ``/status`` and ``/test``.

    Distinct from :meth:`Settings.validate_runtime`, which only reports things
    worth *warning* about. Each item here is a deployment prerequisite with an
    explicit pass/fail so an operator can see readiness at a glance.
    """
    token = settings.discord_bot_token.get_secret_value()
    return [
        {
            "label": "Bot token",
            "ok": bool(token) and settings.token_is_well_formed,
            "detail": (
                f"set · fp {settings.token_fingerprint}"
                if token and settings.token_is_well_formed
                else ("missing" if not token else "malformed")
            ),
        },
        {
            "label": "Database driver",
            "ok": settings.database_dialect.endswith(
                ("+aiosqlite", "+asyncpg", "+asyncmy")
            ),
            "detail": settings.database_dialect,
        },
        {
            "label": "Owner IDs",
            "ok": bool(settings.owner_ids),
            "detail": (
                f"{len(settings.owner_ids)} configured"
                if settings.owner_ids
                else "none — dev commands fall back to the app owner"
            ),
        },
        {
            "label": "Command sync",
            # Guild sync is a first-class choice (instant propagation) and is
            # only a problem when its target guild is missing — not merely for
            # differing from the global default.
            "ok": (
                settings.command_sync_mode == "global"
                or settings.dev_guild_id is not None
            ),
            "detail": (
                "global (up to 1h propagation)"
                if settings.command_sync_mode == "global"
                else (
                    f"guild {settings.dev_guild_id} (instant)"
                    if settings.dev_guild_id is not None
                    else "guild mode but DEV_GUILD_ID is unset"
                )
            ),
        },
        {
            "label": "Log directory",
            "ok": settings.log_dir.is_dir(),
            "detail": str(settings.log_dir),
        },
    ]
