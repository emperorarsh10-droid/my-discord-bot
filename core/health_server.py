"""Minimal HTTP liveness server for hosts that suspend idle processes.

Why this exists
---------------
Some free hosts (Render's free web-service tier, for example) shut a process
down after a period without *inbound* traffic. A Discord bot is the worst case:
it holds an outbound gateway socket and receives no HTTP requests at all, so it
would be suspended roughly fifteen minutes after boot even while perfectly
healthy. Binding a trivial HTTP server and having an external monitor ping it
makes the process look active, so the host leaves it alone.

This is deliberately **not** a web dashboard. It exposes three GET routes and
nothing else: no auth surface, no database reads, no user data.

Dependency note
---------------
``aiohttp`` is a hard requirement of ``discord.py``, and ``aiohttp.web`` ships
inside the same wheel, so this adds nothing to ``requirements.txt`` — which
matters on a 512 MB free instance.

Liveness vs readiness
---------------------
Every route returns **200 as long as the process is running**, even when the
gateway is mid-reconnect. Returning 503 on a transient Discord blip would make a
monitor declare a false outage, and would hand the host a health signal that
contradicts "the bot is still up". The real bot state is reported in the JSON
body of ``/health`` for a human or a log to read.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from typing import Any, Final

from aiohttp import web

from core.dashboard_state import runtime_state
from core.logging_setup import get_logger

__all__ = ["HealthServer", "resolve_port"]

logger = get_logger("zagrosian.health")

#: Render (and most PaaS) inject the listening port as ``PORT``. Binding is
#: therefore opt-in: with no ``PORT`` in the environment — local development,
#: a systemd unit — nothing is started and no socket is opened.
_PORT_ENV: Final[str] = "PORT"

#: 0.0.0.0 rather than 127.0.0.1: a loopback bind is unreachable from the host's
#: proxy, which is the single most common reason a Render health check fails
#: while the service looks perfectly healthy from inside the container.
_BIND_HOST: Final[str] = "0.0.0.0"

#: Seconds to let in-flight requests finish during shutdown.
_SHUTDOWN_GRACE: Final[float] = 5.0

_ALIVE_TEXT: Final[str] = "Bot is alive"


def resolve_port() -> int | None:
    """Return the port to bind, or ``None`` when no HTTP server is wanted.

    A malformed or out-of-range ``PORT`` is logged and treated as absent: a typo
    in the host's configuration must not take the bot down with it.
    """
    raw = os.environ.get(_PORT_ENV, "").strip()
    if not raw:
        return None

    try:
        port = int(raw)
    except ValueError:
        logger.error("Ignoring %s=%r: not an integer", _PORT_ENV, raw)
        return None

    if not 1 <= port <= 65535:
        logger.error("Ignoring %s=%d: outside 1-65535", _PORT_ENV, port)
        return None

    return port


def _payload() -> dict[str, Any]:
    """Immutable snapshot of bot state for the JSON route.

    Reads only in-memory state under a short-lived lock. No database round trip:
    a slow or unreachable database must not make the health route itself time
    out, or the monitor would flap while the bot is otherwise fine.
    """
    snapshot = runtime_state.snapshot()
    probe = snapshot.get("database") or {}
    return {
        "status": snapshot["status"],
        "online": snapshot["online"],
        "uptime": snapshot["uptime_human"],
        "uptime_seconds": snapshot["uptime_seconds"],
        "latency_ms": snapshot["latency_ms"],
        "guilds": snapshot["guild_count"],
        "commands": snapshot["command_count"],
        "cogs_failed": snapshot["cogs_failed"],
        "errors_this_session": snapshot["error_count"],
        "last_error": snapshot["last_error"],
        # ``ok`` is None until /test or /status has run a real probe.
        "database": {
            "dialect": probe.get("url") or None,
            "ok": probe.get("ok"),
            "latency_ms": probe.get("latency_ms"),
        },
    }


class HealthServer:
    """Owns the aiohttp application and its listening socket.

    Usage mirrors the rest of the codebase: build, ``await start()``, and
    ``await stop()`` from inside the bot's own event loop, so the HTTP server
    and the gateway share one loop, one thread and one shutdown path.

    Passing ``port=0`` binds an ephemeral port; :attr:`port` then reports the
    one the OS actually chose, because ``TCPSite`` resolves that internally and
    nothing else would expose it.
    """

    __slots__ = ("_requested_port", "_runner", "_site")

    def __init__(self, port: int) -> None:
        self._requested_port = port
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    @property
    def port(self) -> int:
        """The bound port, resolved from the live socket where possible."""
        server = getattr(self._site, "_server", None)
        sockets = getattr(server, "sockets", None) or ()
        for sock in sockets:
            try:
                return int(sock.getsockname()[1])
            except (OSError, IndexError, TypeError, ValueError):
                continue
        return self._requested_port

    @property
    def is_running(self) -> bool:
        return self._site is not None

    # -- routes ----------------------------------------------------------
    async def _handle_root(self, _request: web.Request) -> web.Response:
        """Plain-text root, so a human opening the URL sees something obvious."""
        return web.Response(text=_ALIVE_TEXT, status=200)

    async def _handle_health(self, _request: web.Request) -> web.Response:
        """JSON liveness plus bot diagnostics. Always 200 while the process runs."""
        return web.json_response(_payload(), status=200)

    async def _handle_favicon(self, _request: web.Request) -> web.Response:
        """Browsers and some monitors request this; a 404 is noise in the log."""
        raise web.HTTPNoContent

    def _build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/", self._handle_root)
        app.router.add_get("/health", self._handle_health)
        app.router.add_get("/favicon.ico", self._handle_favicon)
        return app

    # -- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        """Bind and begin serving. Raises on failure; the caller decides policy."""
        if self.is_running:
            return

        app = self._build_app()
        runner = web.AppRunner(
            app,
            access_log=None,  # keep the monitor's pings out of the bot's logs
            shutdown_timeout=_SHUTDOWN_GRACE,
        )
        await runner.setup()

        site = web.TCPSite(runner, host=_BIND_HOST, port=self._requested_port)
        await site.start()

        self._runner = runner
        self._site = site
        logger.info(
            "Health server listening on %s:%d (/ and /health)",
            _BIND_HOST,
            self.port,
        )

    async def stop(self) -> None:
        """Close the socket and release the app. Safe to call when not running."""
        runner, self._runner = self._runner, None
        self._site = None
        if runner is None:
            return

        with contextlib.suppress(Exception):
            await runner.cleanup()
        logger.info("Health server stopped")


async def start_from_env() -> HealthServer | None:
    """Start a health server if ``PORT`` is set, else return ``None``.

    Never raises. A host that cannot bind the port is a degraded deployment, not
    a reason to refuse to run a moderation bot — the caller logs it and the
    gateway connects anyway. That way a port conflict costs you the anti-sleep
    trick, not the bot.
    """
    port = resolve_port()
    if port is None:
        logger.info(
            "No %s set; health server disabled (this deployment is expected to "
            "run as a worker, not a web service)",
            _PORT_ENV,
        )
        return None

    server = HealthServer(port)
    try:
        await server.start()
    except OSError as exc:
        logger.critical(
            "Could not bind health server on %s:%d (%s: %s). The bot will still "
            "run, but a host that suspends idle processes will suspend it too.",
            _BIND_HOST,
            port,
            type(exc).__name__,
            exc,
        )
        runtime_state.record_error("health_server", f"{type(exc).__name__}: {exc}")
        return None
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.critical("Health server failed to start: %s", exc, exc_info=True)
        runtime_state.record_error("health_server", f"{type(exc).__name__}: {exc}")
        return None

    return server
