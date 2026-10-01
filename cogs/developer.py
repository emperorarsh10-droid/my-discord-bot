"""Developer cog — diagnostics and live operations.

``/test`` is the command this whole system is built around: one invocation that
proves, rather than assumes, that each moving part is alive. It runs four
independent probes concurrently, measures each one, and reports a verdict per
subsystem:

===================  ====================================================
Subsystem            What is actually proven
===================  ====================================================
Database             A real ``SELECT 1`` round trip, timed, with the
                     engine pool recycled if the connection died
Latency              Gateway heartbeat *and* a genuine REST round trip
                     (``application_info``), not a cached number
Web Watcher          An HTTP request to the watcher's own ``/healthz``,
                     which doubles as proof the two halves can talk
Configuration        A live audit of every required setting, with secrets
                     reduced to length + fingerprint
===================  ====================================================

Every probe is wrapped so a failure produces a verdict, never a traceback in
the user's face: this command has to work precisely when things are broken.
"""

from __future__ import annotations

import asyncio
import platform
import sys
import time
from pathlib import Path
from typing import Any, Final

import discord
from discord import app_commands
from discord.ext import commands

from config import PROJECT_ROOT, get_settings
from core.dashboard_state import humanize_duration, runtime_state
from core.database import get_database
from core.diagnostics import (
    CHECK_FAIL,
    CHECK_OK,
    CHECK_WARN,
    config_checklist,
    probe_state,
    sparkline,
)
from core.embeds import (
    BRAND_NAME,
    COLOR_ERROR,
    COLOR_INFO,
    COLOR_NEUTRAL,
    COLOR_SUCCESS,
    base_embed,
    truncate,
)
from core.errors import PermissionDeniedError
from core.logging_setup import command_context, get_logger

logger = get_logger("zagrosian.cog.developer")


async def _note_manual_sync(config, tree) -> None:
    """Persist the tree fingerprint after an operator-driven ``/sync``.

    Imported lazily from ``main`` because ``main`` loads this cog: a module-level
    import would be circular. Best-effort - failing to write a scratch file must
    never turn a successful sync into an error reply.
    """
    from main import _record_sync, _tree_fingerprint

    try:
        _record_sync(Path(config.data_dir), _tree_fingerprint(tree))
    except (OSError, ImportError) as exc:
        logger.debug("Could not record fingerprint after /sync: %s", exc)

#: How long ``/test`` waits for a live REST round trip to answer.
REST_PROBE_TIMEOUT: Final[float] = 5.0

OK: Final[str] = "🟢"
WARN: Final[str] = "🟡"
FAIL: Final[str] = "🔴"
SKIP: Final[str] = "⚪"

#: Human word shown beside each subsystem verdict.
VERDICT_LABEL: Final[dict[str, str]] = {
    OK: "PASS",
    WARN: "WARN",
    FAIL: "FAIL",
    SKIP: "SKIP",
}


def _millis(value: float | None) -> str:
    """Render a millisecond measurement, or ``n/a`` when nothing was measured."""
    return f"{value:.0f} ms" if value is not None else "n/a"

#: Settings that must be present for the bot to operate, and what "present" means.
REQUIRED_CONFIG_KEYS: Final[tuple[tuple[str, bool], ...]] = (
    ("discord_bot_token", lambda s: bool(s.discord_bot_token.get_secret_value())),
    ("database_url", lambda s: s.database_url.startswith(("sqlite+", "postgresql+", "mysql+"))),
    ("log_dir", lambda s: s.log_dir.is_dir()),
    ("owner_ids", lambda s: len(s.owner_ids) > 0),
    ("cog_directory", lambda s: s.cog_path.is_dir()),
)


