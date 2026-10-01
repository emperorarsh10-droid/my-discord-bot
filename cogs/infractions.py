"""Infraction records — the read side of the moderation ledger.

``cogs.moderation`` *writes* cases. This cog is everything a moderator needs to
go back and look at them:

* ``/warns``, ``/infractions``, ``/cases``, ``/viewcase`` — history,
* ``/clearwarns``, ``/reason`` — correction, which never destroys a row,
* ``/timeout``, ``/untimeout`` — Discord-native timeouts, distinct from the mute
  *role* that ``/mute`` applies,
* ``/unban``, ``/banlist`` — the ban lifecycle,
* ``/notes`` — context that is not an infraction and must never inflate a count.

Two rules run through all of it:

**Revocation is not deletion.** A warning that gets cleared is marked revoked with
who cleared it and why. A ledger that can be edited into agreeing with itself is
worth nothing in a dispute.

**Reads never raise.** Every history query degrades to an empty result and a log
line, because a moderator staring at a blank list during an incident needs to be
told the database is unreachable, not handed a traceback.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

from cogs._base import NO_REASON, FeatureCog, require_text
from core.database import get_database
from core.embeds import (
    COLOR_SUCCESS,
    COLOR_WARNING,
    base_embed,
    describe_member,
    format_duration,
    success_embed,
    timestamp,
    truncate,
    warning_embed,
)
from core.errors import PermissionDeniedError, ZagrosError
from core.logging_setup import get_logger
from core.models import CaseAction, ModCase
from core.ratelimit import RateLimit
from core.services import (
    build_audit_reason,
    count_active_warnings,
    count_case_history,
    dm_notice_embed,
    fetch_case_history,
    find_case,
    get_or_create_guild_config,
    log_action,
    notify_member,
    punishment_embed,
    record_case,
    revoke_cases,
    utc_expiry,
)
from core.transformers import Duration

logger = get_logger("zagrosian.cog.infractions")

#: Discord's hard ceiling on a communication timeout.
MAX_TIMEOUT: Final[timedelta] = timedelta(days=28)
CASES_PER_PAGE: Final[int] = 10
HISTORY_LIMIT: Final[int] = 50
#: Shortest plausible snowflake. Used to tell a pasted user id from a case number.
DISCORD_MIN_ID: Final[int] = 15


class Infractions(FeatureCog):
    """History, correction and the timeout/ban lifecycle."""

    feature_name = "infractions"
    bulk_limit = RateLimit(max_calls=4, window=30.0)

    # ------------------------------------------------------------------ #
    # /timeout
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="timeout",
        description="Silence a member with Discord's native timeout (max 28 days).",
    )
    @app_commands.guild_only
    @app_commands.describe(
        target="The member to time out.",
        duration="How long, up to 28d (30m, 12h, 7d).",
        reason="Why they are being timed out.",
    )
    async def timeout(
        self,
        interaction: discord.Interaction,
        target: discord.Member,
        duration: Duration,
        reason: app_commands.Range[str, 1, 512] | None = None,
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction, require=("moderate_members",))
        self.throttle("timeout", interaction.guild_id, interaction.user.id)

        # ``Duration.transform`` resolves to a plain timedelta before the callback
        # runs, so the annotation above is a lie the type checker cannot see and
        # the runtime value is what matters here.
        delta: timedelta = duration
        if delta <= timedelta(0):
            raise ZagrosError("The duration must be a positive amount of time.")
        if delta > MAX_TIMEOUT:
            raise ZagrosError(
                f"Discord caps timeouts at 28 days; **{format_duration(delta)}** is "
                "longer. Use `/mute` for an indefinite silence."
            )

        guild = interaction.guild
        reason_text = require_text(reason)

        if target.id == interaction.user.id:
            raise ZagrosError("You cannot time yourself out.")
        self.assert_hierarchy(interaction.user, target, guild.me, verb="time out")

        until = discord.utils.utcnow() + delta
        expires_at = utc_expiry(delta)

        case = await self._record(
            interaction, CaseAction.TIMEOUT, target.id, reason_text, expires_at
        )

        try:
            await target.timeout(until, reason=build_audit_reason("Timeout", reason_text))
        except discord.Forbidden:
            await self._retract(interaction, case)
            raise PermissionDeniedError(
                f"Discord refused the timeout on **{target}**. I need "
                "**Moderate Members** and a role above theirs."
            ) from None
        except discord.HTTPException as exc:
            await self._retract(interaction, case)
            raise ZagrosError(f"Discord rejected the timeout: {exc}") from exc

        await self._announce(
            interaction,
            target,
            action="Timeout",
            reason_text=reason_text,
            case_ref=case.case_ref if case else None,
            duration=delta,
        )
        await self.reply(
            interaction,
            success_embed(
                f"**{target}** cannot post or act for "
                f"{format_duration(delta)} ({timestamp(until, 'R')}).\n"
                f"Reason: {truncate(reason_text, 500)}",
                title="Member timed out",
                author=interaction.user,
            ),
        )

    # ------------------------------------------------------------------ #
    # /untimeout
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="untimeout",
        description="Lift a member's timeout early.",
    )
    @app_commands.guild_only
    @app_commands.describe(
        target="The member to release.",
        reason="Why the timeout is being lifted.",
    )
    async def untimeout(
        self,
        interaction: discord.Interaction,
        target: discord.Member,
        reason: app_commands.Range[str, 1, 512] | None = None,
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction, require=("moderate_members",))
        self.throttle("untimeout", interaction.guild_id, interaction.user.id)

        guild = interaction.guild
        reason_text = require_text(reason, "Timeout lifted")
        self.assert_hierarchy(interaction.user, target, guild.me, verb="release")

        if target.timed_out_until is None:
            raise ZagrosError(f"**{target}** is not currently timed out.")

        case = await self._record(
            interaction, CaseAction.UNTIMEOUT, target.id, reason_text
        )
        try:
            await target.timeout(None)
        except discord.Forbidden as exc:
            raise PermissionDeniedError(
                f"Discord refused to release **{target}**. I need "
                "**Moderate Members** and a role above theirs."
            ) from exc

        # The original timeout is now void, so mark it inactive rather than
        # leaving an active row that /infractions would still count. Scoped to
        # TIMEOUT on purpose: lifting a timeout must not quietly clear an active
        # warning or mute, or the member's history reads clean while the other
        # sanction is still live.
        await revoke_cases(
            interaction.guild_id,
            user_id=target.id,
            actions=(CaseAction.TIMEOUT,),
            moderator_id=interaction.user.id,
            reason=f"Lifted early by {interaction.user}",
        )

        await self._announce(
            interaction,
            target,
            action="Timeout lifted",
            reason_text=reason_text,
            case_ref=case.case_ref if case else None,
        )
        await self.reply(
            interaction,
            success_embed(
                f"**{target}** can post and act again.",
                title="Timeout lifted",
                author=interaction.user,
            ),
        )

    # ------------------------------------------------------------------ #
    # /warns
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="warns",
        description="Show a member's active warnings.",
    )
    @app_commands.guild_only
    @app_commands.describe(target="The member to look up.")
    async def warns(
        self, interaction: discord.Interaction, target: discord.Member
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction)

        rows = await fetch_case_history(
            interaction.guild_id,
            user_id=target.id,
            action=CaseAction.WARN.value,
            limit=HISTORY_LIMIT,
        )
        active = [row for row in rows if row.is_active]

        embed = base_embed(
            title=f"Warnings · {target}",
            colour=COLOR_WARNING if active else COLOR_SUCCESS,
            author=interaction.user,
            thumbnail=target.display_avatar.url,
        )
        embed.description = (
            f"**{len(active)}** active warning(s) "
            f"out of {len(rows)} on record."
        )
        ladder = await self._ladder(interaction, target)
        if ladder:
            embed.add_field(name="Warn ladder", value=ladder, inline=False)
        for row in active[:10]:
            embed.add_field(
                name=(
                    f"{row.case_ref} · "
                    f"{discord.utils.format_dt(row.created_at, 'R') if row.created_at else ''}"
                ),
                value=truncate(row.reason or NO_REASON, 300),
                inline=False,
            )
        if len(active) > 10:
            embed.add_field(
                name="Older",
                value=f"{len(active) - 10} more — see `/cases` for the full log.",
                inline=False,
            )
        await self.reply(interaction, embed)

    # ------------------------------------------------------------------ #
    # /clearwarns
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="clearwarns",
        description="Revoke a member's warnings. The record is kept, not erased.",
    )
    @app_commands.guild_only
    @app_commands.describe(
        target="The member whose warnings should be revoked.",
        case_id="Revoke only this case. Omit to revoke every active warning.",
        reason="Why they are being cleared.",
    )
    async def clearwarns(
        self,
        interaction: discord.Interaction,
        target: discord.Member,
        case_id: app_commands.Range[str, 1, 32] | None = None,
        reason: app_commands.Range[str, 1, 512] | None = None,
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction)
        self.throttle("clearwarns", interaction.guild_id, interaction.user.id)

        reason_text = require_text(reason, "Warnings cleared")
        case_ref: str | None = None

        if case_id is not None:
            found = await find_case(interaction.guild_id, case_id)
            if found is None or found.action != CaseAction.WARN.value:
                raise ZagrosError(f"No warning case `{case_id}` found in this server.")
            if found.user_id != target.id:
                raise ZagrosError(
                    f"`{found.case_ref}` belongs to a different member."
                )
            if not found.is_active:
                raise ZagrosError(f"`{found.case_ref}` was already revoked.")
            case_ref = found.case_ref

        revoked = await revoke_cases(
            interaction.guild_id,
            user_id=target.id,
            case_ref=case_ref,
            moderator_id=interaction.user.id,
            reason=reason_text,
        )
        if not revoked:
            raise ZagrosError(f"**{target}** has no active warnings to clear.")

        embed = success_embed(
            f"Revoked **{revoked}** warning(s) from **{target}**.\n"
            f"The ledger still records them as revoked by {interaction.user} — "
            "use `/viewcase` to see who and why.",
            title="Warnings cleared",
            author=interaction.user,
        )
        if case_ref:
            embed.add_field(name="Scope", value=f"only `{case_ref}`", inline=True)
        await self.reply(interaction, embed)

    # ------------------------------------------------------------------ #
    # /infractions
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="infractions",
        description="Every active action against a member, grouped by type.",
    )
    @app_commands.guild_only
    @app_commands.describe(target="The member to look up.")
    async def infractions(
        self, interaction: discord.Interaction, target: discord.Member
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction)

        rows = await fetch_case_history(
            interaction.guild_id, user_id=target.id, limit=HISTORY_LIMIT
        )
        active = [row for row in rows if row.is_active]
        by_action: dict[str, list[ModCase]] = {}
        for row in active:
            by_action.setdefault(row.action, []).append(row)

        embed = base_embed(
            title=f"Infractions · {target}",
            author=interaction.user,
            thumbnail=target.display_avatar.url,
        )
        embed.description = (
            f"**{len(active)}** active · **{len(rows)}** total on record."
        )
        for action, cases in sorted(by_action.items(), key=lambda kv: -len(kv[1])):
            newest = cases[0]
            when = (
                discord.utils.format_dt(newest.created_at, "R")
                if newest.created_at
                else "unknown"
            )
            embed.add_field(
                name=f"{action.replace('_', ' ').title()} · {len(cases)}",
                value=f"Last: {when}\n{truncate(newest.reason or NO_REASON, 240)}",
                inline=False,
            )
        if not active:
            embed.add_field(
                name="Clean",
                value="No active infractions on record.", inline=False
            )
        await self.reply(interaction, embed)

    # ------------------------------------------------------------------ #
    # /reason
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="reason",
        description="Rewrite the reason on a past case.",
    )
    @app_commands.guild_only
    @app_commands.describe(
        case_id="The case reference, e.g. ZEYE-000042.",
        new_reason="The corrected reason.",
    )
    async def reason(
        self,
        interaction: discord.Interaction,
        case_id: str,
        new_reason: app_commands.Range[str, 1, 512],
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction)

        found = await find_case(interaction.guild_id, case_id)
        if found is None:
            raise ZagrosError(f"No case matching `{case_id}` in this server.")

        case_ref = found.case_ref
        action_label = found.action
        old = found.reason or NO_REASON
        new_text = truncate(new_reason.strip(), 2000)

        # Re-fetched inside this session on purpose. `found` came from a session
        # that has since closed, so it is detached: assigning to it and calling
        # session.add() re-attaches the whole row, and a detached instance is
        # exactly what SQLAlchemy raises on. Looking it up again by case_ref
        # edits one column on a live identity-mapped instance.
        db = get_database()
        async with db.session() as session:
            row = (
                await session.execute(
                    select(ModCase).where(
                        ModCase.guild_id == interaction.guild_id,
                        ModCase.case_ref == case_ref,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                raise ZagrosError(f"`{case_ref}` vanished while I was editing it.")
            row.reason = new_text
            await session.commit()

        await self.reply(
            interaction,
            warning_embed(
                f"**{case_ref}** ({action_label}) reason rewritten.\n"
                f"Was: {truncate(old, 300)}\n"
                f"Now: {truncate(new_text, 300)}",
                title="Case reason edited",
                author=interaction.user,
            ),
        )

    # ------------------------------------------------------------------ #
    # /notes
    # ------------------------------------------------------------------ #
    notes_group = app_commands.Group(
        name="notes",
        description="Context about a member that is not an infraction.",
    )

    @notes_group.command(name="add", description="Attach a private note to a member.")
    @app_commands.guild_only
    @app_commands.describe(target="The member.", text="The note.")
    async def notes_add(
        self,
        interaction: discord.Interaction,
        target: discord.Member,
        text: app_commands.Range[str, 1, 1000],
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction)
        await self._write_note(interaction, target, text)

    @notes_group.command(name="view", description="Read a member's notes.")
    @app_commands.guild_only
    @app_commands.describe(target="The member.")
    async def notes_view(
        self, interaction: discord.Interaction, target: discord.Member
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction)

        from core.models import GuildNote

        db = get_database()
        async with db.session() as session:
            rows = list(
                (
                    await session.execute(
                        select(GuildNote)
                        .where(
                            GuildNote.guild_id == interaction.guild_id,
                            GuildNote.user_id == target.id,
                        )
                        .order_by(GuildNote.created_at.desc())
                        .limit(25)
                    )
                ).scalars()
            )

        embed = base_embed(
            title=f"Notes · {target}",
            author=interaction.user,
            thumbnail=target.display_avatar.url,
        )
        if not rows:
            embed.description = "No notes recorded."
        for note in rows:
            author = interaction.guild.get_member(note.author_id)
            when = (
                discord.utils.format_dt(note.created_at, "R")
                if note.created_at
                else ""
            )
            embed.add_field(
                name=f"{author or note.author_id} · {when}",
                value=truncate(note.body, 400),
                inline=False,
            )
        await self.reply(interaction, embed)

    async def _write_note(
        self,
        interaction: discord.Interaction,
        target: discord.Member,
        text: str,
    ) -> None:
        from core.models import GuildNote

        db = get_database()
        async with db.session() as session:
            session.add(
                GuildNote(
                    guild_id=interaction.guild_id,
                    user_id=target.id,
                    author_id=interaction.user.id,
                    body=truncate(text.strip(), 1000),
                )
            )
            await session.commit()
        await self.reply(
            interaction,
            success_embed(
                f"Note attached to **{target}**.", author=interaction.user
            ),
        )

    # ------------------------------------------------------------------ #
    # /cases
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="cases",
        description="Browse the guild's case ledger.",
    )
    @app_commands.guild_only
    @app_commands.describe(
        page="Page number (each page holds 10 cases).",
        action="Only show this action type.",
        include_revoked="Include revoked cases in the listing.",
    )
    async def cases(
        self,
        interaction: discord.Interaction,
        page: app_commands.Range[int, 1, 100] = 1,
        action: str | None = None,
        include_revoked: bool = True,
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction)

        action_filter = action.strip().lower() if action else None
        if action_filter:
            action_filter = action_filter.replace(" ", "_")

        total = await count_case_history(
            interaction.guild_id,
            action=action_filter,
            include_revoked=include_revoked,
        )
        pages = max(1, -(-total // CASES_PER_PAGE))
        page = min(page, pages)

        rows = await fetch_case_history(
            interaction.guild_id,
            action=action_filter,
            include_revoked=include_revoked,
            limit=CASES_PER_PAGE,
            offset=(page - 1) * CASES_PER_PAGE,
        )

        embed = base_embed(
            title=f"Cases · page {page}/{pages}",
            author=interaction.user,
        )
        embed.description = f"**{total}** matching case(s)."
        if not rows:
            embed.add_field(name="Empty", value="Nothing on record.", inline=False)
        for row in rows:
            status = "" if row.is_active else " · **revoked**"
            when = (
                discord.utils.format_dt(row.created_at, "R") if row.created_at else ""
            )
            embed.add_field(
                name=f"`{row.case_ref}` {row.action}{status}",
                value=(
                    f"<@{row.user_id}> · {when}\n"
                    f"{truncate(row.reason or NO_REASON, 220)}"
                ),
                inline=False,
            )
        await self.reply(interaction, embed)

    # ------------------------------------------------------------------ #
    # /viewcase
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="viewcase",
        description="Show one case in full.",
    )
    @app_commands.guild_only
    @app_commands.describe(case_id="The case reference, e.g. ZEYE-000042.")
    async def viewcase(
        self, interaction: discord.Interaction, case_id: str
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction)

        found = await find_case(interaction.guild_id, case_id)
        if found is None:
            raise ZagrosError(f"No case matching `{case_id}` in this server.")

        guild = interaction.guild
        target = guild.get_member(found.user_id)
        moderator = guild.get_member(found.moderator_id)

        embed = base_embed(
            title=f"{found.case_ref} · {found.action.replace('_', ' ').title()}",
            colour=COLOR_WARNING if not found.is_active else None,
            author=interaction.user,
            thumbnail=target.display_avatar.url if target else None,
        )
        embed.add_field(
            name="Member",
            value=(
                describe_member(target)
                if target
                else f"<@{found.user_id}> (not in server)"
            ),
            inline=True,
        )
        embed.add_field(
            name="Moderator",
            value=(
                f"**{moderator}**" if moderator else f"`{found.moderator_id}`"
            ),
            inline=True,
        )
        embed.add_field(
            name="When",
            value=(
                timestamp(found.created_at) if found.created_at else "unknown"
            ),
            inline=True,
        )
        embed.add_field(
            name="Reason", value=truncate(found.reason or NO_REASON, 1000), inline=False
        )
        if found.expires_at:
            embed.add_field(
                name="Expires",
                value=(
                    f"{timestamp(found.expires_at)} "
                    f"({discord.utils.format_dt(found.expires_at, 'R')})"
                ),
                inline=True,
            )
        if not found.is_active:
            revoker = guild.get_member(found.revoked_by) if found.revoked_by else None
            embed.add_field(
                name="Revoked",
                value=(
                    f"by **{revoker or found.revoked_by}** — "
                    f"{truncate(found.revoked_reason or 'no reason', 500)}"
                ),
                inline=False,
            )
        await self.reply(interaction, embed)

    # ------------------------------------------------------------------ #
    # /unban
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="unban",
        description="Lift a ban.",
    )
    @app_commands.guild_only
    @app_commands.describe(
        user_id="The banned user's id. Bans cannot be resolved by name.",
        reason="Why the ban is being lifted.",
    )
    async def unban(
        self,
        interaction: discord.Interaction,
        user_id: str,
        reason: app_commands.Range[str, 1, 512] | None = None,
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction, require=("ban_members",))
        self.throttle("unban", interaction.guild_id, interaction.user.id)

        guild = interaction.guild
        self.assert_bot_permissions(guild.me, None, ("ban_members",), verb="unban")

        cleaned = user_id.strip().strip("<@!>")
        if not cleaned.isdigit() or len(cleaned) < DISCORD_MIN_ID:
            raise ZagrosError(
                "That is not a user id. Discord does not let a bot look up a banned "
                "user by name, so paste the numeric id from the ban entry."
            )
        target_id = int(cleaned)

        try:
            ban = await guild.fetch_ban(discord.Object(id=target_id))
        except discord.NotFound:
            raise ZagrosError(f"**{target_id}** is not banned in this server.") from None
        except discord.Forbidden:
            raise PermissionDeniedError(
                "I cannot read the ban list here. I need **Ban Members**."
            ) from None

        reason_text = require_text(reason, "Ban lifted")
        case = await self._record(
            interaction, CaseAction.UNBAN, target_id, reason_text
        )

        try:
            await guild.unban(
                discord.Object(id=target_id),
                reason=build_audit_reason(None, interaction.user, reason_text),
            )
        except discord.Forbidden as exc:
            raise PermissionDeniedError(
                "Discord refused the unban. I need **Ban Members** here."
            ) from exc

        await revoke_cases(
            interaction.guild_id,
            user_id=target_id,
            case_ref=None,
            moderator_id=interaction.user.id,
            reason=f"Ban lifted by {interaction.user}",
        )

        subject = ban.user or discord.Object(id=target_id)
        await log_action(
            guild,
            punishment_embed(
                action="Unban",
                guild=guild,
                member=subject,
                moderator=interaction.user,
                reason=reason_text,
                case_ref=case.case_ref if case else None,
            ),
        )

        # DM only when the ban came with a message, so the moderator is not
        # silently broadcasting an unban to someone they already banned.
        if ban.user is not None:
            await notify_member(
                ban.user,
                dm_notice_embed(
                    action="unbanned",
                    guild=guild,
                    reason=reason_text,
                    case_ref=case.case_ref if case else None,
                    moderator=interaction.user,
                ),
            )

        await self.reply(
            interaction,
            success_embed(
                f"**{subject}** (`{target_id}`) can rejoin.",
                title="Ban lifted",
                author=interaction.user,
            ),
        )

    # ------------------------------------------------------------------ #
    # /banlist
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="banlist",
        description="Show the current bans with their reasons.",
    )
    @app_commands.guild_only
    @app_commands.describe(page="Page number (10 bans per page).")
    async def banlist(
        self, interaction: discord.Interaction, page: app_commands.Range[int, 1, 50] = 1
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction, require=("ban_members",))
        self.throttle("banlist", interaction.guild_id, interaction.user.id)

        guild = interaction.guild
        self.assert_bot_permissions(guild.me, None, ("ban_members",), verb="read bans")

        try:
            bans = await guild.fetch_bans()
        except discord.Forbidden as exc:
            raise PermissionDeniedError(
                "I cannot read the ban list here. I need **Ban Members**."
            ) from exc
        except discord.HTTPException as exc:
            raise ZagrosError(f"Discord would not return the ban list: {exc}") from exc

        per_page = 10
        pages = max(1, -(-len(bans) // per_page))
        page = min(page, pages)
        window = bans[(page - 1) * per_page : page * per_page]

        embed = base_embed(
            title=f"Bans · page {page}/{pages}",
            author=interaction.user,
        )
        embed.description = f"**{len(bans)}** active ban(s)."
        if not window:
            embed.add_field(name="Empty", value="Nobody is banned.", inline=False)
        for entry in window:
            user = entry.user
            name = f"**{user}**" if user else f"`{entry.user.id}`"
            embed.add_field(
                name=name,
                value=(
                    f"`{entry.user.id}`\n"
                    f"Reason: {truncate(entry.reason or 'No reason provided', 200)}"
                ),
                inline=False,
            )
        await self.reply(interaction, embed)

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    async def _record(
        self,
        interaction: discord.Interaction,
        action: CaseAction,
        user_id: int,
        reason: str,
        expires_at: datetime | None = None,
    ) -> ModCase | None:
        """Write a ledger row; ``None`` if the ledger is down (action still went through)."""
        try:
            return await record_case(
                guild_id=interaction.guild_id,
                user_id=user_id,
                moderator_id=interaction.user.id,
                action=action,
                reason=reason,
                expires_at=expires_at,
            )
        except ZagrosError as exc:
            logger.warning("Case write failed for %s: %s", action.value, exc.user_message)
            return None

    async def _retract(self, interaction: discord.Interaction, case: ModCase | None) -> None:
        """Void a ledger row written for an action that Discord then refused.

        Called when the API call fails *after* the case was allocated: without
        this the ledger would claim a ban that never happened.
        """
        if case is None:
            return
        try:
            case.is_active = False
            case.revoked_by = interaction.user.id
            case.revoked_reason = "Action failed; row retracted"
            db = get_database()
            async with db.session() as session:
                session.add(case)
                await session.commit()
        except Exception as exc:  # noqa: BLE001 - best-effort audit repair
            logger.error("Could not retract case %s: %s", case.case_ref, exc)

    async def _announce(
        self,
        interaction: discord.Interaction,
        target: discord.Member,
        *,
        action: str,
        reason_text: str,
        case_ref: str | None,
        duration: timedelta | None = None,
    ) -> None:
        """Log to the mod channel and DM the member, each guarded."""
        guild = interaction.guild
        await log_action(
            guild,
            punishment_embed(
                action=action,
                guild=guild,
                member=target,
                moderator=interaction.user,
                reason=reason_text,
                case_ref=case_ref,
                duration=duration,
            ),
        )
        await notify_member(
            target,
            dm_notice_embed(
                action=action.lower(),
                guild=guild,
                reason=reason_text,
                case_ref=case_ref,
                duration=duration,
                moderator=interaction.user,
            ),
        )

    async def _ladder(
        self, interaction: discord.Interaction, target: discord.Member
    ) -> str:
        """Describe where this member sits on the guild's warn ladder."""
        config = await get_or_create_guild_config(interaction.guild_id)
        if config is None:
            return ""
        active = await count_active_warnings(interaction.guild_id, target.id)
        lines = [f"Currently **{active}** active warning(s)."]
        if config.warn_timeout_at:
            lines.append(
                f"At **{config.warn_timeout_at}** a "
                f"{config.warn_timeout_minutes}m timeout is applied."
            )
        if config.warn_kick_at:
            lines.append(f"At **{config.warn_kick_at}** the member is kicked.")
        return "\n".join(lines)


async def setup(bot: commands.Bot) -> None:
    """Extension entrypoint."""
    await bot.add_cog(Infractions(bot))
    logger.info("Infractions cog loaded")


#: Minimum plausible snowflake width, used to sanity-check pasted user ids.
DISCORD_MIN_ID: Final[int] = 15
