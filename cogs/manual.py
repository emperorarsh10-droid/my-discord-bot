"""Commands that are mostly about the bot itself: ``/modhelp``, ``/automod``,
``/setup``, ``/topic`` and ``/embed``.

``/automod`` is the one that matters operationally. It owns the persistence side
of :mod:`core.automod` — turning the master switch, tuning thresholds and picking
a penalty per rule — while ``core.automod`` owns the per-message hot path. The
split is deliberate: the listener must never wait on a moderator's settings write,
and a settings command must never be on the message path.

**Every listener-side failure is contained.** ``on_message`` is the busiest
callback in the bot; an exception there would be logged by discord.py and
otherwise vanish, leaving a guild believing it is protected when it is not.
"""

from __future__ import annotations

import contextlib
from typing import Final, Literal

import discord
from discord import app_commands
from discord.ext import commands

from cogs._base import FeatureCog
from core.automod import AutoMod
from core.database import get_database
from core.embeds import (
    COLOR_INFO,
    COLOR_SUCCESS,
    base_embed,
    success_embed,
    truncate,
    warning_embed,
)
from core.errors import ZagrosError
from core.logging_setup import get_logger
from core.models import CaseAction, GuildConfig, utcnow
from core.services import (
    build_audit_reason,
    get_or_create_guild_config,
    log_action,
    record_case,
)

logger = get_logger("zagrosian.cog.manual")

#: Penalties a rule may be set to. ``none`` measures without acting, which is the
#: honest way to tune a threshold on a live server.
PENALTIES: Final[tuple[str, ...]] = ("none", "delete", "warn", "timeout", "kick")

automod_group = app_commands.Group(
    name="automod",
    description="Turn filtering on, tune thresholds, choose penalties.",
)