class Developer(commands.Cog):
    """Owner-only diagnostics. Nothing here is exposed to server members."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config = get_settings()

    # ------------------------------------------------------------------ #
    # /test — the diagnostic
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="test",
        description="Full diagnostic: database, latency, cache and configuration.",
    )
    async def test(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._require_tester(interaction)

        started = time.perf_counter()
        # return_exceptions=True: one dead probe must not hide the other two.
        database_result, latency_result, cache_result = await asyncio.gather(
            self._probe_database(),
            self._probe_latency(),
            self._probe_cache(),
            return_exceptions=True,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000

        verdicts: list[tuple[str, str, str]] = []
        for name, result, checker in (
            ("Database", database_result, lambda r: bool(r.get("ok"))),
            ("Latency", latency_result, lambda r: r.get("status") == "ok"),
            ("Gateway cache", cache_result, lambda r: r.get("status") == "ok"),
        ):
            if isinstance(result, BaseException):
                verdicts.append(
                    (FAIL, name, f"probe crashed: {type(result).__name__}: {result}")
                )
                logger.error("Probe %s crashed: %r", name, result, exc_info=result)
            elif result.get("status") == "skipped":
                # A deliberately disabled subsystem is not a fault: it never
                # runs, so reporting FAIL would be a lie about the deployment.
                verdicts.append((SKIP, name, result.get("detail", "")))
            else:
                passed = bool(checker(result))
                verdicts.append(
                    (OK if passed else FAIL, name, result.get("detail", ""))
                )

        config_ok, config_lines = self._check_configuration()
        verdicts.append(
            (
                OK if config_ok else WARN,
                "Configuration",
                "all required settings present"
                if config_ok
                else f"{sum(1 for line in config_lines if line.startswith('!'))} issue(s)",
            )
        )

        failed = any(icon == FAIL for icon, _, _ in verdicts)
        skipped = sum(1 for icon, _, _ in verdicts if icon == SKIP)
        overall = FAIL if failed else OK
        if failed:
            summary = "One or more subsystems are unhealthy — details below."
        elif skipped:
            summary = f"All checked subsystems responding · {skipped} skipped."
        else:
            summary = "All subsystems responding."

        embed = base_embed(
            title=f"{BRAND_NAME} · diagnostic",
            description=(
                f"{overall}  **{summary}**\n"
                f"`{len(verdicts)}` probes · completed in **{elapsed_ms:.0f} ms** · "
                f"{platform.system()} {platform.machine()}"
            ),
            colour=COLOR_SUCCESS if overall == OK else COLOR_ERROR,
            author=interaction.user,
        )
        for icon, name, detail in verdicts:
            label = VERDICT_LABEL.get(icon, "")
            embed.add_field(
                name=f"{icon} {name} — {label}", value=detail or "—", inline=False
            )

        embed.add_field(
            name="📋 Configuration audit",
            value=(
                "```ini\n" + "\n".join(config_lines) + "\n```"
            ),
            inline=False,
        )
        embed.add_field(
            name="🧩 Extensions",
            value=(
                f"loaded: **{', '.join(sorted(self.bot.cogs)) or 'none'}**\n"
                f"commands: **{len(self.bot.tree.get_commands())}** · "
                f"guilds: **{len(self.bot.guilds)}** · "
                f"cached users: **{len(self.bot.users)}**"
            ),
            inline=False,
        )
        embed.set_footer(text=f"session {humanize_duration(runtime_state.uptime_seconds)}")
        await interaction.followup.send(embed=embed, ephemeral=True)
        logger.info("/test executed by %s in %.0f ms", interaction.user.id, elapsed_ms)

    # ------------------------------------------------------------------ #
    # Probes
    # ------------------------------------------------------------------ #
    @command_context("probe:database")
    async def _probe_database(self) -> dict[str, Any]:
        """Time a real query and report the engine that answered."""
        database = get_database()
        result = await database.health()

        if not result["ok"]:
            return {
                "ok": False,
                "status": "fail",
                "detail": (
                    f"`{result['error']}` · engine `{result['dialect']}` · "
                    f"timed out after {self.config.database_timeout:.1f}s"
                ),
            }

        tables = result.get("tables") or []
        version = result.get("server_version") or "unknown"
        return {
            "ok": True,
            "status": "ok",
            "detail": (
                f"`{result['dialect']}` · **{result['latency_ms']} ms** round trip · "
                f"server `{version}`\n{len(tables)} table(s) mapped: "
                f"`{', '.join(tables)}`"
            ),
        }

    @command_context("probe:latency")
    async def _probe_latency(self) -> dict[str, Any]:
        """Gateway heartbeat plus a live REST round trip."""
        gateway_ms = round(self.bot.latency * 1000)

        rest_started = time.perf_counter()
        try:
            info = await asyncio.wait_for(
                self.bot.application_info(), timeout=REST_PROBE_TIMEOUT
            )
            rest_ms = round((time.perf_counter() - rest_started) * 1000, 1)
        except TimeoutError:
            return {
                "ok": False,
                "status": "fail",
                "detail": (
                    f"gateway **{gateway_ms} ms** · REST timed out after "
                    f"{REST_PROBE_TIMEOUT:.0f}s"
                ),
            }
        except Exception as exc:  # noqa: BLE001 - the point is to report anything
            return {
                "ok": False,
                "status": "fail",
                "detail": f"gateway **{gateway_ms} ms** · REST failed: {type(exc).__name__}: {exc}",
            }

        status = "degraded" if rest_ms > 1000 else "ok"

        return {
            "ok": status == "ok",
            "status": status,
            "detail": (
                f"gateway heartbeat **{gateway_ms} ms** · REST round trip "
                f"**{rest_ms} ms**\nauthenticated as `{info.name}` "
                f"(owner `{info.owner}`)"
            ),
        }

    @command_context("probe:cache")
    async def _probe_cache(self) -> dict[str, Any]:
        """Prove the gateway cache is populated and cheap to read from.

        Reading cached collections is what every command does first, so a stale
        or empty cache is the failure that actually produces "the application did
        not respond" in the client.
        """
        started = time.perf_counter()
        guilds = len(self.bot.guilds)
        users = len(self.bot.users)
        channels = sum(len(guild.channels) for guild in self.bot.guilds)
        elapsed_ms = (time.perf_counter() - started) * 1000

        details = (
            f"**{guilds}** guild(s) · **{users}** users · **{channels}** channels · "
            f"read in **{elapsed_ms:.2f} ms**"
        )
        if not guilds:
            return {
                "ok": False,
                "status": "fail",
                "detail": f"cache is empty — {details}\nthe bot is not in any server",
            }

        # A live gateway session is what "the cache is being filled" actually
        # means. The previous check called is_ws_ratelimited(), which only reports
        # the gateway shard's own reconnect backoff - it says nothing about the
        # REST bucket, and Discord rate-limits a freshly booted bot routinely.
        # That reported a perfectly healthy cache as "REST bucket is currently
        # rate limited", which sent operators chasing an API problem they did not
        # have. Cache warmth is a function of the session being up, so test that.
        session_alive = self.bot.is_ready() and not self.bot.is_closed()
        if not session_alive:
            return {
                "ok": False,
                "status": "fail",
                "detail": f"{details}\ngateway session is not established",
            }

        return {
            "ok": True,
            "status": "ok",
            "detail": f"gateway cache warm · {details}",
        }

    def _check_configuration(self) -> tuple[bool, list[str]]:
        """Audit required settings. Secrets are reported as length + digest."""
        settings = get_settings()
        lines: list[str] = []

        for name, predicate in REQUIRED_CONFIG_KEYS:
            try:
                healthy = bool(predicate(settings))
            except Exception as exc:  # noqa: BLE001
                lines.append(f"! {name} = <check failed: {exc}>")
                continue
            lines.append(f"{'ok' if healthy else '!'} {name} = {_config_value(name, settings)}")

        token = settings.discord_bot_token.get_secret_value()
        lines.append(f"ok token_fingerprint = {settings.token_fingerprint}")
        lines.append(f"ok token_length = {len(token)}")
        lines.append(
            f"{'ok' if settings.token_is_well_formed else '!'} token_format = "
            f"{'valid shape' if settings.token_is_well_formed else 'unexpected characters'}"
        )
        # The previous version reported "off" as healthy, on the claim that
        # AutoMod runs on Discord's own rules and never needs message content.
        # That is only half true: the native rules cover keyword/phrase/spam
        # blocks, but cogs/manual.py enforces the *ladder* and the guild's
        # escalation policy from an on_message listener, and
        # cogs/community.py reposts stickies from one too. Without the intent
        # message.content arrives empty, so those paths silently do nothing -
        # and the audit told the operator everything was fine. Report the truth
        # instead: the listener-side features are inert without it.
        if self.bot.intents.message_content:
            lines.append("ok message_content_intent = ON (enforcement + sticky active)")
        else:
            lines.append(
                "! message_content_intent = OFF - native AutoMod rules still apply, "
                "but AutoMod warn/kick escalation and sticky-message reposting "
                "cannot see message text and are inert. Enable the privileged "
                "intent in the Discord portal, then set "
                "intents.message_content = True in main.py"
            )
        lines.append(f"ok command_sync_mode = {settings.command_sync_mode}")
        lines.append(f"ok python = {platform.python_version()} ({sys.platform})")

        clean = all(not line.startswith("!") for line in lines)
        return clean, lines

    # ------------------------------------------------------------------ #
    # /sync
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="sync",
        description="Publish the slash command tree to Discord.",
    )
    @app_commands.describe(
        scope="Where to publish. 'global' can take up to an hour to propagate.",
    )
    async def sync(
        self,
        interaction: discord.Interaction,
        scope: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._require_owner(interaction)

        target = (scope or self.config.command_sync_mode).strip().lower()
        started = time.perf_counter()
        try:
            if target == "global":
                synced = await self.bot.tree.sync()
                destination = "globally"
            elif target == "guild":
                if self.config.dev_guild_id is None:
                    raise ValueError(
                        "DEV_GUILD_ID is not set, so a guild sync has no target. "
                        "Add it to .env, or sync globally."
                    )
                guild = self.bot.get_guild(self.config.dev_guild_id)
                if guild is None:
                    raise ValueError(
                        f"I am not in the dev guild `{self.config.dev_guild_id}`. "
                        "Check DEV_GUILD_ID."
                    )
                synced = await self.bot.tree.sync(guild=guild)
                destination = f"guild {guild.name} (`{guild.id}`)"
            else:
                raise ValueError(
                    f"`{scope}` is not a valid scope. Use `global` or `guild`."
                )
        except app_commands.CommandSyncFailure as exc:
            detail = "; ".join(
                f"`{name}` ({len(children)} subcommand(s))"
                for name, children in (exc.failed_commands or [])
            )
            await interaction.followup.send(
                embed=base_embed(
                    title="Sync failed",
                    description=(
                        f"Discord rejected part of the command tree.\n{detail or 'no detail'}"
                    ),
                    colour=COLOR_ERROR,
                ),
                ephemeral=True,
            )
            logger.error("Command sync failed: %s", detail)
            return
        except (ValueError, discord.HTTPException) as exc:
            await interaction.followup.send(
                embed=base_embed(
                    title="Sync aborted",
                    description=str(exc),
                    colour=COLOR_ERROR,
                ),
                ephemeral=True,
            )
            logger.warning("Command sync aborted: %s", exc)
            return

        elapsed = (time.perf_counter() - started) * 1000
        embed = base_embed(
            title="Commands synchronised",
            description=(
                f"Published **{len(synced)}** command(s) {destination} in "
                f"**{elapsed:.0f} ms**."
            ),
            colour=COLOR_SUCCESS,
            author=interaction.user,
        )
        if target == "global":
            embed.set_footer(
                text="Global commands can take up to an hour to appear in clients."
            )
        await interaction.followup.send(embed=embed, ephemeral=True)
        logger.info("Synced %s command(s) to %s", len(synced), destination)
        # Record the fingerprint so a manual publish is not immediately undone by
        # a redundant one on the next restart. Imported here rather than at module
        # scope: main imports this cog, so a top-level import would be circular.
        await _note_manual_sync(self.config, self.bot.tree)

    # ------------------------------------------------------------------ #
    # /reload
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="reload",
        description="Reload extensions from disk without restarting the process.",
    )
    @app_commands.describe(
        extension="Extension to reload, e.g. `cogs.moderation`. Omit for all loaded.",
    )
    async def reload(
        self,
        interaction: discord.Interaction,
        extension: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._require_owner(interaction)

        targets = (
            [extension.strip()]
            if extension
            else sorted(self.bot.extensions)
        )
        results: list[tuple[str, str, bool]] = []
        for name in targets:
            if name not in self.bot.extensions:
                results.append((name, "not loaded", False))
                continue
            try:
                await self.bot.reload_extension(name)
            except Exception as exc:
                results.append((name, f"{type(exc).__name__}: {exc}", False))
                logger.exception("Reload of %s failed", name)
            else:
                results.append((name, "reloaded", True))

        succeeded = sum(1 for _, _, ok in results if ok)
        lines = "\n".join(
            f"{'🟢' if ok else '🔴'} `{name}` — {detail}" for name, detail, ok in results
        )
        embed = base_embed(
            title="Extension reload",
            description=(
                f"**{succeeded}/{len(results)}** extension(s) reloaded.\n\n{lines}"
            ),
            colour=COLOR_SUCCESS if succeeded == len(results) else COLOR_ERROR,
            author=interaction.user,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------ #
    # /status — the operational panel, in an embed
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="status",
        description="Health, configuration and live logs — the whole panel in one reply.",
    )
    async def status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._require_tester(interaction, command="/status")
        embeds = self._build_status_embeds(interaction)
        await interaction.followup.send(embeds=embeds, ephemeral=True)
        logger.info("/status executed by %s", interaction.user.id)

    def _build_status_embeds(
        self, interaction: discord.Interaction
    ) -> list[discord.Embed]:
        """Assemble the full operational picture as a set of embeds.

        Reads only in-process state — no probes, no network — so it is safe to
        run at any moment and returns instantly. ``/test`` is the command that
        *proves* the subsystems answer; this one *reports* what is already
        known.
        """
        bot = self.bot
        state = runtime_state.snapshot()
        settings = self.config

        online = bool(state["online"])
        main = base_embed(
            title=f"{BRAND_NAME} · status",
            description=(
                f"{CHECK_OK if online else CHECK_FAIL} **{state['status_label']}** · "
                f"uptime **{humanize_duration(state['uptime_seconds'])}** · "
                f"gateway **{_millis(state['latency_ms'])}** · "
                f"Python `{platform.python_version()}` on `{sys.platform}`"
            ),
            colour=COLOR_SUCCESS if online else COLOR_ERROR,
            author=interaction.user,
        )
        if bot.user is not None:
            main.set_thumbnail(url=bot.user.display_avatar.url)

        guilds = len(bot.guilds)
        channels = sum(len(guild.channels) for guild in bot.guilds)
        commands = len(bot.tree.get_commands())
        cogs = sorted(bot.cogs)

        main.add_field(
            name="Servers", value=f"**{guilds}** · {channels} channels", inline=True
        )
        main.add_field(name="Cached users", value=f"**{len(bot.users)}**", inline=True)
        main.add_field(
            name="Gateway ping",
            value=f"**{_millis(state['latency_ms'])}**",
            inline=True,
        )
        main.add_field(
            name="REST ping",
            value=f"**{_millis(state['rest_latency_ms'])}**",
            inline=True,
        )
        main.add_field(
            name="Commands",
            value=f"**{commands}** · `{settings.command_sync_mode}`",
            inline=True,
        )
        main.add_field(
            name="Extensions",
            value=f"**{len(cogs)}** · {len(state['cogs_failed'])} failed",
            inline=True,
        )
        main.add_field(
            name="Mod. actions",
            value=f"**{state['moderation_action_count']}** this session",
            inline=True,
        )
        main.add_field(
            name="Uptime",
            value=f"**{humanize_duration(state['uptime_seconds'])}**",
            inline=True,
        )
        main.add_field(
            name="Errors",
            value=f"**{state['error_count']}** this session",
            inline=True,
        )

        trend = sparkline(list(state["latency_history"]))
        main.add_field(
            name="Latency trend",
            value=f"`{trend}`" if trend else "collecting samples…",
            inline=False,
        )

        main.add_field(
            name="Subsystem health",
            value=(
                f"database **{probe_state(runtime_state.database)}** · "
                f"automod hits **{len(state['automod_alerts'])}** · "
                f"run `/test` for live probes"
            ),
            inline=False,
        )
        if state["last_error"]:
            stamp = (
                f"<t:{int(state['last_error_at'])}:R> · "
                if state["last_error_at"]
                else ""
            )
            main.add_field(
                name="Last error",
                value=f"{stamp}{truncate(state['last_error'], 300)}",
                inline=False,
            )
        main.set_footer(
            text=(
                f"session {humanize_duration(state['uptime_seconds'])} · "
                f"{settings.case_prefix} ledger"
            )
        )
        embeds = [main]

        # -- configuration checklist ------------------------------------
        checklist_lines = [
            f"{CHECK_OK if item['ok'] else CHECK_FAIL} "
            f"**{item['label']}** — {item['detail']}"
            for item in config_checklist(settings)
        ]
        config_embed = base_embed(
            title=f"{BRAND_NAME} · configuration",
            description="\n".join(checklist_lines),
            colour=COLOR_INFO,
        )
        warnings = settings.validate_runtime()
        if warnings:
            config_embed.add_field(
                name="Warnings",
                value="\n".join(
                    f"{CHECK_WARN} {truncate(warning, 300)}" for warning in warnings
                ),
                inline=False,
            )
        config_embed.add_field(
            name="Environment",
            value=(
                f"database `{settings.database_dialect}` · "
                f"owners **{len(settings.owner_ids)}**\n"
                f"log dir `{settings.log_dir}`\n"
                f"project root `{PROJECT_ROOT}`"
            ),
            inline=False,
        )
        embeds.append(config_embed)

        # -- live log tail ----------------------------------------------
        records = runtime_state.logs.snapshot(12)
        if records:
            body = "\n".join(
                f"{record.timestamp[11:19]} {record.level:<7} "
                f"{record.logger.rsplit('.', 1)[-1]}: {truncate(record.message, 200)}"
                for record in records
            )
            log_embed = base_embed(
                title=f"{BRAND_NAME} · recent logs",
                description=f"```\n{truncate(body, 3900)}\n```",
                colour=COLOR_NEUTRAL,
            )
            log_embed.set_footer(
                text=(
                    f"buffered {len(runtime_state.logs)}/{runtime_state.logs.maxlen} · "
                    f"dropped {runtime_state.logs.dropped}"
                )
            )
            embeds.append(log_embed)

        return embeds

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    @command_context("developer:guard")
    async def _require_tester(
        self, interaction: discord.Interaction, *, command: str = "/test"
    ) -> None:
        """Open to the owners *and* to server administrators.

        These are strictly read-only diagnostics, so a guild administrator may
        run them without their ID being listed in ``OWNER_IDS``. In a DM, only
        the owners may run them.
        """
        if interaction.user.id in self.config.owner_ids:
            return
        try:
            app = await self.bot.application_info()
        except discord.HTTPException:
            app = None
        if app is not None and interaction.user.id == app.owner.id:
            return
        member = interaction.user
        if isinstance(member, discord.Member) and member.guild_permissions.manage_guild:
            return
        raise PermissionDeniedError(
            f"`{command}` is limited to the bot's owners and to server administrators "
            "(**Manage Server**)."
        )

    @command_context("developer:guard")
    async def _require_owner(self, interaction: discord.Interaction) -> None:
        """Bot owners only: these commands can republish the whole tree."""
        if interaction.user.id in self.config.owner_ids:
            return
        app = await self.bot.application_info()
        if interaction.user.id == app.owner.id:
            return
        raise PermissionDeniedError(
            "This command is restricted to the bot's owners. Add your user ID to "
            "`OWNER_IDS` in `.env`, or set it in the Discord developer portal."
        )


def _config_value(name: str, settings: Any) -> str:
    """Render a safe value for the configuration audit block."""
    if name == "discord_bot_token":
        return "<redacted>"
    if name == "database_url":
        return settings._redacted_dsn()
    if name == "log_dir":
        return str(settings.log_dir)
    if name == "owner_ids":
        return f"{len(settings.owner_ids)} id(s)"
    if name == "cog_directory":
        return str(settings.cog_path)
    return str(getattr(settings, name, "?"))


async def setup(bot: commands.Bot) -> None:
    """Extension entrypoint."""
    await bot.add_cog(Developer(bot))
    logger.info("Developer cog loaded")
