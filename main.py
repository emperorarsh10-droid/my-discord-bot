"""Entry point for The Zagrosian Eye.

Boot order matters and is deliberate:

1.  **Logging first**, so anything that fails afterwards is recorded.
2.  **Settings validated**, with warnings surfaced loudly rather than ignored.
3.  **Database connected** before any cog loads, so a cog that touches storage
    during import cannot race the schema.
4.  **Extensions loaded one by one**, each isolated: one broken cog must not
    prevent the others from loading, and the failure is reported to ``/status``
    and to ``/test``.
5.  **Slash commands published**, then the gateway connection.
6.  **Deterministic shutdown**: the sampler is cancelled and the database pool
    is disposed, in that order, on every exit path.

Run it::

    python main.py
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib
import logging
import os
import pkgutil
import signal
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final

import discord
from discord.ext import commands

from config import ConfigurationError, Settings, get_settings
from core.dashboard_state import (
    LATENCY_SAMPLE_SECONDS,
    BotStatus,
    humanize_duration,
    runtime_state,
)
from core.database import get_database
from core.errors import register_global_handlers
from core.health_server import HealthServer, start_from_env
from core.logging_setup import configure_logging, get_logger

__all__ = ["build_bot", "load_extensions", "main", "sync_commands"]

logger = get_logger("zagrosian.main")

BOT_VERSION: Final[str] = "1.0.0"

BANNER: Final[str] = r"""
   ____  __                                __     __  __           __
  / __ \/ /___ _____  ____ ___  ____ ______/ /__  / /_/ /_____  ____/ /__
 / /_/ / / __ `__ \/ __ `/ / / / __ `/ ___/ //_/  / __/ ___/ _ `/ ___/ //_/
/ ____/ / / / / / / /_/ / /_/ / /_/ / /__/ ,<    / /_/ /__/ /_/ /__/ ,<
\/____/_/_/ /_/ /_/\__,_/\__,_/\__,_/\___/_/|_|  /_.___/\___/\__,_/_/|_|
"""


class ZagrosianBot(commands.Bot):
    """The client.

    Kept as a subclass rather than a bare ``Bot`` so the process-wide clock,
    the error handlers and the instance state the cogs rely on
    (``_zagrosian_started_at``) have one obvious home.
    """

    def __init__(self, settings: Settings) -> None:
        # message_content is REQUIRED, not optional. Discord's own AutoMod rules
        # cover keyword/phrase blocks server-side, but this bot enforces the
        # *escalation ladder* and the guild's warn/kick policy from an
        # on_message listener (cogs/manual.py), and reposts sticky messages from
        # another one (cogs/community.py). All of those read message.content,
        # which arrives as an empty string when the privileged intent is off —
        # so with it disabled the bot logged in, every slash command worked, and
        # the listener-side enforcement silently did nothing at all. That is the
        # worst kind of failure to debug, so the intent is on by default and
        # /status says so plainly if the portal has not granted it yet.
        #
        # Granting it requires the Message Content privileged intent in the
        # Discord developer portal. Without it the bot still starts, but the
        # audit in /test reports the gap rather than claiming health.
        intents = discord.Intents.default()
        intents.message_content = settings.message_content_intent

        super().__init__(
            command_prefix=commands.when_mentioned_or(
                "!"
            ),  # text prefix commands are unused (all slash)
            intents=intents,
            help_command=None,
            description="Moderation and server management for the modern era.",
            # Nothing this bot sends should ever be able to ping @everyone.
            allowed_mentions=discord.AllowedMentions.none(),
            case_insensitive=True,
            strip_after_prefix=True,
            owner_ids=settings.owner_ids or None,
        )
        self.settings = settings
        self.started_at = time.monotonic()

    async def on_ready(self) -> None:
        """Announce a successful connection, once per session."""
        runtime_state.set_status(BotStatus.ONLINE)
        runtime_state.set_cog_inventory(
            self.extensions.keys(), runtime_state.cog_errors
        )
        runtime_state.set_command_count(len(self.tree.get_commands()))
        self._log_banner()

    async def on_connect(self) -> None:
        if runtime_state.status is not BotStatus.ONLINE:
            runtime_state.set_status(BotStatus.CONNECTING)

    async def on_disconnect(self) -> None:
        # Only downgrade when we are not mid-shutdown, otherwise ``/status``
        # would briefly show "offline" during a normal restart.
        if runtime_state.status is BotStatus.ONLINE:
            runtime_state.set_status(BotStatus.OFFLINE)
            logger.warning("Lost gateway connection; will attempt to reconnect")

    async def on_resumed(self) -> None:
        logger.info("Gateway session resumed")

    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Publish guild-scoped commands the moment the target guild is joined.

        If the bot boots before it has been invited, the startup sync has no
        guild to publish to. Handling the join closes that gap, so accepting the
        invite is enough — no restart required.
        """
        if self.settings.syncs_globally or guild.id != self.settings.dev_guild_id:
            return
        try:
            await _publish_guild_commands(self, guild)
        except discord.HTTPException as exc:
            logger.error("Guild sync on join failed for %s: %s", guild.id, exc)

    def _log_banner(self) -> None:
        user = self.user
        assert user is not None
        guilds = len(self.guilds)
        latency = round(self.latency * 1000)

        runtime_state.set_cache_totals(
            guilds=guilds, users=len(self.users), channels=sum(len(g.channels) for g in self.guilds)
        )
        logger.info(BANNER)
        logger.info(
            "ZAGROSIAN EYE ONLINE | %s#%s | %s guild(s) · %s cached users · "
            "%s commands · %s ms",
            user.name,
            user.discriminator or "0",
            guilds,
            len(self.users),
            len(self.tree.get_commands()),
            latency,
        )
        logger.info(
            "Session uptime %s · database `%s` · status panel `/status`",
            humanize_duration(runtime_state.uptime_seconds),
            self.settings.database_dialect,
        )
        if runtime_state.cogs_failed:
            logger.error(
                "Loaded with %s failing extension(s): %s",
                len(runtime_state.cogs_failed),
                ", ".join(runtime_state.cogs_failed),
            )