class Manual(FeatureCog):
    """Bot-facing commands: help, setup, AutoMod control, topics and embeds."""

    feature_name = "manual"

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__(bot)
        self.automod = AutoMod(bot)

    def cog_unload(self) -> None:
        self.bot.tree.remove_command("automod")

    # ------------------------------------------------------------------ #
    # /modhelp
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="modhelp",
        description="A short guide to the moderation commands, grouped by job.",
    )
    @app_commands.guild_only
    async def modhelp(self, interaction: discord.Interaction) -> None:
        await self.begin(interaction)
        await self.authorise(interaction)

        sections = (
            (
                "Removing people",
                (
                    "`/ban` `/unban` `/softban` `/kick` `/banlist`\n"
                    "`/massban` — ban many ids from a pasted list"
                ),
            ),
            (
                "Silencing",
                (
                    "`/timeout` `/untimeout` — Discord's own communication timeout\n"
                    "`/mute` `/unmute` — the server's mute role\n"
                    "`/vcmute` `/vcunmute` — voice, via the same role"
                ),
            ),
            (
                "Messages",
                (
                    "`/purge` — bulk delete recent messages\n"
                    "`/slowmode` `/slowmodeall` `/lockdown` `/unlock`\n"
                    "`/filter` — blocked phrases, `/clean invites` for links"
                ),
            ),
            (
                "Recording",
                (
                    "`/warn` `/warns` `/clearwarns` `/reason` `/notes`\n"
                    "`/cases` `/viewcase` `/infractions` `/modstats`\n"
                    "`/dm` — message a member on the server's behalf"
                ),
            ),
            (
                "Roles and members",
                (
                    "`/role` `/strip` `/roleall` `/temprole` `/nick`\n"
                    "`/whois` `/avatar` `/altcheck` `/verify`\n"
                    "`/ignore` `/unignore` — exempt from filtering"
                ),
            ),
            (
                "Emergencies",
                (
                    "`/panic` `/unpanic` `/lockdownall` `/unlockall`\n"
                    "`/nuke` — rebuild a channel, restoring permissions\n"
                    "`/automod status` — what filtering is doing right now"
                ),
            ),
            (
                "Setting up",
                (
                    "`/setup` — guided first-time configuration\n"
                    "`/config logs` — where moderation logs go\n"
                    "`/config muted-role` — the role `/mute` grants"
                ),
            ),
        )

        embed = base_embed(
            title="Moderation guide",
            author=interaction.user,
            colour=COLOR_INFO,
            description=(
                "Everything below takes a reason and writes it to the moderation "
                "log with your name on it. Roles are enforced by hierarchy: if "
                "someone is above you or above me, the command will tell you so "
                "rather than fail silently."
            ),
        )
        for title, body in sections:
            embed.add_field(name=title, value=truncate(body, 1024), inline=False)

        guild = interaction.guild
        me = guild.me
        missing = [
            name.replace("_", " ").title()
            for name in (
                "ban_members",
                "kick_members",
                "moderate_members",
                "manage_roles",
                "manage_messages",
                "manage_nicknames",
                "move_members",
            )
            if not getattr(me.guild_permissions, name, False)
        ]
        if missing:
            embed.set_footer(
                text="Missing from my own role: " + ", ".join(missing)
                + " — some commands will refuse to run."
            )
        await self.reply(interaction, embed)

    # ------------------------------------------------------------------ #
    # /setup
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="setup",
        description="One-time guided setup: check what is missing and fix it in order.",
    )
    @app_commands.guild_only
    async def setup(self, interaction: discord.Interaction) -> None:
        await self.begin(interaction)
        await self.authorise(interaction, required=("manage_guild",))

        guild = interaction.guild
        me = guild.me
        config = await get_or_create_guild_config(guild.id)

        steps: list[tuple[bool, str, str]] = []

        for name, why in (
            ("administrator", "lets me act anywhere; prefer a narrower set below"),
            ("ban_members", "needed for /ban and /massban"),
            ("kick_members", "needed for /kick and /vckick"),
            ("moderate_members", "needed for /timeout"),
            ("manage_roles", "needed for /role, /strip, /mute and /temprole"),
            ("manage_messages", "needed for /purge and /slowmode"),
            ("manage_nicknames", "needed for /nick"),
            ("move_members", "needed for /vcmute and voice moves"),
            ("manage_channels", "needed for /lockdown, /nuke and /topic"),
        ):
            ok = bool(getattr(me.guild_permissions, name, False))
            steps.append((ok, name.replace("_", " ").title(), why))

        muted_role = None
        if config is not None and config.muted_role_id:
            muted_role = guild.get_role(int(config.muted_role_id))
        steps.append(
            (
                muted_role is not None,
                "Mute role configured",
                "run `/config muted-role` to pick or create it",
            )
        )
        steps.append(
            (
                bool(config and config.log_channel_id),
                "Log channel configured",
                "run `/config logs` to choose one",
            )
        )
        steps.append(
            (
                bool(config and config.automod_enabled),
                "AutoMod enabled",
                "run `/automod enable` when the roles are sorted",
            )
        )

        done = sum(1 for ok, _, _ in steps if ok)
        embed = base_embed(
            title=f"Setup · {done}/{len(steps)} ready",
            author=interaction.user,
            colour=COLOR_SUCCESS if done == len(steps) else COLOR_INFO,
            description=(
                "Work down this list in order — each step depends on the ones above "
                "it. Move my role above your moderators first: Discord enforces role "
                "order, and I cannot touch anyone my role is below."
            ),
        )
        for ok, label, why in steps:
            embed.add_field(
                name=f"{'Done' if ok else 'Todo'} — {label}",
                value=f"`{why}`",
                inline=False,
            )

        # A sanity check that costs one API call and catches the single most
        # common deployment mistake: Message Content intent off means every
        # message filter silently sees an empty string.
        if not me.guild_permissions.manage_messages:
            embed.set_footer(
                text="Start with Manage Messages — without it, message filtering "
                "and /purge cannot work."
            )
        await self.reply(interaction, embed)

    # ------------------------------------------------------------------ #
    # /topic
    # ------------------------------------------------------------------ #
    @app_commands.command(name="topic", description="Show or set a channel's topic.")
    @app_commands.guild_only
    @app_commands.describe(
        channel="Which channel. Defaults to this one.",
        text="The new topic. Omit to view the current one.",
    )
    async def topic(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
        text: app_commands.Range[str, 1, 1024] | None = None,
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction)

        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            raise ZagrosError("Topics only exist on text channels.")

        if text is None:
            current = target.topic or "*No topic set.*"
            await self.reply(
                interaction,
                base_embed(
                    title=f"Topic · {target.name}",
                    author=interaction.user,
                    description=truncate(current, 4000),
                ),
            )
            return

        await self.authorise(interaction, required=("manage_channels",))
        self.assert_bot_permissions(
            interaction.guild.me, target, ("manage_channels",), verb="change the topic"
        )
        before = target.topic or "*none*"
        try:
            await target.edit(
                topic=text,
                reason=build_audit_reason(None, interaction.user, "Channel topic"),
            )
        except discord.Forbidden as exc:
            raise ZagrosError(
                f"Discord refused the topic change: {exc}"
            ) from exc

        await self.reply(
            interaction,
            success_embed(
                f"**{target.mention}**\nBefore: {truncate(before, 300)}\n"
                f"After: {truncate(text, 300)}",
                title="Topic updated",
                author=interaction.user,
            ),
        )

    # ------------------------------------------------------------------ #
    # /embed
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="embed", description="Send a rich embed with optional fields and a footer."
    )
    @app_commands.guild_only
    @app_commands.describe(
        title="The embed's headline.",
        description="The body text.",
        color="Hex colour, e.g. #7C5CFF. Optional.",
        footer="Small text along the bottom.",
        image="An image URL to show.",
        channel="Where to send it. Defaults to this channel.",
    )
    async def embed(
        self,
        interaction: discord.Interaction,
        title: app_commands.Range[str, 1, 256],
        description: app_commands.Range[str, 1, 4000] | None = None,
        color: app_commands.Range[str, 1, 9] | None = None,
        footer: app_commands.Range[str, 1, 2048] | None = None,
        image: app_commands.Range[str, 1, 500] | None = None,
        channel: discord.TextChannel | None = None,
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction, required=("manage_channels",))

        from core.embeds import parse_hex_color

        if color is not None:
            parsed = parse_hex_color(color)
            if parsed is None:
                raise ZagrosError(
                    f"`{truncate(color, 20)}` is not a hex colour. Use six digits - "
                    "`#7C5CFF`, `7C5CFF` or `0x7C5CFF`."
                )
        else:
            parsed = None

        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            raise ZagrosError("Embeds can only be sent to a text channel.")

        payload = base_embed(
            title=title,
            description=description,
            colour=parsed if parsed is not None else COLOR_INFO,
            footer=footer,
        )
        if image:
            if not image.lower().startswith(("http://", "https://")):
                raise ZagrosError("The image must be an http(s) URL.")
            payload.set_image(url=image)

        try:
            sent = await target.send(embed=payload)
        except discord.Forbidden as exc:
            raise ZagrosError(f"Discord refused to send there: {exc}") from exc

        await self.reply(
            interaction,
            success_embed(
                f"Posted in {target.mention}.", title="Embed sent", author=interaction.user
            ),
        )
        logger.info("Embed sent by %s to %s", interaction.user.id, target.id)
        _ = sent

    # ------------------------------------------------------------------ #
    # /automod
    # ------------------------------------------------------------------ #
    @automod_group.command(name="status", description="Show what AutoMod is configured to do.")
    @app_commands.guild_only
    async def automod_status(self, interaction: discord.Interaction) -> None:
        await self.begin(interaction)
        await self.authorise(interaction)

        config = await get_or_create_guild_config(interaction.guild_id)
        resolved = await self.automod.settings_for(interaction.guild_id, force=True)

        embed = base_embed(
            title="AutoMod status",
            author=interaction.user,
            colour=COLOR_SUCCESS if resolved.enabled else COLOR_INFO,
            description=(
                f"**Filtering is {'on' if resolved.enabled else 'off'}.**\n"
                + (
                    ""
                    if resolved.enabled
                    else "Nothing is being filtered. Run `/automod enable` when ready."
                )
            ),
        )
        embed.add_field(
            name="Rules",
            value="\n".join(
                [
                    f"Duplicate flood → `{resolved.duplicate_action}`",
                    f"Caps abuse → `{resolved.caps_action}` "
                    f"({resolved.caps_percent}% over {resolved.caps_min_length} letters)",
                    f"Invite links → `{resolved.invite_action}`",
                    f"Spam → timeout {resolved.spam_timeout_seconds}s "
                    f"({resolved.spam_max_messages} in {resolved.spam_window_seconds}s)",
                ]
            ),
            inline=False,
        )
        ignores = (
            len(resolved.ignored_channels),
            len(resolved.ignored_roles),
            len(resolved.ignored_users),
        )
        embed.add_field(
            name="Exemptions",
            value=(
                f"{ignores[0]} channel(s), {ignores[1]} role(s), {ignores[2]} member(s).\n"
                "Managed with `/ignore` and `/unignore`."
            ),
            inline=False,
        )
        await self.reply(interaction, embed)
        _ = config

    @automod_group.command(
        name="enable", description="Turn AutoMod on, optionally with a whole preset."
    )
    @app_commands.guild_only
    @app_commands.describe(
        preset="'off', 'warn' (log only), 'delete', or 'strict' (delete, timeout, kick)."
    )
    async def automod_enable(
        self,
        interaction: discord.Interaction,
        preset: Literal["off", "warn", "delete", "strict"] = "delete",
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction)

        actions = {
            "off": ("none", "none", "none"),
            "warn": ("warn", "warn", "warn"),
            "delete": ("delete", "delete", "delete"),
            "strict": ("delete", "timeout", "kick"),
        }
        duplicate, caps, invite = actions[preset]
        enabled = preset != "off"

        await self._write(
            interaction,
            automod_enabled=enabled,
            automod_duplicate_action=duplicate,
            automod_caps_action=caps,
            automod_invite_action=invite,
        )
        await self.reply(
            interaction,
            success_embed(
                f"AutoMod is **{'on' if enabled else 'off'}** using the `{preset}` "
                "preset.\n"
                f"Duplicates → `{duplicate}` · caps → `{caps}` · invites → `{invite}`",
                title="AutoMod configured",
                author=interaction.user,
            ),
        )

    @automod_group.command(name="caps", description="Set the anti-caps threshold.")
    @app_commands.guild_only
    @app_commands.describe(
        percent="Percent of capital letters that counts as shouting. 0 disables.",
        min_length="Ignore messages with fewer letters than this.",
    )
    async def automod_caps(
        self,
        interaction: discord.Interaction,
        percent: app_commands.Range[int, 0, 100],
        min_length: app_commands.Range[int, 4, 100] = 12,
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction)

        await self._write(
            interaction,
            anticaps_percent=percent,
            anticaps_min_length=min_length,
        )
        await self.reply(
            interaction,
            success_embed(
                f"Messages with **{percent}% or more** capitals across at least "
                f"**{min_length}** letters are now treated as shouting."
                if percent
                else "Anti-caps is now disabled (`percent: 0`).",
                title="Anti-caps updated",
                author=interaction.user,
            ),
        )

    @automod_group.command(name="spam", description="Set the message-rate timeout.")
    @app_commands.guild_only
    @app_commands.describe(
        window="How many seconds to watch.",
        max_messages="How many messages in that window trips it.",
        timeout_seconds="How long the automatic timeout lasts.",
    )
    async def automod_spam(
        self,
        interaction: discord.Interaction,
        window: app_commands.Range[int, 2, 120],
        max_messages: app_commands.Range[int, 2, 30],
        timeout_seconds: app_commands.Range[int, 5, 2419200] = 60,
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction)

        if max_messages > 25:
            raise ZagrosError(
                "25 messages per window is the practical ceiling — beyond that, "
                "legitimate fast conversations trip it."
            )

        await self._write(
            interaction,
            spam_window_seconds=window,
            spam_max_messages=max_messages,
            spam_timeout_seconds=timeout_seconds,
        )
        await self.reply(
            interaction,
            success_embed(
                f"**{max_messages}** messages within **{window}s** now triggers a "
                f"{timeout_seconds}s timeout.",
                title="Spam filter updated",
                author=interaction.user,
            ),
        )

    @automod_group.command(name="reset", description="Invalidate cached AutoMod settings.")
    @app_commands.guild_only
    async def automod_reset(self, interaction: discord.Interaction) -> None:
        await self.begin(interaction)
        await self.authorise(interaction)

        self.automod.invalidate(interaction.guild_id)
        await self.automod.settings_for(interaction.guild_id, force=True)
        await self.reply(
            interaction,
            success_embed(
                "Cached settings dropped; the next message re-reads them from the "
                "database.",
                title="AutoMod cache cleared",
                author=interaction.user,
            ),
        )

    async def _write(self, interaction: discord.Interaction, **fields) -> None:
        """Persist AutoMod columns and invalidate the cache."""
        db = get_database()
        async with db.session() as session:
            config = await session.get(GuildConfig, interaction.guild_id)
            if config is None:
                config = GuildConfig(guild_id=interaction.guild_id)
                session.add(config)
            for key, value in fields.items():
                setattr(config, key, value)
            await session.commit()
        # Drop the cached copy so the next message sees the new values rather than
        # waiting out the TTL.
        self.automod.invalidate(interaction.guild_id)

    # ------------------------------------------------------------------ #
    # Listener: enforcement
    # ------------------------------------------------------------------ #
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Apply the configured penalties. Never raises."""
        try:
            await self._enforce(message)
        except discord.HTTPException as exc:
            logger.info("AutoMod action failed in %s: %s", message.guild.id, exc)
        except Exception as exc:  # noqa: BLE001 - a listener must never propagate
            logger.error(
                "AutoMod error in guild %s channel %s: %s",
                message.guild.id,
                message.channel.id,
                exc,
            )

    async def _ladder_rung(self, guild, member, ladder, verdict) -> None:
        """Apply the warn escalation rung the guild configured.

        Counts only this member's active AutoMod warns, so a kick issued for
        something else never pushes them over a threshold they did not reach here.
        """
        from datetime import timedelta

        from sqlalchemy import func, select

        from core.models import ModCase

        db = get_database()
        async with db.session() as session:
            count = (
                await session.execute(
                    select(func.count())
                    .select_from(ModCase)
                    .where(
                        ModCase.guild_id == guild.id,
                        ModCase.user_id == member.id,
                        ModCase.action == CaseAction.AUTOMOD,
                        ModCase.is_active.is_(True),
                    )
                )
            ).scalar_one()

        if count < (ladder.warn_timeout_at or 0):
            return

        if ladder.warn_kick_at and count >= ladder.warn_kick_at:
            with contextlib.suppress(discord.Forbidden):
                await member.kick(reason=build_audit_reason(None, guild.me, "AutoMod ladder"))
            return

        if ladder.warn_timeout_minutes:
            self.assert_bot_permissions(
                guild.me, None, ("moderate_members",), verb="timeout"
            )
            until = utcnow() + timedelta(minutes=ladder.warn_timeout_minutes)
            with contextlib.suppress(discord.Forbidden):
                await member.timeout(
                    until, reason=build_audit_reason(None, guild.me, "AutoMod ladder")
                )

    async def _enforce(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return

        verdict, settings = await self.automod.inspect(message)
        if not verdict.tripped or verdict.action == "none":
            return

        guild = message.guild
        member = message.author
        action = verdict.action

        if action == "delete" and message.guild.me.guild_permissions.manage_messages:
            with contextlib.suppress(discord.NotFound, discord.Forbidden):
                await message.delete()

        case_action = CaseAction.AUTOMOD
        await record_case(
            guild_id=guild.id,
            user_id=member.id,
            moderator_id=guild.me.id,
            action=case_action,
            reason=f"{verdict.reason}: {verdict.detail}",
        )

        if action == "warn":
            # The warn ladder lives on the config, not in memory: it has to survive
            # a redeploy, which a dict on the bot would not.
            ladder = await get_or_create_guild_config(guild.id)
            if ladder is not None and ladder.warn_timeout_at:
                await self._ladder_rung(guild, member, ladder, verdict)
        elif action == "timeout":
            from datetime import timedelta

            self.assert_bot_permissions(
                guild.me, message.channel, ("moderate_members",), verb="timeout"
            )
            until = utcnow() + timedelta(seconds=settings.spam_timeout_seconds)
            with contextlib.suppress(discord.Forbidden):
                await member.timeout(
                    until, reason=build_audit_reason(None, guild.me, "AutoMod spam")
                )
        elif action == "kick":
            self.assert_hierarchy(guild.me, member, guild.me, verb="kick")
            with contextlib.suppress(discord.Forbidden):
                await member.kick(reason=build_audit_reason(None, guild.me, "AutoMod"))

        await log_action(
            guild,
            warning_embed(
                f"**{member}** (`{member.id}`) — {verdict.reason}\n"
                f"{verdict.detail}\nAction: `{action}`\n"
                f"Message: {truncate(message.content, 300) or '*(no text)*'}",
                title="AutoMod",
                author=guild.me,
            ),
        )


async def setup(bot: commands.Bot) -> None:
    """Extension entrypoint."""
    await bot.add_cog(Manual(bot))
    # The group is a module-level object because the decorators attach to it at
    # import time; discord.py only auto-injects groups declared as class
    # attributes, so this one is grafted onto the tree explicitly.
    bot.tree.add_command(automod_group)
    logger.info("Manual cog loaded")
