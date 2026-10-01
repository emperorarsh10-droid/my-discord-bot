"""Moderation cog — bans, kicks, mutes, warnings and message purges.

Every command follows the same five-beat contract, so behaviour is uniform and
auditable:

1.  **Defer** immediately. Discord voids an interaction after three seconds; a
    ban that needs several API round trips must not wait for its first response.
2.  **Authorise and validate** the target against the role hierarchy *before*
    touching anything.
3.  **Allocate the case number first**, so the audit-log reason string, the log
    embed and the DM all reference the same identifier.
4.  **Act**, then record the case. A ledger failure is reported to the
    moderator but never rolls back an action that already happened.
5.  **Report** to the moderator, the log channel and the member's DMs — each
    step individually guarded so one dead channel cannot break the others.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from typing import Final, Literal

import discord
from discord import app_commands
from discord.ext import commands, tasks
from sqlalchemy import select

from config import get_settings
from core.database import get_database
from core.embeds import (
    COLOR_SUCCESS,
    COLOR_WARNING,
    base_embed,
    describe_channel,
    describe_member,
    describe_role,
    format_duration,
    success_embed,
    timestamp,
    truncate,
    warning_embed,
)
from core.errors import (
    HierarchyError,
    PermissionDeniedError,
    ZagrosError,
)
from core.logging_setup import command_context, get_logger
from core.models import CaseAction, ModCase
from core.ratelimit import RateLimit
from core.services import (
    build_audit_reason,
    dm_notice_embed,
    ensure_mute_role,
    expire_stale_bans,
    expire_stale_mutes,
    fetch_active_cases,
    get_or_create_guild_config,
    has_moderation_access,
    log_action,
    notify_member,
    punishment_embed,
    record_case,
    utc_expiry,
)
from core.targets import parse_user_ids
from core.transformers import Duration

logger = get_logger("zagrosian.cog.moderation")

def _is_owner(user_id: object) -> bool:
    """Return whether ``user_id`` is a configured bot owner.

    Settings resolve lazily on every call, so importing this module never
    requires a readable ``.env`` and a later env change is picked up.
    """
    return user_id in get_settings().owner_ids


_OWNER_EXEMPT: Final[Callable[[object], bool]] = _is_owner

PUNISHMENT_LIMIT: Final[RateLimit] = RateLimit(
    max_calls=5, window=30.0, exempt=_OWNER_EXEMPT
)
PURGE_LIMIT: Final[RateLimit] = RateLimit(
    max_calls=3, window=30.0, exempt=_OWNER_EXEMPT
)

#: Discord accepts 0-7 days of history deletion on ban.
MAX_DELETE_DAYS: Final[int] = 7
#: How many bulk-delete requests a single /clear may issue before giving up.
MAX_PURGE_REQUESTS: Final[int] = 3
#: In-flight bans for one /massban. Ten parallel REST calls is already enough
#: to stall unrelated commands through the shared global bucket.
MAX_MASSBAN_CONCURRENCY: Final[int] = 5

NO_REASON: Final[str] = "No reason provided"


def _purge_ceiling() -> int:
    """Read ``MAX_PURGE_AMOUNT`` without hard-failing the whole extension.

    The value is needed while the ``/clear`` decorators evaluate, i.e. at import
    time, and a broken ``.env`` should produce a loud startup failure from
    ``config`` rather than an unimportable cog. So this degrades to the default
    and the real value is still re-validated when the command runs.
    """
    raw = os.getenv("MAX_PURGE_AMOUNT", "").strip()
    if raw.isdigit():
        return max(2, min(int(raw), 5000))
    return 100


#: Per-call bulk-delete ceiling, mirrored into the ``/clear`` option bounds.
MAX_PURGE_AMOUNT: Final[int] = _purge_ceiling()


class Moderation(commands.Cog):
    """Enforcement commands. Requires Manage Messages / Kick / Ban at minimum."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.settings = get_settings()

    def cog_load(self) -> None:
        # The sweeper must not launch from __init__: extensions load *before*
        # login, and ``wait_until_ready()`` raises on a client that has not been
        # initialised yet. Waiting for the real on_ready event is both correct
        # and reconnect-safe.
        self.bot.add_listener(self._start_sweeper, "on_ready")

    def cog_unload(self) -> None:
        self.bot.remove_listener(self._start_sweeper, "on_ready")
        self._expiry_sweep.cancel()

    async def _start_sweeper(self) -> None:
        if not self._expiry_sweep.is_running():
            self._expiry_sweep.start()

    # ------------------------------------------------------------------ #
    # /ban
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="ban",
        description="Ban a user from this server, optionally deleting their recent messages.",
    )
    @app_commands.guild_only
    @app_commands.describe(
        target="The user to ban. They do not have to still be in the server.",
        reason="Why they are being banned. Stored in the case ledger.",
        delete_days="Days of their message history to remove (0-7).",
    )
    async def ban(
        self,
        interaction: discord.Interaction,
        target: discord.User,
        reason: app_commands.Range[str, 1, 512] | None = None,
        delete_days: app_commands.Range[int, 0, 7] = 0,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._guard(interaction)
        PUNISHMENT_LIMIT.apply("ban", interaction.guild_id, interaction.user.id)

        reason_text = (reason or NO_REASON).strip()
        guild = interaction.guild
        me = guild.me

        if target.id == interaction.user.id:
            raise ZagrosError("You cannot ban yourself.")
        if target.id == me.id:
            raise ZagrosError("I cannot ban myself.")
        if target.id == guild.owner_id:
            raise PermissionDeniedError(
                "The server owner cannot be banned. Transfer ownership first."
            )

        member = guild.get_member(target.id)
        if member is not None:
            self._assert_moderator_can_act(guild.me, member)
        if member is not None and member.top_role >= me.top_role:
            raise HierarchyError(
                f"**{member}** holds a role at or above mine "
                f"(`{member.top_role.name}`). Move my role above theirs, or "
                "choose a different target."
            )

        case_ref = await self._open_case(
            interaction, CaseAction.BAN, target.id, reason_text
        )

        try:
            await guild.ban(
                target,
                delete_message_days=int(delete_days),
                reason=build_audit_reason(case_ref, interaction.user, reason_text),
            )
        except discord.Forbidden as exc:
            raise PermissionDeniedError(
                "I am missing **Ban Members**, or my highest role is not above "
                "this user's."
            ) from exc
        except discord.HTTPException as exc:
            if exc.status == 403:
                raise PermissionDeniedError(
                    "Discord refused the ban. That usually means the target is the "
                    "server owner, or my role sits below theirs."
                ) from exc
            raise ZagrosError(
                f"Discord returned `{exc.status}` while banning: {truncate(exc.text, 200)}"
            ) from exc

        history_note = (
            f"\n{describe_days(int(delete_days))} of message history removed."
            if delete_days
            else ""
        )
        embed = punishment_embed(
            action="Ban",
            guild=guild,
            member=target,
            moderator=interaction.user,
            reason=reason_text,
            case_ref=case_ref,
            dm_message=f"**{target}** was banned from **{guild.name}**.{history_note}",
        )
        dm = dm_notice_embed(
            action="Banned",
            guild=guild,
            reason=reason_text,
            case_ref=case_ref,
            moderator=interaction.user,
        )
        dm_sent, logged = await asyncio.gather(
            self._safe_notify(target, dm, guild.id),
            log_action(guild, embed),
        )

        history_note = (
            f"\n{describe_days(int(delete_days))} of message history deleted."
            if delete_days
            else ""
        )
        await interaction.followup.send(
            embed=success_embed(
                f"**{target}** was banned.{history_note}\n"
                f"{self._case_line(case_ref)}",
                title="Banned",
            ).set_footer(text=self._delivery_line(dm_sent, logged)),
            ephemeral=True,
        )
        logger.info(
            "Banned %s from guild %s by %s (case=%s, delete_days=%s)",
            target.id, guild.id, interaction.user.id, case_ref, delete_days,
        )

    # ------------------------------------------------------------------ #
    # /kick
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="kick", description="Remove a member from this server."
    )
    @app_commands.guild_only
    @app_commands.describe(
        target="The member to kick.",
        reason="Why they are being kicked.",
    )
    async def kick(
        self,
        interaction: discord.Interaction,
        target: discord.Member,
        reason: app_commands.Range[str, 1, 512] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._guard(interaction)
        PUNISHMENT_LIMIT.apply("kick", interaction.guild_id, interaction.user.id)

        reason_text = (reason or NO_REASON).strip()
        guild = interaction.guild
        me = guild.me

        if target.id == interaction.user.id:
            raise ZagrosError("You cannot kick yourself.")
        if target.id == me.id:
            raise ZagrosError("I cannot kick myself.")
        if target.id == guild.owner_id:
            raise PermissionDeniedError("The server owner cannot be kicked.")
        self._assert_moderator_can_act(guild.me, target)
        if target.top_role >= me.top_role:
            raise HierarchyError(
                f"**{target}** holds a role at or above mine "
                f"(`{target.top_role.name}`). I cannot act on them."
            )

        case_ref = await self._open_case(
            interaction, CaseAction.KICK, target.id, reason_text
        )

        try:
            await guild.kick(
                target,
                reason=build_audit_reason(case_ref, interaction.user, reason_text),
            )
        except discord.Forbidden as exc:
            raise PermissionDeniedError(
                "I am missing **Kick Members**, or my highest role is not above "
                "this member's."
            ) from exc
        except discord.HTTPException as exc:
            raise ZagrosError(
                f"Discord returned an error while kicking: {truncate(exc.text, 200)}"
            ) from exc

        embed = punishment_embed(
            action="Kick",
            guild=guild,
            member=target,
            moderator=interaction.user,
            reason=reason_text,
            case_ref=case_ref,
        )
        dm = dm_notice_embed(
            action="Kicked",
            guild=guild,
            reason=reason_text,
            case_ref=case_ref,
            moderator=interaction.user,
        )
        dm_sent, logged = await asyncio.gather(
            self._safe_notify(target, dm, guild.id),
            log_action(guild, embed),
        )

        await interaction.followup.send(
            embed=success_embed(
                f"**{target}** was kicked.\n{self._case_line(case_ref)}",
                title="Kicked",
            ).set_footer(text=self._delivery_line(dm_sent, logged)),
            ephemeral=True,
        )
        logger.info(
            "Kicked %s from guild %s by %s (case=%s)",
            target.id, guild.id, interaction.user.id, case_ref,
        )

    # ------------------------------------------------------------------ #
    # /mute
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="mute",
        description="Silence a member using the server's mute role.",
    )
    @app_commands.guild_only
    @app_commands.describe(
        target="The member to silence.",
        duration="How long the mute lasts (30m, 12h, 7d, 1w 2d). Omit for permanent.",
        reason="Why they are being muted.",
    )
    async def mute(
        self,
        interaction: discord.Interaction,
        target: discord.Member,
        duration: Duration | None = None,
        reason: app_commands.Range[str, 1, 512] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._guard(interaction)
        PUNISHMENT_LIMIT.apply("mute", interaction.guild_id, interaction.user.id)

        reason_text = (reason or NO_REASON).strip()
        guild = interaction.guild
        me = guild.me

        if target.id == interaction.user.id:
            raise ZagrosError("You cannot mute yourself.")
        if target.id == me.id:
            raise ZagrosError("I cannot mute myself.")
        if target.id == guild.owner_id:
            raise PermissionDeniedError("The server owner cannot be muted.")
        self._assert_moderator_can_act(guild.me, target)
        if target.top_role >= me.top_role:
            raise HierarchyError(
                f"**{target}** holds a role at or above mine "
                f"(`{target.top_role.name}`). I cannot mute them."
            )

        role = await ensure_mute_role(guild)
        if role in target.roles:
            raise ZagrosError(
                f"**{target}** already holds the mute role `{role.name}`. "
                "Use `/unmute` to lift it."
            )

        expiry = utc_expiry(duration)
        case_ref = await self._open_case(
            interaction, CaseAction.MUTE, target.id, reason_text, expires_at=expiry
        )

        try:
            await target.add_roles(
                role,
                reason=build_audit_reason(case_ref, interaction.user, reason_text),
            )
        except discord.Forbidden as exc:
            raise PermissionDeniedError(
                f"I could not assign `{role.name}`. Confirm my role sits above it "
                "and that I hold **Manage Roles**."
            ) from exc
        except discord.HTTPException as exc:
            raise ZagrosError(
                f"Discord returned an error while muting: {truncate(exc.text, 200)}"
            ) from exc

        embed = punishment_embed(
            action="Mute",
            guild=guild,
            member=target,
            moderator=interaction.user,
            reason=reason_text,
            case_ref=case_ref,
            duration=duration,
            dm_message=f"**{target}** was muted in **{guild.name}**.",
        )
        dm = dm_notice_embed(
            action="Muted",
            guild=guild,
            reason=reason_text,
            case_ref=case_ref,
            duration=duration,
            moderator=interaction.user,
        )
        dm_sent, logged = await asyncio.gather(
            self._safe_notify(target, dm, guild.id),
            log_action(guild, embed),
        )

        expiry_line = (
            f" until {timestamp(expiry)}" if expiry else ""
        )
        await interaction.followup.send(
            embed=success_embed(
                f"**{target}** is now muted via {describe_role(role)}.\n"
                f"Duration **{format_duration(duration)}**{expiry_line}\n"
                f"{self._case_line(case_ref)}",
                title="Muted",
            ).set_footer(text=self._delivery_line(dm_sent, logged)),
            ephemeral=True,
        )
        logger.info(
            "Muted %s in guild %s for %s by %s (case=%s)",
            target.id, guild.id, format_duration(duration),
            interaction.user.id, case_ref,
        )

    # ------------------------------------------------------------------ #
    # /unmute
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="unmute", description="Lift an active mute from a member."
    )
    @app_commands.guild_only
    @app_commands.describe(
        target="The member to unmute.",
        reason="Why the mute is being lifted.",
    )
    async def unmute(
        self,
        interaction: discord.Interaction,
        target: discord.Member,
        reason: app_commands.Range[str, 1, 512] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._guard(interaction)
        PUNISHMENT_LIMIT.apply("unmute", interaction.guild_id, interaction.user.id)

        reason_text = (reason or "Mute lifted").strip()
        guild = interaction.guild

        config = await get_or_create_guild_config(guild.id)
        role: discord.Role | None = None
        if config and config.muted_role_id:
            role = guild.get_role(config.muted_role_id)
        if role is None:
            role = discord.utils.get(guild.roles, name=self.settings.default_mute_role)
        if role is None:
            raise ZagrosError(
                "This server has no mute role. Run `/setup muted-role`, or "
                "`/mute` will create one on first use."
            )
        if role not in target.roles:
            raise ZagrosError(f"**{target}** does not hold the mute role `{role.name}`.")

        # Close the newest open mute case so the ledger matches reality.
        case_ref: str | None = None
        active = await fetch_active_cases(
            guild.id, user_id=target.id, action=CaseAction.MUTE, limit=1
        )
        if active:
            case_ref = active[0].case_ref
            await self._revoke_case(active[0].id, interaction.user.id, reason_text)

        try:
            await target.remove_roles(
                role,
                reason=build_audit_reason(case_ref, interaction.user, reason_text),
            )
        except discord.Forbidden as exc:
            raise PermissionDeniedError(
                f"I could not remove `{role.name}`. Confirm my role position and "
                "that I hold **Manage Roles**."
            ) from exc
        except discord.HTTPException as exc:
            raise ZagrosError(
                f"Discord returned an error: {truncate(exc.text, 200)}"
            ) from exc

        embed = punishment_embed(
            action="Unmute",
            guild=guild,
            member=target,
            moderator=interaction.user,
            reason=reason_text,
            case_ref=case_ref,
            dm_message=f"**{target}** was unmuted in **{guild.name}**.",
        )
        logged = await log_action(guild, embed)

        await interaction.followup.send(
            embed=success_embed(
                f"**{target}** is no longer muted.\n"
                f"Role {describe_role(role)} removed · "
                f"case `{case_ref or 'none'}`",
                title="Unmuted",
            ).set_footer(text=self._delivery_line(False, logged)),
            ephemeral=True,
        )
        logger.info(
            "Unmuted %s in guild %s by %s (case=%s)",
            target.id, guild.id, interaction.user.id, case_ref,
        )

    # ------------------------------------------------------------------ #
    # /warn
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="warn", description="Record a formal warning against a member."
    )
    @app_commands.guild_only
    @app_commands.describe(
        target="The member to warn.",
        reason="What they did. Stored permanently in the case ledger.",
    )
    async def warn(
        self,
        interaction: discord.Interaction,
        target: discord.Member,
        reason: app_commands.Range[str, 1, 512],
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._guard(interaction)
        PUNISHMENT_LIMIT.apply("warn", interaction.guild_id, interaction.user.id)

        reason_text = reason.strip()
        guild = interaction.guild

        if target.id == interaction.user.id:
            raise ZagrosError("You cannot warn yourself.")
        if target.id == guild.me.id:
            raise ZagrosError("I cannot warn myself.")
        if target.id == guild.owner_id:
            raise PermissionDeniedError("The server owner cannot be warned.")
        self._assert_moderator_can_act(guild.me, target)

        case = await record_case(
            guild_id=guild.id,
            user_id=target.id,
            moderator_id=interaction.user.id,
            action=CaseAction.WARN,
            reason=reason_text,
        )
        history = await fetch_active_cases(
            guild.id, user_id=target.id, action=CaseAction.WARN, limit=1000
        )

        embed = punishment_embed(
            action="Warning",
            guild=guild,
            member=target,
            moderator=interaction.user,
            reason=reason_text,
            case_ref=case.case_ref,
            dm_message=f"**{target}** was warned in **{guild.name}**.",
            extra=[("Total warnings", str(len(history)), True)],
        )
        dm = dm_notice_embed(
            action="Warned",
            guild=guild,
            reason=reason_text,
            case_ref=case.case_ref,
            moderator=interaction.user,
        )
        dm_sent, logged = await asyncio.gather(
            self._safe_notify(target, dm, guild.id),
            log_action(guild, embed),
        )

        await interaction.followup.send(
            embed=success_embed(
                f"**{target}** has been warned.\n"
                f"{self._case_line(case.case_ref)}\n"
                f"This is warning **#{len(history)}** for this member.",
                title="Warning issued",
            ).set_footer(text=self._delivery_line(dm_sent, logged)),
            ephemeral=True,
        )
        logger.info(
            "Warned %s in guild %s by %s (case=%s, total_warnings=%s)",
            target.id, guild.id, interaction.user.id, case.case_ref, len(history),
        )

    # ------------------------------------------------------------------ #
    # /warnings
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="warnings",
        description="Review a member's active moderation history.",
    )
    @app_commands.guild_only
    @app_commands.describe(
        target="The member to inspect.",
        action="Optionally filter to a single type of action.",
    )
    async def warnings(
        self,
        interaction: discord.Interaction,
        target: discord.Member,
        action: Literal[
            "ban", "kick", "mute", "warn", "purge"
        ] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._guard(interaction)

        wanted = CaseAction(action) if action else None
        cases = await fetch_active_cases(
            interaction.guild_id, user_id=target.id, action=wanted, limit=25
        )

        embed = base_embed(
            title=f"Moderation history · {target}",
            description=describe_member(target),
            author=interaction.user,
        )
        if not cases:
            scope = f"`{action}` " if action else ""
            embed.description += f"\n\nNo active {scope}cases on record."
            embed.colour = COLOR_SUCCESS
        else:
            for case in cases:
                expiry = ""
                if case.expires_at is not None:
                    expiry = (
                        f" · expires {timestamp(case.expires_at)}"
                        if not case.is_expired
                        else " · expired"
                    )
                embed.add_field(
                    name=f"`{case.case_ref}` · {case.action.title()}{expiry}",
                    value=(
                        f"Issued {timestamp(case.created_at)} by <@{case.moderator_id}>\n"
                        f"{truncate(case.reason or NO_REASON, 200)}"
                    ),
                    inline=False,
                )
            embed.colour = COLOR_WARNING
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------ #
    # /clear
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="clear",
        description="Bulk-delete recent messages from this channel.",
    )
    @app_commands.guild_only
    @app_commands.describe(
        amount=f"How many messages to delete (2-{MAX_PURGE_AMOUNT} per call).",
        reason="Why the messages are being removed.",
    )
    async def clear(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 2, MAX_PURGE_AMOUNT] = 10,
        reason: app_commands.Range[str, 1, 512] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._guard(interaction, require=("manage_messages",))
        PURGE_LIMIT.apply("clear", interaction.guild_id, interaction.user.id)

        # The annotation above is baked at import time; re-check against the
        # live value so a mid-run config change can never widen the ceiling.
        ceiling = min(self.settings.max_purge_amount, MAX_PURGE_AMOUNT)
        if amount > ceiling:
            raise ZagrosError(
                f"This server's ceiling is {ceiling} messages per `/clear` call."
            )

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            raise ZagrosError("This command only works in a text channel.")

        await self._assert_can_purge(interaction.guild.me, channel)

        reason_text = (reason or "Channel cleanup").strip()
        cutoff = interaction.created_at
        audit_reason = build_audit_reason(None, interaction.user, reason_text)

        deleted_total = 0
        requests = 0
        while deleted_total < amount and requests < MAX_PURGE_REQUESTS:
            batch = await self._purge_batch(
                channel, amount - deleted_total, cutoff, audit_reason
            )
            requests += 1
            if not batch:
                break
            deleted_total += batch
        else:
            if deleted_total < amount:
                logger.info(
                    "Purge in %s/%s hit the %s request ceiling at %s message(s)",
                    interaction.guild_id, channel.id, MAX_PURGE_REQUESTS, deleted_total,
                )

        case = await self._open_case(
            interaction,
            CaseAction.PURGE,
            interaction.user.id,
            f"{deleted_total} message(s) purged in #{channel.name}: {reason_text}",
            quiet=True,
        )

        await interaction.followup.send(
            embed=success_embed(
                f"Deleted **{deleted_total}** message(s) from "
                f"{describe_channel(channel)} in {requests} request(s).\n"
                f"{self._case_line(case)}",
                title="Channel cleared",
            ),
            ephemeral=True,
        )
        logger.info(
            "Purged %s message(s) from %s/%s by %s (case=%s)",
            deleted_total, interaction.guild_id, channel.id,
            interaction.user.id, case,
        )

    # ------------------------------------------------------------------ #
    # /massban
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="massban",
        description="Ban many accounts at once from a pasted list of user IDs.",
    )
    @app_commands.guild_only
    @app_commands.describe(
        targets=(
            "User IDs to ban. Paste them straight from the client: mentions, "
            "bare IDs, separated by spaces, commas or semicolons."
        ),
        reason="Why they are being banned. Stored against every case.",
        delete_days="Days of message history to remove from each account (0-7).",
    )
    async def massban(
        self,
        interaction: discord.Interaction,
        targets: app_commands.Range[str, 1, 4000],
        reason: app_commands.Range[str, 1, 512] | None = None,
        delete_days: app_commands.Range[int, 0, 7] = 0,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._guard(interaction, require=("ban_members",))
        PUNISHMENT_LIMIT.apply("massban", interaction.guild_id, interaction.user.id)

        guild = interaction.guild
        me = guild.me
        reason_text = (reason or NO_REASON).strip()
        user_ids, rejected = parse_user_ids(targets)

        if not user_ids:
            raise ZagrosError(
                "None of that parsed as a user ID. Paste them as `<@123456789>` "
                "or plain 17-20 digit numbers."
            )

        # Roles are not in the ban payload, so anything that still has a member
        # object is checked against the moderator hierarchy before we start.
        blocked: list[tuple[int, str]] = []
        for user_id in user_ids:
            if user_id in {interaction.user.id, me.id}:
                blocked.append((user_id, "is you or me"))
                continue
            if user_id == guild.owner_id:
                blocked.append((user_id, "is the server owner"))
                continue
            member = guild.get_member(user_id)
            if member is not None:
                if member.top_role >= me.top_role:
                    blocked.append((user_id, f"outranks me (`{member.top_role.name}`)"))
                    continue
                try:
                    self._assert_moderator_can_act(interaction.user, member)
                except HierarchyError:
                    blocked.append(
                        (user_id, f"outranks you (`{member.top_role.name}`)")
                    )

        eligible = [uid for uid in user_ids if uid not in {uid for uid, _ in blocked}]
        if not eligible:
            raise ZagrosError(
                "Every ID in that list is one I cannot act on. "
                f"First problem: `{blocked[0][1]}`."
            )

        logger.info(
            "Massban requested in %s by %s: %s target(s), %s blocked, %s unparsable",
            guild.id, interaction.user.id, len(user_ids), len(blocked), len(rejected),
        )
        started = time.perf_counter()
        banned, failed = await self._mass_ban(
            guild, eligible, reason_text, int(delete_days), interaction.user
        )
        elapsed_ms = (time.perf_counter() - started) * 1000

        # One case per account: the ledger is a per-user audit trail, and a
        # single summary row would make "who was banned in this run" unanswerable.
        for user_id in banned:
            await self._open_case(
                interaction, CaseAction.MASSBAN, user_id, reason_text, quiet=True
            )

        await self._reply_bulk(
            interaction,
            action="Mass ban",
            attempted=len(eligible),
            succeeded=banned,
            failed=failed,
            blocked=blocked,
            rejected=rejected,
            elapsed_ms=elapsed_ms,
            reason=reason_text,
        )
        logger.info(
            "Massban in %s finished: %s banned, %s failed in %.0f ms",
            guild.id, len(banned), len(failed), elapsed_ms,
        )

    async def _mass_ban(
        self,
        guild: discord.Guild,
        user_ids: list[int],
        reason: str,
        delete_days: int,
        moderator: discord.Member,
    ) -> tuple[list[int], list[tuple[int, str]]]:
        """Ban ``user_ids`` with bounded concurrency, one case-anchor reason.

        Concurrency is capped rather than unbounded: 100 parallel HTTP requests
        would trip the global rate limit for the *whole bot* and stall unrelated
        commands. Five in flight keeps the run fast and the bucket healthy.
        """
        semaphore = asyncio.Semaphore(MAX_MASSBAN_CONCURRENCY)
        audit_reason = build_audit_reason(None, moderator, reason)
        banned: list[int] = []
        failed: list[tuple[int, str]] = []

        async def ban_one(user_id: int) -> None:
            async with semaphore:
                try:
                    target = await guild.fetch_member(user_id)
                except discord.NotFound:
                    # Fetching a member is a convenience for the audit log; a
                    # missing one is not a reason to skip a ban.
                    target = discord.Object(id=user_id)  # type: ignore[assignment]
                except discord.HTTPException as exc:
                    failed.append((user_id, f"lookup failed ({exc.status})"))
                    return
                try:
                    await guild.ban(
                        target,
                        delete_message_days=delete_days,
                        reason=audit_reason,
                    )
                except discord.Forbidden as exc:
                    failed.append((user_id, f"{exc.status} forbidden"))
                except discord.HTTPException as exc:
                    failed.append((user_id, f"HTTP {exc.status}"))
                else:
                    banned.append(user_id)

        await asyncio.gather(*(ban_one(uid) for uid in user_ids))
        return banned, failed

    async def _reply_bulk(
        self,
        interaction: discord.Interaction,
        *,
        action: str,
        attempted: int,
        succeeded: list[int],
        failed: list[tuple[int, str]],
        blocked: list[tuple[int, str]],
        rejected: list[str],
        elapsed_ms: float,
        reason: str,
    ) -> None:
        """Report a batch run: what landed, what did not, and why."""
        colour = COLOR_SUCCESS if not failed else COLOR_WARNING
        lines = [
            f"**{len(succeeded)}** of **{attempted}** account(s) processed in "
            f"**{elapsed_ms:.0f} ms**."
        ]
        if failed:
            head = ", ".join(f"`{uid}` ({why})" for uid, why in failed[:10])
            more = f" · +{len(failed) - 10} more" if len(failed) > 10 else ""
            lines.append(f"**Failed ({len(failed)}):** {head}{more}")
        if blocked:
            head = ", ".join(f"`{uid}` ({why})" for uid, why in blocked[:10])
            more = f" · +{len(blocked) - 10} more" if len(blocked) > 10 else ""
            lines.append(f"**Skipped ({len(blocked)}):** {head}{more}")
        if rejected:
            head = ", ".join(f"`{token}`" for token in rejected[:10])
            more = f" · +{len(rejected) - 10} more" if len(rejected) > 10 else ""
            lines.append(f"**Unreadable ({len(rejected)}):** {head}{more}")
        lines.append(f"Reason recorded: **{truncate(reason, 120)}**")

        embed = base_embed(
            title=f"{action} · {len(succeeded)}/{attempted}",
            description="\n".join(lines),
            colour=colour,
            author=interaction.user,
        )
        embed.set_footer(text=self._delivery_line(True, True))
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------ #
    # /softban
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="softban",
        description=(
            "Kick a user while purging their recent messages — bans and unbans "
            "them so they can rejoin."
        ),
    )
    @app_commands.guild_only
    @app_commands.describe(
        target="The member to softban.",
        reason="Why they are being softbanned.",
        delete_days="Days of their message history to remove (0-7).",
    )
    async def softban(
        self,
        interaction: discord.Interaction,
        target: discord.Member,
        reason: app_commands.Range[str, 1, 512] | None = None,
        delete_days: app_commands.Range[int, 0, 7] = 1,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._guard(interaction, require=("ban_members",))
        PUNISHMENT_LIMIT.apply("softban", interaction.guild_id, interaction.user.id)

        reason_text = (reason or NO_REASON).strip()
        guild = interaction.guild
        me = guild.me

        if target.id == interaction.user.id:
            raise ZagrosError("You cannot softban yourself.")
        if target.id == me.id:
            raise ZagrosError("I cannot softban myself.")
        if target.id == guild.owner_id:
            raise PermissionDeniedError("The server owner cannot be softbanned.")
        self._assert_moderator_can_act(me, target)
        if target.top_role >= me.top_role:
            raise HierarchyError(
                f"**{target}** holds a role at or above mine "
                f"(`{target.top_role.name}`). I cannot act on them."
            )

        case_ref = await self._open_case(
            interaction, CaseAction.SOFTBAN, target.id, reason_text
        )
        audit_reason = build_audit_reason(case_ref, interaction.user, reason_text)

        try:
            await guild.ban(
                target,
                delete_message_days=int(delete_days),
                reason=audit_reason,
            )
        except discord.Forbidden as exc:
            raise PermissionDeniedError(
                "I am missing **Ban Members**, or my highest role is not above "
                "this user's."
            ) from exc
        except discord.HTTPException as exc:
            raise ZagrosError(
                f"Discord returned `{exc.status}` while softbanning: "
                f"{truncate(exc.text, 200)}"
            ) from exc

        # The unban is what makes it a *soft* ban. A failure here is reported
        # loudly rather than swallowed: the moderator needs to know the user is
        # currently banned when they believed otherwise.
        lifted = False
        try:
            await guild.unban(target.id, reason=f"Softban complete · {case_ref or ''}".strip())
            lifted = True
        except discord.NotFound:
            # Already unbanned by hand in the gap between the two calls.
            lifted = True
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.error("Softban left %s banned in %s: %s", target.id, guild.id, exc)

        embed = punishment_embed(
            action="Softban",
            guild=guild,
            member=target,
            moderator=interaction.user,
            reason=reason_text,
            case_ref=case_ref,
            dm_message=(
                f"**{target}** was removed from **{guild.name}**; their last "
                f"{describe_days(int(delete_days))} of messages were deleted."
            ),
        )
        dm = dm_notice_embed(
            action="Removed (softban)",
            guild=guild,
            reason=reason_text,
            case_ref=case_ref,
            moderator=interaction.user,
        )
        dm_sent, logged = await asyncio.gather(
            self._safe_notify(target, dm, guild.id),
            log_action(guild, embed),
        )

        status = (
            f"**{target}** was removed and "
            f"{describe_days(int(delete_days))} of their history was deleted. "
            "They may rejoin immediately."
            if lifted
            else (
                f"**{target}** was removed and their history was deleted, but the "
                "lifting ban failed — they are still banned. Unban them manually."
            )
        )
        await interaction.followup.send(
            embed=success_embed(
                f"{status}\n{self._case_line(case_ref)}",
                title="Softbanned" if lifted else "Softban incomplete",
            ).set_footer(text=self._delivery_line(dm_sent, logged)),
            ephemeral=True,
        )
        logger.info(
            "Softbanned %s from %s by %s (case=%s, lifted=%s)",
            target.id, guild.id, interaction.user.id, case_ref, lifted,
        )

    # ------------------------------------------------------------------ #
    # /tempban
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="tempban",
        description="Ban a user for a while. The ban lifts itself when the time is up.",
    )
    @app_commands.guild_only
    @app_commands.describe(
        target="The user to ban. They do not have to still be in the server.",
        duration="How long, e.g. `12h`, `3d`, `1w 2d`. Maximum 365 days.",
        reason="Why they are being banned. Stored in the case ledger.",
        delete_days="Days of their message history to remove (0-7).",
    )
    async def tempban(
        self,
        interaction: discord.Interaction,
        target: discord.User,
        duration: Duration,
        reason: app_commands.Range[str, 1, 512] | None = None,
        delete_days: app_commands.Range[int, 0, 7] = 0,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._guard(interaction, require=("ban_members",))
        PUNISHMENT_LIMIT.apply("tempban", interaction.guild_id, interaction.user.id)

        reason_text = (reason or NO_REASON).strip()
        guild = interaction.guild
        me = guild.me

        if target.id == interaction.user.id:
            raise ZagrosError("You cannot ban yourself.")
        if target.id == me.id:
            raise ZagrosError("I cannot ban myself.")
        if target.id == guild.owner_id:
            raise PermissionDeniedError(
                "The server owner cannot be banned. Transfer ownership first."
            )

        member = guild.get_member(target.id)
        if member is not None:
            self._assert_moderator_can_act(me, member)
        if member is not None and member.top_role >= me.top_role:
            raise HierarchyError(
                f"**{member}** holds a role at or above mine "
                f"(`{member.top_role.name}`). Move my role above theirs, or "
                "choose a different target."
            )

        expiry = utc_expiry(duration)
        case_ref = await self._open_case(
            interaction,
            CaseAction.TEMPBAN,
            target.id,
            reason_text,
            expires_at=expiry,
        )

        try:
            await guild.ban(
                target,
                delete_message_days=int(delete_days),
                reason=build_audit_reason(case_ref, interaction.user, reason_text),
            )
        except discord.Forbidden as exc:
            raise PermissionDeniedError(
                "I am missing **Ban Members**, or my highest role is not above "
                "this user's."
            ) from exc
        except discord.HTTPException as exc:
            if exc.status == 403:
                raise PermissionDeniedError(
                    "Discord refused the ban. That usually means the target is the "
                    "server owner, or my role sits below theirs."
                ) from exc
            raise ZagrosError(
                f"Discord returned `{exc.status}` while banning: {truncate(exc.text, 200)}"
            ) from exc

        embed = punishment_embed(
            action="Tempban",
            guild=guild,
            member=target,
            moderator=interaction.user,
            reason=reason_text,
            case_ref=case_ref,
            duration=duration,
            dm_message=(
                f"**{target}** was banned from **{guild.name}** for "
                f"**{format_duration(duration)}**."
            ),
        )
        dm = dm_notice_embed(
            action="Temporarily banned",
            guild=guild,
            reason=reason_text,
            case_ref=case_ref,
            moderator=interaction.user,
            duration=duration,
        )
        dm_sent, logged = await asyncio.gather(
            self._safe_notify(target, dm, guild.id),
            log_action(guild, embed),
        )

        await interaction.followup.send(
            embed=success_embed(
                f"**{target}** is banned for **{format_duration(duration)}**.\n"
                f"Lifts automatically {timestamp(expiry)}.\n"
                f"{self._case_line(case_ref)}",
                title="Temporarily banned",
            ).set_footer(text=self._delivery_line(dm_sent, logged)),
            ephemeral=True,
        )
        logger.info(
            "Tempbanned %s from %s by %s until %s (case=%s)",
            target.id, guild.id, interaction.user.id, expiry, case_ref,
        )

    # ------------------------------------------------------------------ #
    # Background maintenance
    # ------------------------------------------------------------------ #
    @tasks.loop(minutes=5.0)
    async def _expiry_sweep(self) -> None:
        """Release expired mutes and lift expired tempbans in every cached guild.

        Cheap by design — :func:`expire_stale_mutes` returns immediately unless a
        guild actually has members holding the mute role, and
        :func:`expire_stale_bans` only issues requests for guilds that actually
        have an active TEMPBAN row — and a single failure never stops the loop.
        """
        try:
            mutes = 0
            bans = 0
            for guild in list(self.bot.guilds):
                mutes += await expire_stale_mutes(guild)
                bans += len(await expire_stale_bans(guild))
            if mutes or bans:
                logger.info(
                    "Expiry sweep released %s mute(s) and %s tempban(s)",
                    mutes, bans,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Expiry sweep failed; retrying next cycle")

    @_expiry_sweep.before_loop
    async def _before_sweep(self) -> None:
        # Without this the first run would iterate an empty guild list.
        await self.bot.wait_until_ready()
        logger.info("Expiry sweeper armed (interval=5m)")

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        logger.info("Removed from guild %s (%s)", guild.name, guild.id)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    @command_context("moderation:guard")
    async def _guard(
        self,
        interaction: discord.Interaction,
        *,
        require: tuple[str, ...] | None = None,
    ) -> None:
        """Authorisation gate applied to every command in this cog.

        Also verifies the *bot* can act, so the moderator gets an actionable
        message instead of an opaque ``Forbidden`` from Discord.
        """
        if not await has_moderation_access(interaction, require):
            raise PermissionDeniedError(
                "You need to be a moderator here — hold **Manage Messages**, "
                "**Kick Members** or **Ban Members**, or a role named `moderator`, "
                "`mod`, `mods`, `staff` or `helper`."
            )

    @command_context("moderation:hierarchy")
    def _assert_moderator_can_act(
        self, actor: discord.Member, target: discord.Member
    ) -> None:
        """Refuse an action a moderator is not senior enough to take.

        Discord enforces role order for the *bot*; it does nothing about the
        moderator issuing the command. Without this check a moderator with
        ``Moderate Members`` could ban the entire admin team and every ban would
        succeed.

        Bot owners and administrators bypass the check: they can already do
        whatever they like, and an owner being outranked by an Administrator
        role would otherwise be a lockout.
        """
        if _is_owner(actor.id):
            return
        if actor.guild_permissions.administrator or actor.id == actor.guild.owner_id:
            return
        if target.top_role >= actor.top_role:
            raise HierarchyError(
                f"**{target}** holds `{target.top_role.name}`, which is at or above "
                f"your own highest role (`{actor.top_role.name}`). Pick someone "
                "below you in the role list."
            )

    @command_context("moderation:open_case")
    async def _open_case(
        self,
        interaction: discord.Interaction,
        action: CaseAction,
        user_id: int,
        reason: str,
        *,
        expires_at: object = None,
        quiet: bool = False,
    ) -> str | None:
        """Write a ledger row, warning the moderator if the ledger is down.

        Returns the case reference, or ``None`` when the write failed. With
        ``quiet`` the warning is logged only — used by ``/clear``, where an
        extra ephemeral message is just noise.
        """
        try:
            case = await record_case(
                guild_id=interaction.guild_id,
                user_id=user_id,
                moderator_id=interaction.user.id,
                action=action,
                reason=reason,
                expires_at=expires_at,  # type: ignore[arg-type]
            )
        except ZagrosError as exc:
            logger.warning("Case write failed for %s: %s", action.value, exc.user_message)
            if not quiet:
                await interaction.followup.send(
                    embed=warning_embed(
                        exc.user_message, title="Ledger unavailable"
                    ),
                    ephemeral=True,
                )
            return None
        return case.case_ref

    @command_context("moderation:notify")
    async def _safe_notify(
        self, user: discord.abc.User, embed: discord.Embed, guild_id: int
    ) -> bool:
        """DM a user, tolerating a missing ``Member`` (left the server)."""
        member = user if isinstance(user, discord.Member) else None
        if member is None:
            guild = self.bot.get_guild(guild_id)
            member = guild.get_member(user.id) if guild else None
        if member is None:
            logger.info("Skipping DM to %s: not a member of guild %s", user.id, guild_id)
            return False
        return await notify_member(member, embed)

    @staticmethod
    async def _assert_can_purge(
        me: discord.Member, channel: discord.TextChannel
    ) -> None:
        perms = me.permissions_in(channel)
        missing = [
            name
            for name, allowed in (
                ("Manage Messages", perms.manage_messages),
                ("Read Message History", perms.read_message_history),
            )
            if not allowed
        ]
        if missing:
            raise PermissionDeniedError(
                f"I am missing **{'**, **'.join(missing)}** in "
                f"{describe_channel(channel)}."
            )

    @staticmethod
    async def _purge_batch(
        channel: discord.TextChannel,
        limit: int,
        before: discord.Object,
        audit_reason: str,
    ) -> int:
        """One bulk-delete call. Translates every failure into a user error."""
        try:
            deleted = await channel.purge(
                limit=limit,
                before=before,
                bulk=True,
                reason=audit_reason,
            )
        except discord.Forbidden as exc:
            raise PermissionDeniedError(
                f"I was denied access while deleting in {describe_channel(channel)}. "
                "Check **Manage Messages** and **Read Message History**."
            ) from exc
        except discord.NotFound as exc:
            raise ZagrosError(
                f"{describe_channel(channel)} no longer exists, or I lost access "
                "to it."
            ) from exc
        except discord.HTTPException as exc:
            if exc.status == 400:
                raise ZagrosError(
                    "Discord rejected the request. Bulk deletion only covers the "
                    "last 14 days of messages."
                ) from exc
            if exc.status == 403:
                raise PermissionDeniedError(
                    "Only messages newer than 14 days can be bulk-deleted."
                ) from exc
            raise ZagrosError(
                f"Discord returned `{exc.status}` while deleting messages."
            ) from exc
        return len(deleted)

    async def _revoke_case(self, case_id: int, revoked_by: int, reason: str) -> None:
        """Mark a case inactive. Ledger bookkeeping must never raise."""
        try:
            async with get_database().session() as session:
                case = (
                    await session.execute(
                        select(ModCase).where(ModCase.id == case_id)
                    )
                ).scalar_one_or_none()
                if case is not None:
                    case.is_active = False
                    case.revoked_by = revoked_by
                    case.revoked_reason = truncate(reason, 500)
        except Exception:
            logger.exception("Could not revoke case %s", case_id)

    @staticmethod
    def _case_line(case_ref: str | None) -> str:
        return f"Case `{case_ref}`." if case_ref else "Case **not recorded**."

    @staticmethod
    def _delivery_line(dm_sent: bool, logged: bool) -> str:
        return (
            f"{'DM delivered' if dm_sent else 'DM not delivered'} · "
            f"{'logged' if logged else 'log channel unavailable'}"
        )


def describe_days(days: int) -> str:
    """Human wording for a message-history deletion window."""
    if days <= 0:
        return "None"
    if days == 1:
        return "1 day"
    return f"{days} days"


async def setup(bot: commands.Bot) -> None:
    """Extension entrypoint."""
    await bot.add_cog(Moderation(bot))
    logger.info("Moderation cog loaded")