# --------------------------------------------------------------------------- #
# Boot helpers
# --------------------------------------------------------------------------- #
def build_bot(settings: Settings | None = None) -> ZagrosianBot:
    """Construct the client and install the global error handlers."""
    resolved = settings or get_settings()
    bot = ZagrosianBot(resolved)
    register_global_handlers(bot)
    return bot


def _iter_cog_names(cog_directory: str) -> list[str]:
    """Discover importable extension modules inside the cog package.

    Walks the package rather than the filesystem so a cog shipped as a package
    (a directory with ``__init__.py``) is picked up too, and skips dunder and
    private modules.
    """
    try:
        package = importlib.import_module(cog_directory)
    except ImportError as exc:
        raise RuntimeError(
            f"Cog package `{cog_directory}` could not be imported: {exc}"
        ) from exc

    names: list[str] = []
    for info in pkgutil.iter_modules(package.__path__):
        if info.name.startswith("_"):
            continue
        names.append(f"{cog_directory}.{info.name}")
    return sorted(names)


async def load_extensions(bot: ZagrosianBot, settings: Settings) -> list[str]:
    """Load every extension, isolating failures.

    Returns:
        Names of the extensions that failed, with the reason already logged.
    """
    names = _iter_cog_names(settings.cog_package)
    if not names:
        logger.warning("No extensions found in `%s`", settings.cog_package)

    loaded: list[str] = []
    failures: dict[str, str] = {}
    for name in names:
        try:
            await bot.load_extension(name)
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            failures[name] = reason
            logger.exception("Extension %s failed to load", name)
            runtime_state.record_error(f"load_extension:{name}", reason)
        else:
            loaded.append(name)

    runtime_state.set_cog_inventory(loaded, failures)
    logger.info(
        "Extensions: %s loaded, %s failed%s",
        len(loaded),
        len(failures),
        f" ({', '.join(sorted(failures))})" if failures else "",
    )
    return sorted(failures)


