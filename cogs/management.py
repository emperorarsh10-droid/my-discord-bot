"""Management cog — server setup, settings inspection and server information.

This is the administrative surface a server owner uses to hand the bot over:
point it at a log channel, provision the mute role, and confirm at a glance that
everything is wired up. Every mutation writes to ``guild_configs`` and is then
verified against the guild's *live* state, so a setting that failed to apply
cannot hide behind a successful database write.
"""

from __future__ import annotations

import asyncio
import platform
from datetime import timedelta
from pathlib import Path
from typing import Any, Final, Literal

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from config import PROJECT_ROOT, get_settings
from core.automod import (
    MAX_KEYWORD_LEN,
    find_rule,
    normalize_phrase,
    sync_automod_rule,
)
from core.backup import (
    backup_path,
    decode_payload,
    encode_payload,
    list_backup_names,
    restore_guild,
    sanitize_name,
    serialize_guild,
    summarize_backup,
)
from core.dashboard_state import runtime_state
from core.database import get_database
from core.embeds import (
    BRAND_COLOR,
    BRAND_NAME,
    COLOR_INFO,
    COLOR_NEUTRAL,
    COLOR_SUCCESS,
    COLOR_WARNING,
    base_embed,
    describe_channel,
    describe_member,
    describe_role,
    format_duration,
    parse_hex_color,
    success_embed,
    timestamp,
    truncate,
    warning_embed,
)
from core.errors import PermissionDeniedError, ZagrosError
from core.logging_setup import command_context, get_logger
from core.models import GuildBackup, GuildConfig
from core.services import (
    add_filter,
    build_audit_reason,
    ensure_mute_role,
    get_or_create_guild_config,
    has_moderation_access,
    list_filters,
    log_action,
    remove_filter,
    resolve_log_channel,
)

logger = get_logger("zagrosian.cog.management")

#: Permissions whose absence should be surfaced prominently on /serverinfo.
TRACKED_PERMISSIONS: Final[tuple[str, ...]] = (
    "Manage Roles",
    "Manage Messages",
    "Kick Members",
    "Ban Members",
    "Read Message History",
)

#: Longest a backup confirmation stays clickable.
RESTORE_CONFIRM_SECONDS: Final[float] = 120.0

#: ``/filter action:`` choices. A Literal keeps the slash command to one entry
#: instead of three near-identical commands, and Discord renders the options.
FilterAction = Literal["add", "remove", "list"]

def _build_perm_reports(me: discord.Member) -> tuple[list[str], list[str]]:
    """Split the tracked permissions into (granted, missing) for this guild."""
    perms = me.guild_permissions
    granted: list[str] = []
    missing: list[str] = []
    for name in TRACKED_PERMISSIONS:
        key = name.lower().replace(" ", "_")
        (granted if getattr(perms, key, False) else missing).append(name)
    return granted, missing


def _render_names(items: list[str], limit: int = 6) -> str:
    """Render a permission list for a narrow embed column."""
    if not items:
        return "—"
    shown = " · ".join(items[:limit])
    extra = len(items) - limit
    return f"{shown} (+{extra} more)" if extra > 0 else shown


def _probe_state(probe: object) -> str:
    """Describe a health probe as online / offline / unchecked.

    A probe that has never run has ``checked_at == 0``; reporting that as
    "offline" would be a lie, so it reads as "unchecked" instead.
    """
    if not getattr(probe, "checked_at", 0):
        return "**unchecked**"
    return "**online**" if getattr(probe, "ok", False) else "**offline**"


def verification_label(level: discord.VerificationLevel) -> str:
    """Map Discord's verification enum onto a readable label."""
    return {
        discord.VerificationLevel.none: "None",
        discord.VerificationLevel.low: "Low — verified email",
        discord.VerificationLevel.medium: "Medium — registered for 5 minutes",
        discord.VerificationLevel.high: "High — member for 10 minutes",
        discord.VerificationLevel.very_high: "Highest — verified phone",
    }.get(level, str(level))


# --------------------------------------------------------------------------- #
# /setup — one-time provisioning
#
# A module-level Group instance is required: the group must exist at import time
# for ``@setup_group.command()`` to attach to it, and it is grafted onto the
# command tree by ``Management.cog_load``.
# --------------------------------------------------------------------------- #
setup_group = app_commands.Group(
    name="setup",
    description="One-time server provisioning for the bot.",
)