def _tree_fingerprint(tree: discord.app_commands.CommandTree) -> str:
    """A stable digest of everything Discord needs to know about the command tree.

    Only the fields that alter the *published* command matter: name, the option
    shape (name, type, required, choices), and nesting under groups. Handler
    bodies, cog names and descriptions are deliberately excluded for descriptions
    - a description is republished when it changes, but it is not worth forcing
    a global propagation for wording alone.

    The fingerprint exists so a restart can tell "the operator redeployed with
    new commands" apart from "the process bounced". Those look identical from
    inside the container but cost wildly different amounts: every global sync is
    ~88 HTTP calls against a shared, rate-limited budget, and each one restarts
    Discord's propagation timer for *every* server, which is what makes clients
    report the bot's commands as outdated.
    """
    parts: list[str] = []

    def walk(command: discord.app_commands.AppCommand, prefix: str) -> None:
        path = f"{prefix}{command.name}"
        if isinstance(command, discord.app_commands.Group):
            parts.append(f"G {path}")
            for child in sorted(command.commands, key=lambda c: c.name):
                walk(child, f"{path} ")
            return
        parts.append(f"C {path}")
        for option in sorted(getattr(command, "parameters", ()), key=lambda o: o.name):
            parts.append(
                f"  o {option.name} {getattr(option, 'type', '?')} "
                f"req={getattr(option, 'required', False)} "
                f"choices={sorted(str(c) for c in getattr(option, 'choices', ()) or ())}"
            )

    for command in sorted(tree.get_commands(), key=lambda c: c.name):
        walk(command, "")

    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    return digest[:16]


def _load_last_sync(state_dir: Path) -> str | None:
    """Read the fingerprint of the last successful sync, if there was one."""
    path = state_dir / "last_sync.fingerprint"
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _record_sync(state_dir: Path, fingerprint: str) -> None:
    """Remember the fingerprint so the next boot can skip a redundant publish."""
    state_dir.mkdir(parents=True, exist_ok=True)
    target = state_dir / "last_sync.fingerprint"
    tmp = state_dir / "last_sync.fingerprint.tmp"
    tmp.write_text(fingerprint, encoding="utf-8")
    tmp.replace(target)


async def sync_commands(bot: ZagrosianBot, settings: Settings) -> None:
    """Publish the slash command tree, skipping a publish that would change nothing.

    Global sync is the production path; a guild sync is available for
    development so changes appear instantly.

    An unchanged tree is not republished. A global sync costs one HTTP request
    per command, and Discord throttles the command endpoint hard; re-issuing it
    on every process restart exhausted the shared bucket and reset the
    propagation timer, so for up to an hour after each deploy clients showed the
    previous command set and rejected calls as outdated. Persisting the
    fingerprint turns a routine restart into zero REST calls while still
    publishing the moment the tree genuinely changes.

    ``SYNC_COMMANDS_ON_START=always`` restores the old eager behaviour for the
    case where Discord's cache needs nudging by hand; ``/sync`` always
    publishes on demand regardless.
    """
    fingerprint = _tree_fingerprint(bot.tree)
    state_dir = Path(settings.data_dir) if settings.data_dir else Path("data")

    if settings.sync_on_start == "never":
        logger.info("Command sync skipped (SYNC_COMMANDS_ON_START=never)")
        return

    if settings.sync_on_start == "if_changed":
        previous = _load_last_sync(state_dir)
        if previous == fingerprint:
            logger.info(
                "Command tree unchanged since last publish (%s command(s)); "
                "skipping sync to preserve the REST budget",
                len(bot.tree.get_commands()),
            )
            return

    try:
        if settings.syncs_globally:
            synced = await bot.tree.sync()
            logger.info(
                "Published %s global command(s) — Discord may take up to an hour "
                "to propagate them to clients",
                len(synced),
            )
        else:
            assert settings.dev_guild_id is not None  # guaranteed by config validation
            guild = bot.get_guild(settings.dev_guild_id)
            if guild is None:
                logger.error(
                    "Dev guild %s is not cached yet; commands publish when the bot "
                    "joins it (or on the next restart). Is DEV_GUILD_ID correct?",
                    settings.dev_guild_id,
                )
                return
            synced = await _publish_guild_commands(bot, guild)
    except discord.app_commands.CommandSyncFailure as exc:
        for name, children in exc.failed_commands or []:
            logger.error(
                "Sync failed for `%s` and %s of its subcommand(s)", name, len(children)
            )
        raise
    except discord.HTTPException as exc:
        logger.error("Could not publish the command tree: %s %s", exc.status, exc.text)
        raise

    try:
        _record_sync(state_dir, fingerprint)
    except OSError as exc:
        # Losing the fingerprint only costs one redundant sync next boot, which
        # is strictly better than refusing to publish because a scratch file
        # could not be written.
        logger.warning("Could not record sync fingerprint: %s", exc)


async def _publish_guild_commands(bot: ZagrosianBot, guild: discord.Guild) -> int:
    """Sync the command tree to a single guild and report the count."""
    synced = await bot.tree.sync(guild=guild)
    logger.info("Published %s command(s) to guild %s", len(synced), guild.id)
    return len(synced)


async def sample_latency(bot: discord.Client) -> None:
    """Feed the gateway heartbeat into the runtime state on an interval.

    Runs for the life of the process; a failure here is never fatal.
    """
    try:
        while True:
            if bot.is_ready():
                gateway_ms = round(bot.latency * 1000, 1)
                runtime_state.set_latency(
                    gateway_ms,
                    runtime_state.rest_latency_ms,
                )
                runtime_state.set_cache_totals(
                    guilds=len(bot.guilds),
                    users=len(bot.users),
                    channels=sum(len(g.channels) for g in bot.guilds),
                )
            await asyncio.sleep(LATENCY_SAMPLE_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Latency sampler stopped unexpectedly")


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop, on_shutdown: Callable[[str], None]
) -> None:
    """Arrange a clean shutdown on Ctrl+C / SIGTERM.

    The handler never calls ``loop.stop()``: stopping the loop mid-``run`` would
    strand the ``finally`` block's awaits (database dispose) and leave a
    half-dead process. Instead the callback schedules cancellation of the main
    task, so shutdown unwinds the normal path with the loop still running.

    ``loop.add_signal_handler`` is unavailable on Windows' Proactor loop, so a
    ``signal.signal`` fallback keeps behaviour identical on both platforms.
    """

    def request_shutdown(signal_name: str) -> None:
        with contextlib.suppress(Exception):
            on_shutdown(signal_name)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_shutdown, sig.name)
        except (NotImplementedError, RuntimeError, ValueError):
            # Windows: fall back to the plain signal API.
            with contextlib.suppress(ValueError, OSError):
                signal.signal(
                    sig, lambda s, _frame, name=sig.name: request_shutdown(name)
                )