class Management(commands.Cog):
    """Server settings, log-channel setup and diagnostics for the server itself."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config = get_settings()
        #: user id -> channel id the embed draft should post to.
        self._pending_drafts: dict[int, int] = {}

    async def cog_load(self) -> None:
        self.bot.tree.add_command(setup_group)
        logger.debug("Registered /setup group")

    def cog_unload(self) -> None:
        self.bot.tree.remove_command("setup")

    # ------------------------------------------------------------------ #
    # /setup logs
    # ------------------------------------------------------------------ #
    @setup_group.command(
        name="logs", description="Route moderation logs to a specific channel."
    )
    @app_commands.guild_only
    @app_commands.describe(
        channel=(
            "The channel moderation actions should be written to. "
            "Omit to clear it and re-detect."
        ),
    )
    async def setup_logs(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._require_manager(interaction)

        guild = interaction.guild
        me = guild.me

        if channel is not None:
            self._assert_can_log(me, channel)

        stored = await self._persist(guild.id, log_channel_id=channel.id if channel else None)
        if stored is None:
            raise ZagrosError(
                "The settings database is unreachable, so nothing was changed."
            )

        if channel is not None:
            await interaction.followup.send(
                embed=success_embed(
                    f"Moderation actions will now be written to "
                    f"{describe_channel(channel)}.\n"
                    f"Run `/serverinfo` to confirm the whole configuration.",
                    title="Log channel configured",
                ),
                ephemeral=True,
            )
            logger.info(
                "Guild %s log channel set to %s by %s",
                guild.id, channel.id, interaction.user.id,
            )
            return

        # No channel given: clear it, then report what we can fall back to.
        detected = await resolve_log_channel(guild)
        if detected is None:
            await interaction.followup.send(
                embed=warning_embed(
                    "I cleared the configured channel and cannot find a usable "
                    "replacement.\n\nUntil you run this again with a channel, "
                    "moderation actions will not be logged anywhere. Give me "
                    "**View Channel**, **Send Messages**, **Embed Links** and "
                    "**Read Message History** in the target channel, then "
                    "re-invite me if the overrides are still missing.",
                    title="No log channel available",
                ),
                ephemeral=True,
            )
            return

        await self._persist(guild.id, log_channel_id=detected.id)
        await interaction.followup.send(
            embed=success_embed(
                f"Log channel cleared. Falling back to {describe_channel(detected)}.",
                title="Log channel re-detected",
            ),
            ephemeral=True,
        )

    # ------------------------------------------------------------------ #
    # /setup muted-role
    # ------------------------------------------------------------------ #
    @setup_group.command(
        name="muted-role",
        description="Choose, or create and position, the role used by /mute.",
    )
    @app_commands.guild_only
    @app_commands.describe(
        role="An existing role to use. Omit to auto-create and position one.",
    )
    async def setup_muted_role(
        self,
        interaction: discord.Interaction,
        role: discord.Role | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._require_manager(interaction, require=("manage_roles",))

        guild = interaction.guild

        if role is not None:
            if not role.is_assignable():
                raise PermissionDeniedError(
                    f"`{role.name}` is managed by an integration — a bot role or a "
                    "subscription tier — and cannot be assigned to members."
                )
            if role >= guild.me.top_role:
                raise PermissionDeniedError(
                    f"`{role.name}` sits at or above my highest role "
                    f"(`{guild.me.top_role.name}`), so I could never assign it. "
                    "Move my role above it first."
                )
            if await self._persist(guild.id, muted_role_id=role.id) is None:
                raise ZagrosError(
                    "The settings database is unreachable, so nothing was changed."
                )
            await interaction.followup.send(
                embed=success_embed(
                    f"`/mute` will now use {describe_role(role)}.",
                    title="Mute role configured",
                ),
                ephemeral=True,
            )
            logger.info(
                "Guild %s mute role set to %s by %s",
                guild.id, role.id, interaction.user.id,
            )
            return

        created = await ensure_mute_role(guild)
        await interaction.followup.send(
            embed=success_embed(
                f"`/mute` will use {describe_role(created)}, positioned just below "
                "my highest role so I can always assign it.",
                title="Mute role ready",
            ),
            ephemeral=True,
        )
        logger.info("Guild %s mute role provisioned as %s", guild.id, created.id)

    # ------------------------------------------------------------------ #
    # /settings
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="settings",
        description="View every setting this bot holds for this server.",
    )
    @app_commands.guild_only
    async def settings_view(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._require_manager(interaction)

        guild = interaction.guild
        config = await get_or_create_guild_config(guild.id)
        embed = base_embed(
            title=f"Settings · {guild.name}",
            colour=COLOR_INFO,
            author=interaction.user,
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)

        log_channel = (
            guild.get_channel(config.log_channel_id)
            if config and config.log_channel_id
            else None
        )
        mute_role = (
            guild.get_role(config.muted_role_id)
            if config and config.muted_role_id
            else None
        )

        embed.add_field(
            name="Moderation log",
            value=(
                describe_channel(log_channel)
                if isinstance(log_channel, discord.abc.GuildChannel)
                else "**Not set** — run `/setup logs`"
            ),
            inline=True,
        )
        embed.add_field(
            name="Mute role",
            value=(
                describe_role(mute_role)
                if mute_role is not None
                else f"**Not set** — `/mute` creates `{self.config.default_mute_role}` on demand"
            ),
            inline=True,
        )
        embed.add_field(
            name="DM on punishment",
            value=(
                "**Disabled**"
                if config is not None and config.dm_on_punish is False
                else "**Enabled**"
            ),
            inline=True,
        )
        embed.add_field(
            name="Actions recorded",
            value=str(config.action_count if config else 0),
            inline=True,
        )
        embed.add_field(
            name="Case prefix",
            value=f"`{self.config.case_prefix}-000001`",
            inline=True,
        )
        embed.set_footer(
            text=(
                f"Last changed {config.updated_at:%Y-%m-%d %H:%M} UTC"
                if config is not None and config.updated_at is not None
                else "No configuration stored yet"
            )
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="toggle-dm",
        description="Turn member DMs on punishment on or off for this server.",
    )
    @app_commands.guild_only
    @app_commands.describe(enabled="Whether punished members should be DMed.")
    async def toggle_dm(self, interaction: discord.Interaction, enabled: bool) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._require_manager(interaction)

        if await self._persist(interaction.guild_id, dm_on_punish=enabled) is None:
            raise ZagrosError(
                "The settings database is unreachable, so nothing was changed."
            )
        await interaction.followup.send(
            embed=success_embed(
                f"Members will {'be' if enabled else 'no longer be'} DMed when "
                "they are punished.",
                title="DM policy updated",
            ),
            ephemeral=True,
        )

    # ------------------------------------------------------------------ #
    # /userinfo
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="userinfo",
        description="Everything this bot knows about a member of this server.",
    )
    @app_commands.guild_only
    @app_commands.describe(member="The member to inspect. Defaults to you.")
    async def userinfo(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        target = member or interaction.user

        embed = base_embed(
            title=f"{target.display_name}",
            description=describe_member(target),
            colour=COLOR_SUCCESS if not target.bot else COLOR_INFO,
            author=interaction.user,
            thumbnail=target.display_avatar.url,
        )
        embed.add_field(name="Handle", value=f"`{target}`", inline=True)
        embed.add_field(name="User ID", value=f"`{target.id}`", inline=True)
        embed.add_field(
            name="Account created", value=timestamp(target.created_at), inline=True
        )

        joined = getattr(target, "joined_at", None)
        embed.add_field(
            name="Joined server",
            value=f"{timestamp(joined)}" if joined else "—",
            inline=True,
        )
        embed.add_field(
            name="Top role",
            value=describe_role(target.top_role) if target.top_role else "—",
            inline=True,
        )
        embed.add_field(
            name="Boosting",
            value=timestamp(target.premium_since) if target.premium_since else "No",
            inline=True,
        )

        roles = [r for r in reversed(target.roles) if r.name != "@everyone"]
        embed.add_field(
            name=f"Roles ({len(roles)})",
            value=_render_names([r.name for r in roles], limit=20) or "none",
            inline=False,
        )
        embed.add_field(
            name="Key permissions",
            value=_render_names(
                [
                    name
                    for name, ok in (
                        ("Administrator", target.guild_permissions.administrator),
                        ("Manage Server", target.guild_permissions.manage_guild),
                        ("Manage Roles", target.guild_permissions.manage_roles),
                        ("Manage Messages", target.guild_permissions.manage_messages),
                        ("Kick Members", target.guild_permissions.kick_members),
                        ("Ban Members", target.guild_permissions.ban_members),
                    )
                    if ok
                ],
                limit=6,
            )
            or "none",
            inline=False,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------ #
    # /slowmode
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="slowmode",
        description="Set the slowmode delay for a text channel.",
    )
    @app_commands.guild_only
    @app_commands.describe(
        seconds="Slowmode delay in seconds (0 disables). Maximum 21600 (6 hours).",
        channel="The channel to change. Defaults to the current one.",
    )
    async def slowmode(
        self,
        interaction: discord.Interaction,
        seconds: app_commands.Range[int, 0, 21600],
        channel: discord.TextChannel | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._require_manager(interaction, require=("manage_channels",))

        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            raise ZagrosError("Slowmode only applies to text channels.")

        if not interaction.guild.me.permissions_in(target).manage_channels:
            raise PermissionDeniedError(
                f"I need **Manage Channels** in {describe_channel(target)} to "
                "change its slowmode."
            )

        reason = build_audit_reason(
            None, interaction.user, f"Slowmode set to {int(seconds)}s"
        )
        try:
            await target.edit(slowmode_delay=int(seconds), reason=reason)
        except discord.Forbidden as exc:
            raise PermissionDeniedError(
                f"Discord refused the change in {describe_channel(target)}."
            ) from exc
        except discord.HTTPException as exc:
            raise ZagrosError(
                f"Discord returned `{exc.status}` while editing the channel."
            ) from exc

        await interaction.followup.send(
            embed=success_embed(
                (
                    f"Slowmode disabled in {describe_channel(target)}."
                    if seconds == 0
                    else f"Slowmode set to **{int(seconds)}s** in "
                    f"{describe_channel(target)}."
                ),
                title="Slowmode updated",
            ),
            ephemeral=True,
        )
        logger.info(
            "Slowmode %ss set on %s/%s by %s",
            int(seconds), interaction.guild_id, target.id, interaction.user.id,
        )

    # ------------------------------------------------------------------ #
    # /lockdown & /unlock
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="lockdown",
        description="Deny @everyone permission to send messages in a channel.",
    )
    @app_commands.guild_only
    @app_commands.describe(
        channel="Channel to lock. Defaults to the current one.",
        reason="Why the channel is being locked.",
    )
    async def lockdown(
        self,
        interaction: discord.Interaction,
        channel: discord.abc.GuildChannel | None = None,
        reason: app_commands.Range[str, 1, 512] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._require_manager(interaction, require=("manage_roles",))

        guild = interaction.guild
        target = channel or interaction.channel
        if not isinstance(target, discord.abc.GuildChannel) or not hasattr(
            target, "set_permissions"
        ):
            raise ZagrosError("Only standard server channels can be locked.")

        if not guild.me.permissions_in(target).manage_roles:
            raise PermissionDeniedError(
                f"I need **Manage Roles** in {describe_channel(target)} to lock it."
            )

        reason_text = (reason or "Channel lockdown").strip()
        try:
            await target.set_permissions(
                guild.default_role,
                send_messages=False,
                reason=build_audit_reason(None, interaction.user, reason_text),
            )
        except discord.Forbidden as exc:
            raise PermissionDeniedError(
                f"I could not override permissions in {describe_channel(target)}."
            ) from exc
        except discord.HTTPException as exc:
            raise ZagrosError(
                f"Discord returned `{exc.status}` while locking the channel."
            ) from exc

        await interaction.followup.send(
            embed=success_embed(
                f"{describe_channel(target)} is now locked — **@everyone** cannot "
                f"send messages there.\nRun `/unlock` to restore it.",
                title="Channel locked",
            ),
            ephemeral=True,
        )
        logger.info(
            "Lockdown on %s/%s by %s (%s)",
            guild.id, target.id, interaction.user.id, reason_text,
        )

    @app_commands.command(
        name="unlock",
        description="Restore @everyone's ability to send messages in a locked channel.",
    )
    @app_commands.guild_only
    @app_commands.describe(
        channel="Channel to unlock. Defaults to the current one.",
        reason="Why the channel is being unlocked.",
    )
    async def unlock(
        self,
        interaction: discord.Interaction,
        channel: discord.abc.GuildChannel | None = None,
        reason: app_commands.Range[str, 1, 512] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._require_manager(interaction, require=("manage_roles",))

        guild = interaction.guild
        target = channel or interaction.channel
        if not isinstance(target, discord.abc.GuildChannel) or not hasattr(
            target, "set_permissions"
        ):
            raise ZagrosError("Only standard server channels can be unlocked.")

        if not guild.me.permissions_in(target).manage_roles:
            raise PermissionDeniedError(
                f"I need **Manage Roles** in {describe_channel(target)} to unlock it."
            )

        overwrite = target.overwrites_for(guild.default_role)
        if overwrite.send_messages is not False:
            raise ZagrosError(
                f"{describe_channel(target)} is not locked for **@everyone**."
            )

        reason_text = (reason or "Channel unlock").strip()
        # Clear only ``send_messages`` back to neutral; every other permission
        # on the overwrite (if any) is preserved. An overwrite that becomes
        # empty is deleted outright by Discord.
        overwrite.send_messages = None
        try:
            await target.set_permissions(
                guild.default_role,
                overwrite=overwrite,
                reason=build_audit_reason(None, interaction.user, reason_text),
            )
        except discord.Forbidden as exc:
            raise PermissionDeniedError(
                f"I could not override permissions in {describe_channel(target)}."
            ) from exc
        except discord.HTTPException as exc:
            raise ZagrosError(
                f"Discord returned `{exc.status}` while unlocking the channel."
            ) from exc

        await interaction.followup.send(
            embed=success_embed(
                f"{describe_channel(target)} is unlocked — **@everyone** can send "
                "messages again.",
                title="Channel unlocked",
            ),
            ephemeral=True,
        )
        logger.info(
            "Unlock on %s/%s by %s (%s)",
            guild.id, target.id, interaction.user.id, reason_text,
        )

    # ------------------------------------------------------------------ #
    # /botstatus
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="botstatus",
        description="An at-a-glance connection status card for the bot.",
    )
    @app_commands.guild_only
    async def botstatus(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        status = runtime_state.status
        live = status.is_live
        embed = base_embed(
            title=f"{BRAND_NAME} · status",
            colour=COLOR_SUCCESS if live else COLOR_NEUTRAL,
            author=interaction.user,
        )
        embed.add_field(name="State", value=f"**{status.label}**", inline=True)
        embed.add_field(
            name="Gateway",
            value=f"**{round(self.bot.latency * 1000)} ms**",
            inline=True,
        )
        embed.add_field(
            name="Session uptime",
            value=f"**{format_duration(timedelta(seconds=runtime_state.uptime_seconds))}**",
            inline=True,
        )
        embed.add_field(
            name="Websocket",
            value="connected" if self.bot.is_ready() else "not ready",
            inline=True,
        )
        embed.add_field(
            name="Servers",
            value=f"**{len(self.bot.guilds)}**",
            inline=True,
        )
        embed.add_field(
            name="Subsystems",
            value=f"database {_probe_state(runtime_state.database)}",
            inline=True,
        )
        if runtime_state.error_count:
            embed.set_footer(
                text=f"{runtime_state.error_count} session error(s) recorded"
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------ #
    # /serverinfo
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="serverinfo",
        description="A live snapshot of this server and this bot's presence in it.",
    )
    @app_commands.guild_only
    async def serverinfo(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        created = guild.created_at
        embed = base_embed(
            title=guild.name,
            colour=COLOR_NEUTRAL,
            description=guild.description or "This server has no description.",
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)

        embed.add_field(
            name="Created",
            value=f"<t:{int(created.timestamp())}:F> · <t:{int(created.timestamp())}:R>",
            inline=True,
        )
        embed.add_field(name="Owner", value=f"<@{guild.owner_id}>", inline=True)
        embed.add_field(
            name="Members", value=f"**{guild.member_count or 0}** total", inline=True
        )
        embed.add_field(
            name="Channels",
            value=f"{len(guild.channels)} · roles {len(guild.roles)}",
            inline=True,
        )
        embed.add_field(
            name="Boosts",
            value=(
                f"Tier **{guild.premium_tier}** · "
                f"**{guild.premium_subscription_count or 0}** boosts"
            ),
            inline=True,
        )
        embed.add_field(
            name="Verification",
            value=verification_label(guild.verification_level),
            inline=True,
        )

        await self._append_bot_section(guild, embed)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------ #
    # /system
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="system",
        description="Runtime information: uptime, host, latency and loaded extensions.",
    )
    @app_commands.guild_only
    async def system(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        embed = base_embed(
            title=f"{BRAND_NAME} · system",
            colour=COLOR_INFO,
            author=interaction.user,
        )
        embed.add_field(
            name="Latency", value=f"**{round(self.bot.latency * 1000)} ms**", inline=True
        )
        embed.add_field(
            name="Servers",
            value=f"**{len(self.bot.guilds)}** · users **{len(self.bot.users)}**",
            inline=True,
        )
        embed.add_field(
            name="Commands",
            value=f"**{len(self.bot.tree.get_commands())}** slash commands",
            inline=True,
        )
        embed.add_field(
            name="Host",
            value=(
                f"Python **{platform.python_version()}** on "
                f"**{platform.system()} {platform.release()}**"
            ),
            inline=False,
        )
        embed.add_field(
            name="Uptime",
            value=f"**{format_duration(timedelta(seconds=runtime_state.uptime_seconds))}**",
            inline=True,
        )
        embed.add_field(
            name="Subsystems",
            value=f"database {_probe_state(runtime_state.database)}",
            inline=True,
        )
        if runtime_state.error_count:
            embed.add_field(
                name="Session errors",
                value=(
                    f"**{runtime_state.error_count}** · last: "
                    f"{truncate(runtime_state.last_error or 'unknown', 120)}"
                ),
                inline=False,
            )
        embed.add_field(
            name="Extensions",
            value=_render_names(sorted(self.bot.cogs), limit=24) or "none",
            inline=False,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------ #
    # /filter — native AutoMod phrase blocking
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="filter",
        description="Block or unblock phrases. Discord's own AutoMod does the enforcing.",
    )
    @app_commands.guild_only
    @app_commands.describe(
        action="Whether to add a phrase, remove one, or list the current filter.",
        phrase="The phrase to block. Not needed for `list`.",
    )
    async def filter_phrases(
        self,
        interaction: discord.Interaction,
        action: FilterAction,
        phrase: app_commands.Range[str, 1, 120] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._require_manager(interaction)

        guild = interaction.guild
        phrases = await list_filters(guild.id)

        if action == "list":
            await interaction.followup.send(
                embed=await self._filter_embed(guild, phrases), ephemeral=True
            )
            return

        if not phrase:
            raise ZagrosError(
                f"Tell me the phrase to {action} — `/filter action:{action} "
                "phrase:...`."
            )

        canonical = normalize_phrase(phrase)[:MAX_KEYWORD_LEN].strip()
        if not canonical:
            raise ZagrosError(
                "That phrase is nothing but whitespace, so there is nothing to "
                "block."
            )

        if action == "add":
            if canonical in phrases:
                await interaction.followup.send(
                    embed=warning_embed(
                        f"`{canonical}` is already blocked in this server. Nothing "
                        "changed.",
                        title="Already filtered",
                    ),
                    ephemeral=True,
                )
                return
            try:
                await add_filter(
                    guild.id, canonical, created_by=interaction.user.id
                )
            except SQLAlchemyError as exc:
                raise ZagrosError(
                    "The filter database refused the write, so nothing was changed."
                ) from exc
            updated = await list_filters(guild.id)
        else:
            if canonical not in phrases:
                await interaction.followup.send(
                    embed=warning_embed(
                        f"`{canonical}` is not in this server's filter, so there is "
                        "nothing to remove.",
                        title="Not filtered",
                    ),
                    ephemeral=True,
                )
                return
            try:
                await remove_filter(guild.id, canonical)
            except SQLAlchemyError as exc:
                raise ZagrosError(
                    "The filter database refused the write, so nothing was changed."
                ) from exc
            updated = await list_filters(guild.id)

        await self._sync_filter(interaction, guild, updated)
        logger.info(
            "Filter %s in %s by %s: %r (now %s phrase(s))",
            action, guild.id, interaction.user.id, canonical, len(updated),
        )

    async def _sync_filter(
        self,
        interaction: discord.Interaction,
        guild: discord.Guild,
        phrases: list[str],
    ) -> None:
        """Push ``phrases`` to the native rule and report the honest result.

        The local table is already written by the time this runs, so a failure
        here is a *partial* state, not a lost change: the phrases are remembered
        and the next `/filter` run re-syncs them. That is stated plainly instead
        of pretending the block is live.
        """
        try:
            outcome = await sync_automod_rule(
                guild, phrases, reason=build_audit_reason(
                    None, interaction.user, "Phrase filter updated"
                )
            )
        except discord.Forbidden as exc:
            await interaction.followup.send(
                embed=warning_embed(
                    "The phrase list is saved, but I could not touch Discord's "
                    "AutoMod rule — I need the **Manage Server** permission in "
                    f"{describe_channel(interaction.channel)}.\n\n"
                    "Nothing is being *blocked* until that is fixed. Re-run the "
                    "command afterwards and I will re-apply it.",
                    title="Saved, but not enforced yet",
                ),
                ephemeral=True,
            )
            logger.warning(
                "Automod sync refused in %s (missing Manage Guild): %s", guild.id, exc
            )
            return
        except discord.HTTPException as exc:
            await interaction.followup.send(
                embed=warning_embed(
                    f"The phrase list is saved, but Discord rejected the rule "
                    f"change (`{exc.status}`). Nothing is being *blocked* until the "
                    "sync succeeds.",
                    title="Saved, but not enforced yet",
                ),
                ephemeral=True,
            )
            logger.warning("Automod sync HTTP error in %s: %s", guild.id, exc)
            return

        embed = success_embed(
            f"{len(phrases)} phrase(s) now blocked by Discord's AutoMod.\n{outcome}",
            title="Filter updated",
            author=interaction.user,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _filter_embed(
        self, guild: discord.Guild, phrases: list[str]
    ) -> discord.Embed:
        """Render the current filter plus whether the native rule agrees."""
        rule = await find_rule(guild)
        embed = base_embed(
            title=f"Blocked phrases · {guild.name}",
            colour=COLOR_INFO if phrases else COLOR_NEUTRAL,
        )
        if phrases:
            embed.description = "\n".join(f"• `{p}`" for p in phrases[:20]) or "—"
            if len(phrases) > 20:
                embed.description += f"\n_…and {len(phrases) - 20} more_"
        else:
            embed.description = (
                "No phrases are blocked. Add one with "
                "`/filter action:add phrase:...`."
            )

        if not phrases:
            state = "**inactive** — nothing to enforce"
        elif rule is None:
            state = (
                "**rule missing** — Discord's rule was deleted by hand; re-run "
                "`/filter action:add` to rebuild it"
            )
        else:
            live = {p.casefold() for p in (rule.trigger.keyword_filter or [])}
            drift = sorted(set(phrases) - live)
            state = (
                f"**live** — rule `{rule.name}` enforcing "
                f"{len(rule.trigger.keyword_filter or [])} keyword(s)"
                if not drift
                else (
                    f"**drifted** — {len(drift)} phrase(s) are saved but missing "
                    f"from the rule: `{'`, `'.join(drift[:5])}`"
                )
            )
        embed.add_field(name="Enforcement", value=state, inline=False)
        embed.set_footer(
            text=(
                "Message Content intent stays off — enforcement is Discord-side, "
                "not by reading messages."
            )
        )
        return embed

    # ------------------------------------------------------------------ #
    # /embed_builder
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="embed_builder",
        description="Compose an embed through a short form and post it yourself.",
    )
    @app_commands.guild_only
    @app_commands.describe(
        title="Headline text. Required.",
        description="Body text below the headline.",
        color="Accent colour as a hex value, e.g. `#7C5CFF`. Omit for the brand colour.",
        image="URL of an image to show large on the right.",
        footer="Small print along the bottom.",
        channel="Where to post it. Defaults to this channel.",
    )
    async def embed_builder(
        self,
        interaction: discord.Interaction,
        title: app_commands.Range[str, 1, 256],
        description: app_commands.Range[str, 1, 4000] | None = None,
        color: app_commands.Range[str, 1, 9] | None = None,
        image: app_commands.Range[str, 1, 500] | None = None,
        footer: app_commands.Range[str, 1, 2048] | None = None,
        channel: discord.TextChannel | None = None,
    ) -> None:
        await self._require_manager(interaction, require=("manage_channels",))

        if color is not None and parse_hex_color(color) is None:
            raise ZagrosError(
                f"`{truncate(color, 20)}` is not a hex colour. Use six digits — "
                "`#7C5CFF`, `7C5CFF` or `0x7C5CFF`."
            )

        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            raise ZagrosError("Embeds can only be posted in a text channel.")
        self._assert_can_log(interaction.guild.me, target)

        await interaction.response.send_modal(
            EmbedDraftModal(
                title=title,
                body=description or "",
                colour=color or "",
                image=image or "",
                footer=footer or "",
            )
        )
        self._pending_drafts[interaction.user.id] = target.id
        logger.debug("Embed draft opened for %s in %s", interaction.user.id, target.id)

    @app_commands.command(
        name="embed_cancel",
        description="Throw away the embed draft you have open.",
    )
    @app_commands.guild_only
    async def embed_cancel(self, interaction: discord.Interaction) -> None:
        self._pending_drafts.pop(interaction.user.id, None)
        await interaction.response.send_message(
            embed=success_embed("Draft discarded.", title="Cancelled"), ephemeral=True
        )

    @commands.Cog.listener()
    async def on_modal_submit(self, interaction: discord.Interaction) -> None:
        """Post the composed embed once the draft form is submitted."""
        if not isinstance(interaction, EmbedDraftModal):
            return
        if not interaction.response.is_done():
            return

        guild = self.bot.get_guild(interaction.guild_id)
        if guild is None:
            await interaction.response.send_message(
                "I am no longer in this server, so there is nowhere to post that.",
                ephemeral=True,
            )
            return

        channel_id = self._pending_drafts.pop(interaction.user.id, None)
        target = guild.get_channel(channel_id) if channel_id else interaction.channel
        if not isinstance(target, discord.TextChannel):
            await interaction.response.send_message(
                "I cannot post embeds in that channel any more. Run the command "
                "again and pick another one.",
                ephemeral=True,
            )
            return

        colour = parse_hex_color(interaction.colour) if interaction.colour else None
        embed = base_embed(
            title=interaction.title,
            description=interaction.body or None,
            colour=colour if colour is not None else BRAND_COLOR,
            author=interaction.user,
        )
        if interaction.image:
            if not interaction.image.lower().startswith(("http://", "https://")):
                await interaction.response.send_message(
                    f"`{truncate(interaction.image, 60)}` is not an http(s) URL, so I "
                    "did not attach it.",
                    ephemeral=True,
                )
                return
            embed.set_image(url=interaction.image)
        if interaction.footer:
            embed.set_footer(text=interaction.footer)

        try:
            await target.send(embed=embed)
        except discord.Forbidden as exc:
            await interaction.response.send_message(
                f"I am missing permission to post in {describe_channel(target)}.",
                ephemeral=True,
            )
            logger.warning("Embed post refused in %s/%s: %s", guild.id, target.id, exc)
            return
        except discord.HTTPException as exc:
            await interaction.response.send_message(
                f"Discord rejected that embed (`{exc.status}`). Long titles, "
                "descriptions or footers are the usual cause.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=success_embed(
                f"Posted in {describe_channel(target)}.", title="Embed sent"
            ),
            ephemeral=True,
        )
        logger.info(
            "Embed posted to %s/%s by %s", guild.id, target.id, interaction.user.id
        )

    # ------------------------------------------------------------------ #
    # /backup_create & /backup_load
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="backup_create",
        description="Snapshot this server's roles, channels and settings to a sealed file.",
    )
    @app_commands.guild_only
    @app_commands.describe(
        name="A label for this backup, e.g. `before-reorganisation`. Reusing a "
             "label overwrites it.",
    )
    async def backup_create(
        self,
        interaction: discord.Interaction,
        name: app_commands.Range[str, 1, 64],
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._require_manager(interaction)

        guild = interaction.guild
        label = sanitize_name(name)
        payload = serialize_guild(guild)
        summary = summarize_backup(payload)

        directory = self._backup_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = backup_path(directory, guild.id, label)
        envelope = encode_payload(payload, self.config.bot_token)
        try:
            path.write_text(envelope, encoding="utf-8")
        except OSError as exc:
            raise ZagrosError(
                f"I could not write the backup file to `{path}`: {exc.strerror or exc}."
            ) from exc

        try:
            await self._record_backup(guild.id, label, envelope, summary, interaction.user.id)
        except SQLAlchemyError as exc:
            # The file is the backup of record; the row is an index. Losing the
            # index is recoverable, so the file is left in place and reported.
            logger.error("Backup index write failed for %s/%s: %s", guild.id, label, exc)
            await interaction.followup.send(
                embed=warning_embed(
                    f"The backup is safe on disk at `{path.name}` but I could not "
                    "index it, so `/backup_load` will not list it. The file itself "
                    "is complete.",
                    title="Backup written, not indexed",
                ),
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            embed=success_embed(
                f"**{summary['guild_name']}** — {summary['roles']} role(s), "
                f"{summary['channels']} channel(s).\n"
                f"Saved as `{label}` in `{path.parent.name}/`.\n"
                f"Restoring reapplies names and colours only; it never creates or "
                "deletes channels and never widens anyone's permissions.",
                title="Backup created",
            ),
            ephemeral=True,
        )
        logger.info(
            "Backup %s/%s written (%s roles, %s channels) by %s",
            guild.id, label, summary["roles"], summary["channels"], interaction.user.id,
        )

    @app_commands.command(
        name="backup_load",
        description="Reapply a stored backup's names, colours and positions.",
    )
    @app_commands.guild_only
    @app_commands.describe(
        name="Which backup to load. Omit to list what is available.",
    )
    async def backup_load(
        self,
        interaction: discord.Interaction,
        name: app_commands.Range[str, 1, 64] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._require_manager(interaction)

        guild = interaction.guild
        directory = self._backup_dir()
        stored = await self._list_backups(guild.id, directory)

        if name is None:
            embed = base_embed(
                title=f"Backups · {guild.name}",
                colour=COLOR_INFO if stored else COLOR_NEUTRAL,
                description=(
                    "\n".join(
                        f"• `{label}` — {row.role_count} roles, "
                        f"{row.channel_count} channels, "
                        f"{timestamp(row.created_at)}"
                        for label, row in stored[:25]
                    )
                    or "No backups stored for this server yet. Run "
                    "`/backup_create name:...`."
                ),
            )
            if stored:
                embed.set_footer(text="Load one with `/backup_load name:...`")
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        label = sanitize_name(name)
        row = next((r for lbl, r in stored if lbl == label), None)
        if row is None:
            raise ZagrosError(
                f"There is no backup called `{label}`. Run `/backup_load` with no "
                "name to see what exists."
            )

        if not guild.me.guild_permissions.manage_roles:
            raise PermissionDeniedError(
                "I need **Manage Roles** to restore role names and colours."
            )

        path = backup_path(directory, guild.id, label)
        try:
            envelope = row.payload or path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ZagrosError(
                f"I could not read the backup file: {exc.strerror or exc}."
            ) from exc

        try:
            payload = decode_payload(envelope, self.config.bot_token)
        except ValueError as exc:
            raise ZagrosError(
                f"That backup could not be opened: {exc}. It was truncated, "
                "edited, or sealed with a different bot token."
            ) from exc

        summary = summarize_backup(payload)
        if summary.get("guild_id") not in (None, guild.id):
            raise ZagrosError(
                "That backup belongs to a different server. I will not apply it here."
            )

        # Confirmation is not decoration here: the very next call renames the
        # server and its roles. So the decision moves onto a button, is scoped to
        # the moderator who asked, and expires on its own. The view applies the
        # restore and edits its own message, so this coroutine only waits.
        view = RestoreConfirmView(self, guild, label, envelope, interaction.user.id)
        await interaction.followup.send(
            view=view,
            embed=warning_embed(
                f"Restore **{summary['guild_name']}** — {summary['roles']} role(s), "
                f"{summary['channels']} channel(s)?\n\n"
                f"This renames **{guild.name}** and its roles and repositions roles "
                "that already exist. It does **not** create or delete channels, and "
                "does **not** change any permission.",
                title="Restore needs confirmation",
            ),
            ephemeral=True,
        )
        if not await view.wait():
            await interaction.followup.send(
                "Cancelled or timed out — nothing in this server was changed.",
                ephemeral=True,
            )

    async def _apply_restore(
        self,
        guild: discord.Guild,
        label: str,
        envelope: str,
        moderator: discord.Member,
    ) -> discord.Embed:
        """Run the restore and build the embed describing what actually changed."""
        try:
            payload = decode_payload(envelope, self.config.bot_token)
        except ValueError as exc:
            return warning_embed(
                f"`{label}` could not be opened: {exc}. It was truncated, edited, "
                "or sealed with a different bot token.",
                title="Backup unreadable",
            )

        try:
            changed = await restore_guild(
                guild,
                payload,
                reason=build_audit_reason(
                    None, moderator, f"Restore backup {label}"
                ),
            )
        except discord.Forbidden as exc:
            logger.warning("Restore forbidden in %s: %s", guild.id, exc)
            return warning_embed(
                "Discord refused the restore. I need **Manage Roles** and "
                "**Manage Server** here.",
                title="Restore refused",
            )
        except discord.HTTPException as exc:
            logger.warning("Restore HTTP error in %s: %s", guild.id, exc)
            return warning_embed(
                f"Discord returned `{exc.status}` mid-restore. Some changes may "
                "have landed before it gave up.",
                title="Restore interrupted",
            )

        colour = COLOR_WARNING if changed["skipped"] else COLOR_SUCCESS
        embed = base_embed(
            title=f"Restored {label}",
            colour=colour,
            author=moderator,
        )
        embed.add_field(
            name="Server profile",
            value=f"**{changed['guild']}** change(s)",
            inline=True,
        )
        embed.add_field(
            name="Roles",
            value=f"**{changed['roles']}** name/colour/position change(s)",
            inline=True,
        )
        embed.add_field(
            name="Skipped",
            value=(
                f"**{changed['skipped']}** item(s) Discord refused"
                if changed["skipped"]
                else "**0** — everything applied"
            ),
            inline=True,
        )
        embed.add_field(
            name="Never touched",
            value=(
                "Channel list, permission overrides and role permissions are "
                "deliberately not restored. A snapshot is a record of how the "
                "server looked, not a grant of authority."
            ),
            inline=False,
        )
        logger.info(
            "Backup %s/%s restored by %s: %s", guild.id, label, moderator.id, changed,
        )
        return embed

    # ------------------------------------------------------------------ #
    # Native AutoMod listener
    # ------------------------------------------------------------------ #
    @commands.Cog.listener()
    async def on_automod_action(
        self, action: discord.AutoModActionExecution
    ) -> None:
        """Record and announce every block Discord's AutoMod performed.

        The event carries the rule *id*, not the matched keyword, so the keyword
        is resolved against the saved phrase list. A mismatch is not fatal — the
        block already happened, and losing the reason in the log would be worse
        than a slightly generic line.
        """
        if not action.guild or not self.bot.user or action.user_id == self.bot.user.id:
            return

        rule = action.rule
        phrases = await list_filters(action.guild.id)
        keyword = _matched_keyword(rule.trigger.keyword_filter or [], phrases)
        label = f"{rule.name}" if rule is not None else "an AutoMod rule"

        runtime_state.record_automod(
            guild_id=action.guild.id,
            user_id=action.user_id,
            rule_id=rule.id if rule is not None else None,
            keyword=keyword,
            action="block",
            channel_id=action.channel_id,
        )

        member = action.guild.get_member(action.user_id)
        who = describe_member(member) if member is not None else f"user {action.user_id}"
        embed = base_embed(
            title="AutoMod blocked a message",
            colour=COLOR_WARNING,
            author=member,
        )
        embed.add_field(name="Member", value=truncate(who, 200), inline=True)
        embed.add_field(
            name="Channel",
            value=describe_channel(action.channel) or f"channel {action.channel_id}",
            inline=True,
        )
        embed.add_field(
            name="Rule", value=truncate(label, 100), inline=True
        )
        embed.add_field(
            name="Matched",
            value=f"`{truncate(keyword, 200)}`" if keyword else "**unknown keyword**",
            inline=False,
        )
        if action.content:
            embed.add_field(
                name="Content that was blocked",
                value=truncate(action.content, 1000),
                inline=False,
            )
        await log_action(action.guild, embed)
        logger.info(
            "AutoMod rule %s blocked %s in %s/%s",
            rule.id if rule is not None else "?",
            action.user_id, action.guild.id, action.channel_id,
        )

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    @command_context("management:bot_section")
    async def _append_bot_section(
        self, guild: discord.Guild, embed: discord.Embed
    ) -> None:
        """Append the "how is the bot doing here" block to /serverinfo."""
        me = guild.me
        granted, missing = _build_perm_reports(me)
        log_channel = await resolve_log_channel(guild)
        config = await get_or_create_guild_config(guild.id)
        mute_role = (
            guild.get_role(config.muted_role_id)
            if config and config.muted_role_id
            else None
        )

        embed.add_field(
            name=f"{BRAND_NAME} in this server",
            value=(
                f"Latency **{round(self.bot.latency * 1000)} ms** · joined "
                f"<t:{int(me.joined_at.timestamp())}:R>\n"
                f"Log channel: "
                + (describe_channel(log_channel) if log_channel else "**not set**")
                + "\nMute role: "
                + (describe_role(mute_role) if mute_role is not None else "**not set**")
            ),
            inline=False,
        )
        embed.add_field(
            name="Granted to me", value=_render_names(granted), inline=True
        )
        embed.add_field(
            name="Missing from me",
            value=_render_names(missing) or "**nothing — fully operational**",
            inline=True,
        )

        if missing:
            embed.colour = 0xB26A00
            embed.set_footer(
                text=(
                    "Some moderation commands will refuse to run until these are "
                    "granted."
                )
            )

    @command_context("management:guard")
    async def _require_manager(
        self,
        interaction: discord.Interaction,
        *,
        require: tuple[str, ...] | None = None,
    ) -> None:
        """Only users who can actually configure the bot may do so."""
        if not await has_moderation_access(interaction, require):
            raise PermissionDeniedError(
                "You need **Manage Server**, a moderator role, or "
                f"{' or '.join(f'**{p}**' for p in require)} to change this "
                "server's configuration."
                if require
                else "You need **Manage Server** or a moderator role to change "
                "this server's configuration."
            )

    @staticmethod
    def _assert_can_log(me: discord.Member, channel: discord.TextChannel) -> None:
        perms = me.permissions_in(channel)
        missing = [
            name
            for name, ok in (
                ("View Channel", perms.view_channel),
                ("Send Messages", perms.send_messages),
                ("Embed Links", perms.embed_links),
                ("Read Message History", perms.read_message_history),
            )
            if not ok
        ]
        if missing:
            raise PermissionDeniedError(
                f"I cannot write to {describe_channel(channel)} — I am missing "
                f"**{'**, **'.join(missing)}** there.\n\nOverride the channel "
                "permissions, or re-invite me with the scopes this channel needs."
            )

    async def _persist(self, guild_id: int, **fields: object) -> GuildConfig | None:
        """Write the guild config row, creating it on first use.

        Returns ``None`` when the database refused the write, so callers can
        report a clean failure instead of pretending the change landed.
        """
        try:
            async with get_database().session() as session:
                config = await session.get(GuildConfig, guild_id)
                if config is None:
                    config = GuildConfig(guild_id=guild_id)
                    session.add(config)
                for key, value in fields.items():
                    setattr(config, key, value)
            return config
        except (SQLAlchemyError, RuntimeError) as exc:
            logger.error("Config write failed for guild %s: %s", guild_id, exc)
            return None

    @staticmethod
    def _backup_dir() -> Path:
        """Where sealed backup files live, relative to the project root."""
        return PROJECT_ROOT / "data" / "backups"

    async def _list_backups(
        self, guild_id: int, directory: Path
    ) -> list[tuple[str, GuildBackup]]:
        """Backups for one guild, newest first.

        Files on disk and rows in the table are merged: the row is the fast path,
        the file is the backup of record, so a row missing from one source still
        shows up in ``/backup_load``'s list.
        """
        rows: dict[str, GuildBackup] = {}
        try:
            async with get_database().session() as session:
                result = await session.execute(
                    select(GuildBackup)
                    .where(GuildBackup.guild_id == guild_id)
                    .order_by(GuildBackup.created_at.desc())
                )
                for row in result.scalars().all():
                    rows[row.name] = row
        except (SQLAlchemyError, RuntimeError) as exc:
            logger.warning("Could not index backups for guild %s: %s", guild_id, exc)

        indexed: list[tuple[str, GuildBackup]] = list(rows.items())
        for name in list_backup_names(directory, guild_id):
            if name not in rows:
                indexed.append((name, _orphan_backup(guild_id, name)))
        indexed.sort(key=lambda item: item[0])
        indexed.reverse()
        return indexed

    async def _record_backup(
        self,
        guild_id: int,
        name: str,
        payload: str,
        summary: dict[str, Any],
        created_by: int,
    ) -> GuildBackup:
        """Index a written backup file, replacing any earlier one of that name."""
        async with get_database().session() as session:
            row = await session.scalar(
                select(GuildBackup).where(
                    GuildBackup.guild_id == guild_id,
                    GuildBackup.name == name,
                )
            )
            if row is None:
                row = GuildBackup(guild_id=guild_id, name=name)
                session.add(row)
            row.payload = payload
            row.channel_count = int(summary["channels"])
            row.role_count = int(summary["roles"])
            row.created_by = created_by
        return row


def _orphan_backup(guild_id: int, name: str) -> GuildBackup:
    """A placeholder for a file on disk that has no index row.

    The payload stays empty on purpose: the caller falls back to reading the
    file, and inventing text here would risk decoding the wrong thing.
    """
    return GuildBackup(
        guild_id=guild_id,
        name=name,
        payload="",
        role_count=0,
        channel_count=0,
    )


def _matched_keyword(
    live_keywords: list[str], saved_phrases: list[str]
) -> str:
    """Best-effort guess at which phrase tripped AutoMod.

    ``AutoModActionExecution`` does not tell us the matched keyword, so the saved
    phrase list is the only local hint. The saved list wins when the two agree;
    otherwise the rule's own keywords are reported, because that is at least
    factually what Discord is enforcing.
    """
    folded = {p.casefold() for p in saved_phrases}
    for keyword in live_keywords:
        if keyword.casefold() in folded:
            return keyword
    return live_keywords[0] if live_keywords else ""


class EmbedDraftModal(discord.ui.Modal, title="Compose an embed"):
    """The ``/embed_builder`` form.

    Command arguments are pre-filled so the moderator types only what they want
    to change — re-entering a long description to fix a typo is the fastest way
    to make someone abandon the tool.
    """

    body: discord.ui.TextInput = discord.ui.TextInput(
        label="Description",
        style=discord.TextStyle.long,
        placeholder="The text under the headline.",
        required=False,
        max_length=4000,
    )
    colour: discord.ui.TextInput = discord.ui.TextInput(
        label="Accent colour",
        style=discord.TextStyle.short,
        placeholder="#7C5CFF",
        required=False,
        max_length=9,
    )
    image: discord.ui.TextInput = discord.ui.TextInput(
        label="Image URL",
        style=discord.TextStyle.short,
        placeholder="https://…",
        required=False,
        max_length=500,
    )
    footer: discord.ui.TextInput = discord.ui.TextInput(
        label="Footer",
        style=discord.TextStyle.short,
        required=False,
        max_length=2048,
    )

    def __init__(
        self,
        *,
        title: str,
        body: str = "",
        colour: str = "",
        image: str = "",
        footer: str = "",
    ) -> None:
        super().__init__()
        self.embed_title = title
        self.body.default = body
        self.colour.default = colour
        self.image.default = image
        self.footer.default = footer

    @property
    def title(self) -> str:  # type: ignore[override]
        """The embed headline, set from the command rather than the modal."""
        return self.embed_title


class RestoreConfirmView(discord.ui.View):
    """Yes/no gate in front of :func:`core.backup.restore_guild`.

    The view owns the decision, not the command coroutine, so the command can
    simply wait on it. Only the moderator who ran ``/backup_load`` may answer;
    anyone else clicking gets an ephemeral nudge instead of a silent no-op that
    looks like a broken button.
    """

    def __init__(
        self,
        cog: Management,
        guild: discord.Guild,
        label: str,
        payload: str,
        requester_id: int,
        *,
        timeout: float = RESTORE_CONFIRM_SECONDS,
    ) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.guild = guild
        self.label = label
        self.payload = payload
        self.requester_id = requester_id
        self.decision: bool | None = None
        self._settled: discord.Event = discord.Event()

    async def wait(self, seconds: float | None = None) -> bool:
        """Block until a decision lands. ``False`` on timeout or cancellation."""
        try:
            async with asyncio.timeout(seconds or self.timeout):
                await self._settled.wait()
        except (TimeoutError, asyncio.CancelledError):
            self.decision = False
        finally:
            self.stop()
        return bool(self.decision)

    def _settle(self, value: bool) -> None:
        self.decision = value
        self._settled.set()

    @discord.ui.button(label="Restore", style=discord.ButtonStyle.danger)
    async def _confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the moderator who ran this command can answer that.",
                ephemeral=True,
            )
            return
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        self.stop()
        result = await self.cog._apply_restore(
            self.guild, self.label, self.payload, interaction.user
        )
        await interaction.response.edit_message(embed=result, view=self)
        self._settle(True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def _cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the moderator who ran this command can answer that.",
                ephemeral=True,
            )
            return
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(
            embed=warning_embed(
                "Cancelled. Nothing in this server was changed.", title="Restore cancelled"
            ),
            view=self,
        )
        self.stop()
        self._settle(False)

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        self._settle(False)


async def setup(bot: commands.Bot) -> None:
    """Extension entrypoint."""
    await bot.add_cog(Management(bot))
    logger.info("Management cog loaded")