def _start_exit_watchdog(
    shutdown_event: threading.Event, grace_seconds: float = 15.0
) -> None:
    """Force the process to die if a graceful shutdown overruns.

    aiosqlite runs each connection on a worker thread and ``asyncio.run`` waits
    on the default executor during teardown. A wedged worker would otherwise
    leave a zombie process holding the SQLite file — the exact failure this
    guards against.
    """

    def watch() -> None:
        shutdown_event.wait()
        time.sleep(grace_seconds)
        logger.warning(
            "Graceful shutdown exceeded %.0fs; forcing process exit", grace_seconds
        )
        logging.shutdown()
        os._exit(0)

    threading.Thread(
        target=watch, name="zagrosian-exit-watchdog", daemon=True
    ).start()


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
async def run(
    settings: Settings | None = None, *, shutdown_event: threading.Event | None = None
) -> None:
    """Boot the whole system and block until the gateway disconnects."""
    resolved = settings or get_settings()
    runtime_state.set_status(BotStatus.STARTING)

    # 0. Liveness server -----------------------------------------------
    # Started before anything slow. Render's free tier suspends a service that
    # has not answered an HTTP request, so the socket has to be accepting
    # connections *while* the database and gateway are still coming up — not
    # after them. ``start_from_env`` is a no-op when PORT is unset, which keeps
    # local runs and systemd units free of an unnecessary listener.
    health_server: HealthServer | None = await start_from_env()

    # 1. Storage -------------------------------------------------------
    database = get_database()
    try:
        await database.connect()
    except Exception as exc:
        logger.critical("Database initialisation failed: %s", exc)
        logger.critical(
            "Check DATABASE_URL in .env. The bot cannot start without storage."
        )
        if health_server is not None:
            await health_server.stop()
        raise

    # 2. Client --------------------------------------------------------
    bot = build_bot(resolved)

    # 3. Extensions ----------------------------------------------------
    await load_extensions(bot, resolved)

    # 4. Telemetry + signals -------------------------------------------
    sampler = asyncio.create_task(sample_latency(bot), name="zagrosian-latency")
    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()

    def request_shutdown(signal_name: str) -> None:
        logger.info("Received %s; shutting down cleanly", signal_name)
        runtime_state.set_status(BotStatus.SHUTTING_DOWN)
        if shutdown_event is not None:
            shutdown_event.set()
        if main_task is not None:
            # Always hop onto the loop's thread: on Windows the signal callback
            # runs on the main thread, elsewhere it runs inside the loop.
            loop.call_soon_threadsafe(main_task.cancel)

    _install_signal_handlers(loop, request_shutdown)

    # 5. Gateway -------------------------------------------------------
    exit_code = 0
    try:
        async with bot:
            background = asyncio.create_task(
                _post_ready(bot, resolved), name="zagrosian-post-ready"
            )
            try:
                await bot.start(resolved.bot_token)
            except asyncio.CancelledError:
                logger.info("Shutdown signal received; closing the gateway session")
            finally:
                # Without this, a failed login would leave the post-ready task
                # blocked on wait_until_ready() and hang the shutdown.
                background.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await background
    except discord.LoginFailure:
        exit_code = 2
        logger.critical(
            "Discord rejected the token. Check DISCORD_BOT_TOKEN in .env — a token "
            "with trailing whitespace or a stale reset will both do this."
        )
    except discord.HTTPException as exc:
        exit_code = 3
        logger.critical("Could not reach Discord: %s %s", exc.status, exc.text)
    except Exception:
        exit_code = 1
        logger.exception("Fatal error during the bot session")
    finally:
        # 6. Deterministic shutdown --------------------------------------
        runtime_state.set_status(BotStatus.SHUTTING_DOWN)
        logger.info("Shutting down")

        sampler.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sampler

        # Health server first: keep answering 200 while the rest of the process
        # unwinds. Closing it last would present a refused connection to a
        # monitor mid-shutdown and look like an outage rather than a deploy.
        if health_server is not None:
            await health_server.stop()

        with contextlib.suppress(Exception):
            await database.disconnect()

        runtime_state.set_status(BotStatus.OFFLINE)
        logger.info(
            "Shutdown complete | uptime %s | %s error(s) this session",
            humanize_duration(runtime_state.uptime_seconds),
            runtime_state.error_count,
        )

    if exit_code:
        raise SystemExit(exit_code)


async def _post_ready(bot: ZagrosianBot, settings: Settings) -> None:
    """Wait for the cache, then publish the command tree.

    Split out of :func:`run` so a sync failure surfaces in the log and on
    ``/status`` without tearing down a healthy gateway session.
    """
    await bot.wait_until_ready()
    try:
        await sync_commands(bot, settings)
    except Exception as exc:  # noqa: BLE001 - the bot stays online
        logger.error("Command sync failed: %s", exc)
        runtime_state.record_error("command_sync", f"{type(exc).__name__}: {exc}")


def main() -> int:
    """Console entry point."""
    settings = get_settings()
    configure_logging()

    logger.info("The Zagrosian Eye v%s starting", BOT_VERSION)
    try:
        warnings = settings.validate_runtime()
    except Exception as exc:  # noqa: BLE001 - a hard config failure
        logger.critical("Configuration is unusable: %s", exc)
        return 4

    for warning in warnings:
        logger.warning("Configuration: %s", warning)

    # Read through ``bot_token`` rather than the raw field: that property applies
    # the DISCORD_BOT_TOKEN / DISCORD_TOKEN / BOT_TOKEN fallback and raises a
    # ConfigurationError naming every accepted spelling. Checking the raw field
    # here instead reported "not set" even when a fallback name carried a valid
    # token, which is how a Railway deploy with DISCORD_TOKEN still refused to
    # start.
    try:
        _resolved_token = settings.bot_token
    except ConfigurationError as exc:
        logger.critical("Discord bot token unavailable: %s", exc)
        return 4
    logger.debug("Bot token resolved (%d chars)", len(_resolved_token))

    shutdown_event = threading.Event()
    _start_exit_watchdog(shutdown_event)

    with contextlib.suppress(KeyboardInterrupt):
        try:
            asyncio.run(run(settings, shutdown_event=shutdown_event))
        except SystemExit as exc:
            return int(exc.code or 0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
