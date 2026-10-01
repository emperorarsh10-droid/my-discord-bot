"""Member and server management â€— the everyday moderator's toolkit.

Everything here acts on a person or a role rather than on message history:
``/nick``, ``/role``, ``/whois``, ``/avatar``, ``/verify``, ``/altcheck``,
``/strip``, ``/temprole``, ``/roleall``, ``/dm`` and ``/modstats``.

The three that can touch hundreds of members at once â€— ``/strip``, ``/temprole``'s
sweeper and ``/roleall`` â€— all follow the same shape:

*   **Role hierarchy is checked per member, not once.** A moderator cannot strip
    an admin by pointing ``/roleall`` at a role the admin also holds.
*   **Batches are throttled and resumable.** Discord's REST bucket is global, so
    an unthrottled sweep starves every other command in the process. The sweep
    edits one message with live progress rather than opening one request per
    member.
*   **Nothing is granted that the bot could not take back.** ``/roleall`` and
    ``/temprole`` record what they did, so ``/strip`` and the expiry sweeper have
    an accurate list to undo.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

import discord
from discord import app_commands
from discord.ext import commands, tasks
from sqlalchemy import Select, func, select

from cogs._base import FeatureCog, require_text
from core.database import get_database
from core.embeds import (
    COLOR_INFO,
    base_embed,
    describe_member,
    describe_role,
    format_duration,
    success_embed,
    timestamp,
    truncate,
    warning_embed,
)
from core.errors import HierarchyError, PermissionDeniedError, ZagrosError
from core.logging_setup import command_context, get_logger
from core.models import (
    CaseAction,
    GuildIgnore,
    ModCase,
    TempRoleGrant,
    utcnow,
)
from core.ratelimit import RateLimit
from core.services import (
    build_audit_reason,
    get_setting,
    log_action,
    record_case,
)
from core.transformers import Duration

logger = get_logger("zagrosian.cog.membertools")

#: Pause between bulk role operations. See the module docstring: the REST bucket
#: is shared across the whole bot.
SWEEP_DELAY: Final[float] = 0.6

#: Roles never stripped by ``/roleall``/``/strip`` regardless of position.
PROTECTED_ROLE_NAMES: Final[frozenset[str]] = frozenset(
    {"admin", "administrator", "owner", "bot", "staff", "moderator", "mod", "mods"}
)

#: Accounts younger than this get flagged by ``/altcheck``.
ALT_SUSPICIOUS_DAYS: Final[int] = 14

#: The key holding the verification role id, so ``/verify`` survives a restart.
VERIFY_ROLE_KEY: Final[str] = "verify.role_id"


class MemberTools(FeatureCog):
    """Member, role and account utilities."""

    feature_name = "membertools"
    bulk_limit = RateLimit(max_calls=5, window=60.0)

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__(bot)
        self.temp_role_sweeper.start()

    def cog_unload(self) -> None:
        self.temp_role_sweeper.cancel()

    # ------------------------------------------------------------------ #
    # /nick
    # ------------------------------------------------------------------ #
    @app_commands.command(name="nick", description="Set or clear a member's nickname.")
    @app_commands.guild_only
    @app_commands.describe(
        target="The member to rename.",
        nickname="The new nickname. Leave empty to clear it.",
    )
    async def nick(
        self,
        interaction: discord.Interaction,
        target: discord.Member,
        nickname: app_commands.Range[str, 1, 32] | None = None,
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction, required=("manage_nicknames",))

        guild = interaction.guild
        self.assert_bot_permissions(guild.me, None, ("manage_nicknames",), verb="rename members")
        self.assert_hierarchy(interaction.user, target, guild.me, verb="rename")

        # Discord allows the bot to clear a nickname it did not set, but not to
        # set one above a member whose top role outranks it.
        if nickname and target.top_role >= guild.me.top_role:
            raise HierarchyError(
                f"**{target}** holds `{target.top_role.name}`, at or above my highest "
                "role, so I cannot rename them. Move my role up first."
            )

        before = target.nick
        try:
            await target.edit(
                nick=nickname,
                reason=build_audit_reason(None, interaction.user, "Nickname change"),
            )
        except discord.Forbidden as exc:
            raise PermissionDeniedError(
                "Discord refused the nickname change. I need **Manage Nicknames** "
                "and a role above theirs."
            ) from exc
        except discord.HTTPException as exc:
            raise ZagrosError(f"Discord rejected the nickname: {exc}") from exc

        await log_action(
            guild,
            base_embed(
                title="Nickname changed",
                author=interaction.user,
                description=(
                    f"**{target}** (`{target.id}`)\n"
                    f"**Before:** {before or '*none*'}\n"
                    f"**After:** {nickname or '*none (cleared)*'}"
                ),
            ),
        )
        await self.reply(
            interaction,
            success_embed(
                f"**{target}** is now `{nickname}`."
                if nickname
                else f"Cleared the nickname on **{target}**; they show as `{target.name}` again.",
                title="Nickname updated",
                author=interaction.user,
            ),
        )

    # ------------------------------------------------------------------ #
    # /role
    # ------------------------------------------------------------------ #
    @app_commands.command(name="role", description="Give or take a role from a member.")
    @app_commands.guild_only
    @app_commands.describe(
        target="The member.",
        role="The role to add or remove.",
        action="Whether to add the role or take it away.",
    )
    async def role(
        self,
        interaction: discord.Interaction,
        target: discord.Member,
        role: discord.Role,
        action: Literal["add", "remove"] = "add",
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction, required=("manage_roles",))

        guild = interaction.guild
        self.assert_bot_permissions(guild.me, None, ("manage_roles",), verb="change roles")
        self.assert_hierarchy(interaction.user, target, guild.me, verb="change roles for")

        if not role.is_assignable():
            raise PermissionDeniedError(
                f"`{role.name}` is managed by an integration â€— a bot role or a "
                "subscription tier â€— and cannot be assigned by hand."
            )
        if role >= guild.me.top_role:
            raise HierarchyError(
                f"`{role.name}` sits at or above my highest role "
                f"(`{guild.me.top_role.name}`), so I could never assign it. "
                "Move my role above it first."
            )
        if role == guild.default_role:
            raise ZagrosError(
                "The `@everyone` role is automatic and cannot be added or removed."
            )

        if action == "add":
            if role in target.roles:
                raise ZagrosError(f"**{target}** already holds {describe_role(role)}.")
            await target.add_roles(
                role, reason=build_audit_reason(None, interaction.user, "Role add")
            )
            case_action = CaseAction.ROLE_ADD
            verb = "gave"
        else:
            if role not in target.roles:
                raise ZagrosError(f"**{target}** does not hold {describe_role(role)}.")
            await target.remove_roles(
                role, reason=build_audit_reason(None, interaction.user, "Role remove")
            )
            case_action = CaseAction.ROLE_REMOVE
            verb = "removed"

        await record_case(
            guild_id=interaction.guild_id,
            user_id=target.id,
            moderator_id=interaction.user.id,
            action=case_action,
            reason=f"{action} {role.name}",
        )
        await log_action(
            guild,
            base_embed(
                title=f"Role {action}",
                author=interaction.user,
                description=(
                    f"{verb} {describe_role(role)}\n"
                    f"**Member:** {describe_member(target)}"
                ),
            ),
        )
        await self.reply(
            interaction,
            success_embed(
                f"**{verb.capitalize()}** {describe_role(role)} "
                f"{'to' if action == 'add' else 'from'} {target.display_mention}.",
                title="Role updated",
                author=interaction.user,
            ),
        )

    # ------------------------------------------------------------------ #
    # /whois
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="whois",
        description="Everything about a member: account age, roles, permissions and key dates.",
    )
    @app_commands.guild_only
    @app_commands.describe(target="The member to investigate.")
    async def whois(
        self, interaction: discord.Interaction, target: discord.Member
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction)

        guild = interaction.guild
        now = datetime.now(UTC)

        created = target.created_at.replace(tzinfo=UTC) if target.created_at else now
        joined = target.joined_at.replace(tzinfo=UTC) if target.joined_at else now
        account_age = now - created
        in_server = now - joined

        roles = sorted(
            (r for r in target.roles if r != guild.default_role),
            key=lambda r: r.position,
            reverse=True,
        )
        perms = target.guild_permissions

        embed = base_embed(
            title=f"{target}",
            author=interaction.user,
            thumbnail=target.display_avatar.url,
            description=(
                f"{describe_member(target)}\n"
                f"{len(roles)} role(s) Â· "
                f"joined **{format_duration(in_server)}** ago"
            ),
        )
        embed.add_field(
            name="Account",
            value=(
                f"Created: {timestamp(created)} ({format_duration(account_age)} ago)\n"
                f"ID: `{target.id}`\n"
                f"Bot: {'yes' if target.bot else 'no'}"
            ),
            inline=False,
        )
        embed.add_field(
            name="Notable permissions",
            value=truncate(
                ", ".join(
                    name.replace("_", " ").title()
                    for name, value in (
                        ("administrator", perms.administrator),
                        ("manage_guild", perms.manage_guild),
                        ("manage_roles", perms.manage_roles),
                        ("manage_channels", perms.manage_channels),
                        ("manage_webhooks", perms.manage_webhooks),
                        ("ban_members", perms.ban_members),
                        ("kick_members", perms.kick_members),
                        ("moderate_members", perms.moderate_members),
                        ("manage_messages", perms.manage_messages),
                        ("mention_everyone", perms.mention_everyone),
                    )
                    if value
                )
                or "None",
                1000,
            ),
            inline=False,
        )
        if roles:
            embed.add_field(
                name=f"Roles ({len(roles)})",
                value=truncate(", ".join(r.mention for r in roles[:40]), 1024),
                inline=False,
            )
        if target.premium_since:
            embed.add_field(
                name="Boosting since", value=timestamp(target.premium_since), inline=True
            )
        if target.timed_out_until and target.timed_out_until > now:
            embed.add_field(
                name="Timed out until",
                value=timestamp(target.timed_out_until),
                inline=True,
            )

        await self.reply(interaction, embed)

    # ------------------------------------------------------------------ #
    # /avatar
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="avatar",
        description="Show a member's avatar at full resolution, and the server's.",
    )
    @app_commands.guild_only
    @app_commands.describe(
        target="The member. Omit for the server icon.",
        user="Set to true to show the user's global avatar instead of their server one.",
    )
    async def avatar(
        self,
        interaction: discord.Interaction,
        target: discord.Member | None = None,
        user: bool = False,
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction)

        guild = interaction.guild

        if target is None:
            icon = guild.icon
            if icon is None:
                raise ZagrosError(f"**{guild.name}** has no server icon set.")
            # replace(size=4096) re-derives the CDN URL at Discord's maximum, so
            # the moderator gets the original file rather than a 128px thumbnail.
            embed = base_embed(
                title=f"{guild.name} Â· server icon",
                author=interaction.user,
                thumbnail=icon.replace(size=4096).url,
                colour=guild.accent_colour.value if guild.accent_colour else COLOR_INFO,
            )
            embed.set_image(url=icon.replace(size=4096).url)
            await self.reply(interaction, embed)
            return

        avatar_obj = target.display_avatar if user else (
            target.avatar or target.default_avatar
        )
        url = avatar_obj.replace(size=4096).url
        embed = base_embed(
            title=f"{target} Â· avatar",
            author=interaction.user,
            thumbnail=url,
        )
        embed.set_image(url=url)
        embed.add_field(
            name="Source",
            value=(
                "Global (user) avatar"
                if user and target.avatar
                else "Server-specific avatar"
                if target.avatar
                else "Default avatar"
            ),
            inline=True,
        )
        await self.reply(interaction, embed)

    # ------------------------------------------------------------------ #
    # /verify
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="verify",
        description="Mark a member as verified by granting (or removing) the verify role.",
    )
    @app_commands.guild_only
    @app_commands.describe(
        target="The member to verify.",
        revoke="Set to true to remove the verified role instead.",
    )
    async def verify(
        self,
        interaction: discord.Interaction,
        target: discord.Member,
        revoke: bool = False,
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction, required=("manage_roles",))

        guild = interaction.guild
        raw_role = await get_setting(interaction.guild_id, VERIFY_ROLE_KEY)
        if not raw_role or not raw_role.isdigit():
            raise ZagrosError(
                "No verification role is configured. Set one with "
                "`/settings key:verify.role_id` â€— or grant the member the role "
                "directly with `/role`."
            )
        role = guild.get_role(int(raw_role))
        if role is None:
            raise ZagrosError(
                f"The configured verification role `{raw_role}` no longer exists in "
                "this server. Update it with `/settings`."
            )

        self.assert_bot_permissions(guild.me, None, ("manage_roles",), verb="verify members")
        if role >= guild.me.top_role:
            raise HierarchyError(
                f"The verify role `{role.name}` is at or above my highest role, so I "
                "could never assign it."
            )

        if revoke:
            if role not in target.roles:
                raise ZagrosError(f"**{target}** does not hold the verify role.")
            await target.remove_roles(
                role,
                reason=build_audit_reason(None, interaction.user, "Verification revoked"),
            )
            verb, label = "Revoked verification for", "Verification revoked"
        else:
            if role in target.roles:
                raise ZagrosError(f"**{target}** is already verified.")
            await target.add_roles(
                role, reason=build_audit_reason(None, interaction.user, "Verified")
            )
            verb, label = "Verified", "Member verified"

        await log_action(
            guild,
            success_embed(
                f"**{verb}** {target.mention} ({describe_role(role)})",
                author=interaction.user,
            ),
        )
        await self.reply(
            interaction,
            success_embed(f"**{verb}** {target.mention}.", title=label, author=interaction.user),
        )

    # ------------------------------------------------------------------ #
    # /altcheck
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="altcheck",
        description="Look for accounts registered around the same time â€— possible alts.",
    )
    @app_commands.guild_only
    @app_commands.describe(
        target="The account to investigate.",
        window_days="How far back to search.",
    )
    async def altcheck(
        self,
        interaction: discord.Interaction,
        target: discord.Member,
        window_days: app_commands.Range[int, 1, 365] = 30,
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction)

        guild = interaction.guild
        if not target.created_at:
            raise ZagrosError("Discord did not give me an account creation date.")

        created = target.created_at.replace(tzinfo=UTC)
        cutoff = created - timedelta(days=window_days)

        suspects = [
            member
            for member in guild.members
            if member.id != target.id
            and member.created_at is not None
            and member.joined_at is not None
            and cutoff
            <= member.created_at.replace(tzinfo=UTC)
            <= created + timedelta(days=window_days)
            and member.joined_at.replace(tzinfo=UTC) >= created
        ]

        # Ranking by how close the join date is to the target's account creation
        # date is what separates "registered around the same time" from
        # "registered a year apart and joined later", which is most of a server.
        suspects.sort(
            key=lambda m: abs((m.joined_at.replace(tzinfo=UTC) - created).total_seconds())
        )

        embed = base_embed(
            title=f"Alt check Â· {target}",
            author=interaction.user,
            thumbnail=target.display_avatar.url,
            description=(
                f"**{target}** was created **{format_duration(datetime.now(UTC) - created)}** ago. "
                f"Searching **{window_days}** day(s) either side of that date."
            ),
        )
        if not suspects:
            embed.add_field(
                name="No candidates",
                value="Nobody else joined within that window of the account being created.",
                inline=False,
            )
        else:
            embed.add_field(
                name=f"{len(suspects)} possible alt(s)",
                value=truncate(
                    "\n".join(
                        f"**{m}** (`{m.id}`) â€— created {timestamp(m.created_at)}, "
                        f"joined {timestamp(m.joined_at)}"
                        for m in suspects[:20]
                    ),
                    1024,
                ),
                inline=False,
            )
            embed.set_footer(
                text="Accounts registered close together and joining close together "
                "is a signal, not proof. Check the account's own activity."
            )
        await self.reply(interaction, embed)

    # ------------------------------------------------------------------ #
    # /strip
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="strip",
        description="Remove roles from a member â€— by name, or all of them.",
    )
    @app_commands.guild_only
    @app_commands.describe(
        target="The member to strip.",
        mode="'named' removes only the roles you list; 'all' removes every assignable role.",
        roles="Role names to remove. Only used when mode is 'named'.",
    )
    async def strip(
        self,
        interaction: discord.Interaction,
        target: discord.Member,
        mode: Literal["named", "all"] = "named",
        roles: str | None = None,
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction, required=("manage_roles",))
        self.throttle("strip", interaction.guild_id, interaction.user.id)

        guild = interaction.guild
        self.assert_bot_permissions(guild.me, None, ("manage_roles",), verb="strip roles")
        self.assert_hierarchy(interaction.user, target, guild.me, verb="strip roles from")

        if target.top_role >= guild.me.top_role:
            raise HierarchyError(
                f"**{target}** holds `{target.top_role.name}`, at or above my highest "
                "role, so I cannot remove anything from them."
            )

        if mode == "all":
            candidates = [
                r
                for r in target.roles
                if r != guild.default_role
                and r < guild.me.top_role
                and r.is_assignable()
                and r.name.lower() not in PROTECTED_ROLE_NAMES
            ]
        else:
            if not roles:
                raise ZagrosError(
                    "Name the roles to strip, or use `mode: all` to remove "
                    "everything assignable."
                )
            wanted = {part.strip().lower() for part in roles.split(",") if part.strip()}
            if not wanted:
                raise ZagrosError("No role names were readable in that list.")
            found = {r.name.lower(): r for r in target.roles}
            missing = wanted - found.keys()
            if missing:
                raise ZagrosError(
                    f"**{target}** does not hold: {', '.join(sorted(missing))}."
                )
            candidates = [
                found[name]
                for name in wanted
                if found[name] < guild.me.top_role
                and found[name].is_assignable()
                and found[name].name.lower() not in PROTECTED_ROLE_NAMES
            ]

        if not candidates:
            raise ZagrosError(
                "Nothing to strip: every matching role is protected, managed by an "
                "integration, or above my own role."
            )

        try:
            await target.remove_roles(
                *candidates,
                reason=build_audit_reason(None, interaction.user, "Role strip"),
            )
        except discord.Forbidden as exc:
            raise PermissionDeniedError(
                "Discord refused the role removal. I need **Manage Roles** and to "
                "sit above every role being removed."
            ) from exc

        await record_case(
            guild_id=interaction.guild_id,
            user_id=target.id,
            moderator_id=interaction.user.id,
            action=CaseAction.ROLE_REMOVE,
            reason=(
                    f"strip {len(candidates)} role(s): "
                    f"{', '.join(r.name for r in candidates)[:400]}"
                ),
        )
        await log_action(
            guild,
            base_embed(
                title="Roles stripped",
                author=interaction.user,
                description=(
                    f"**{target}** (`{target.id}`)\n"
                    f"Removed {len(candidates)} role(s): "
                    f"{truncate(', '.join(r.name for r in candidates), 900)}"
                ),
            ),
        )
        await self.reply(
            interaction,
            success_embed(
                f"Removed **{len(candidates)}** role(s) from {target.mention}.",
                title="Roles stripped",
                author=interaction.user,
            ),
        )

    # ------------------------------------------------------------------ #
    # /roleall
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="roleall",
        description="Give or remove a role for every member who can hold it.",
    )
    @app_commands.guild_only
    @app_commands.describe(
        role="The role to apply.",
        action="Give or take the role.",
        min_humans="Skip accounts flagged as bots or webhooks.",
    )
    async def roleall(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        action: Literal["add", "remove"] = "add",
        min_humans: bool = True,
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction, required=("manage_roles",))
        self.throttle("roleall", interaction.guild_id, interaction.user.id)

        guild = interaction.guild
        self.assert_bot_permissions(guild.me, None, ("manage_roles",), verb="change roles")
        if not role.is_assignable():
            raise PermissionDeniedError(
                f"`{role.name}` is managed by an integration and cannot be assigned."
            )
        if role >= guild.me.top_role:
            raise HierarchyError(
                f"`{role.name}` is at or above my highest role "
                f"(`{guild.me.top_role.name}`), so I could never assign it."
            )

        members = [
            m
            for m in guild.members
            if (not min_humans or not m.bot)
            and role not in m.roles
            and m.top_role < role
            and m.top_role < guild.me.top_role
        ]
        if action == "remove":
            # Removal walks the holders, not everyone: someone who does not have
            # the role cannot need it taken away. The hierarchy filter still
            # applies, because a member whose top role outranks mine must keep
            # every role above it.
            members = [
                m
                for m in guild.members
                if (not min_humans or not m.bot)
                and role in m.roles
                and m.top_role < guild.me.top_role
            ]

        total = len(members)
        if total == 0:
            raise ZagrosError(
                "Nobody in this server currently qualifies â€— either they all already "
                "hold the role, or every candidate outranks me."
            )

        # A single deferred edit carries the progress. Opening one request per
        # member would burn the shared REST bucket and starve other commands.
        status = await interaction.followup.send(
            embed=base_embed(
                title=f"Working Â· {action}ing {role.name}",
                description=f"{self.progress_bar(0, total)}\nStartingâ€¦",
                author=interaction.user,
            ),
            ephemeral=True,
        )

        changed = skipped = 0
        for index, member in enumerate(members, 1):
            # Re-check per member: a sweep of 500 members must not hand a role to
            # the one admin who outranks it just because the bulk filter missed it.
            if member.top_role >= guild.me.top_role or member.top_role >= role:
                skipped += 1
                continue
            audit = build_audit_reason(None, interaction.user, f"Role all: {action}")
            try:
                if action == "add":
                    await member.add_roles(role, reason=audit)
                else:
                    await member.remove_roles(role, reason=audit)
                changed += 1
            except discord.HTTPException as exc:
                skipped += 1
                logger.info("Role sweep skipped %s: %s", member.id, exc)
            await asyncio.sleep(SWEEP_DELAY)
            if index % 25 == 0:
                await self._edit_progress(
                    status, interaction, action, role, changed, skipped, index, total
                )

        await self._edit_progress(
            status, interaction, action, role, changed, skipped, total, total
        )
        await log_action(
            guild,
            warning_embed(
                f"**Role sweep** by {interaction.user}: {action} {describe_role(role)} on "
                f"**{changed}** member(s), {skipped} skipped.\n"
                f"Use `/strip` to undo an accidental sweep.",
                title="Bulk role change",
                author=interaction.user,
            ),
        )

    async def _edit_progress(
        self, status, interaction, action, role, changed, skipped, done, total
    ) -> None:
        with contextlib.suppress(discord.HTTPException):
            await status.edit(
                embed=base_embed(
                    title=f"{action.capitalize()}ing {role.name}",
                    description=(
                        f"{self.progress_bar(done, total)}\n"
                        f"**{changed}** changed Â· **{skipped}** skipped"
                    ),
                    author=interaction.user,
                )
            )

    # ------------------------------------------------------------------ #
    # /temprole
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="temprole",
        description="Give a role that expires by itself.",
    )
    @app_commands.guild_only
    @app_commands.describe(
        target="The member.",
        role="The role to grant temporarily.",
        duration="How long it lasts.",
        reason="Why â€— recorded in the ledger.",
    )
    async def temprole(
        self,
        interaction: discord.Interaction,
        target: discord.Member,
        role: discord.Role,
        duration: Duration,
        reason: app_commands.Range[str, 1, 512] | None = None,
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction, required=("manage_roles",))

        guild = interaction.guild
        self.assert_bot_permissions(guild.me, None, ("manage_roles",), verb="grant roles")
        self.assert_hierarchy(interaction.user, target, guild.me, verb="grant roles to")

        if not role.is_assignable():
            raise PermissionDeniedError(
                f"`{role.name}` is managed by an integration and cannot be assigned."
            )
        if role >= guild.me.top_role:
            raise HierarchyError(
                f"`{role.name}` is at or above my highest role, so I could never "
                "assign it."
            )
        if role in target.roles:
            raise ZagrosError(
                f"**{target}** already holds {describe_role(role)}. Assigning it "
                "again would reset nothing and hide a mistake."
            )

        reason_text = require_text(reason, "Temporary role")
        delta: timedelta = duration
        expires_at = utcnow() + delta

        if role not in await target.add_roles(
            role, reason=build_audit_reason(None, interaction.user, "Temp role")
        ):
            # add_roles returns the roles it actually managed to add.
            raise ZagrosError(
                f"Discord did not apply {describe_role(role)} to {target.mention}."
            )

        db = get_database()
        async with db.session() as session:
            existing = (
                await session.execute(
                    select(TempRoleGrant).where(
                        TempRoleGrant.guild_id == guild.id,
                        TempRoleGrant.user_id == target.id,
                        TempRoleGrant.role_id == role.id,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                existing.expires_at = expires_at
                existing.granted_by = interaction.user.id
            else:
                session.add(
                    TempRoleGrant(
                        guild_id=guild.id,
                        user_id=target.id,
                        role_id=role.id,
                        expires_at=expires_at,
                        granted_by=interaction.user.id,
                    )
                )
            await session.commit()

        await record_case(
            guild_id=guild.id,
            user_id=target.id,
            moderator_id=interaction.user.id,
            action=CaseAction.TEMP_ROLE,
            reason=f"{role.name} for {format_duration(delta)} â€— {reason_text}",
            expires_at=expires_at,
        )
        await log_action(
            guild,
            success_embed(
                f"**{target.mention}** gets {describe_role(role)} for "
                f"{format_duration(delta)} ({timestamp(expires_at)}).\n"
                f"Reason: {truncate(reason_text, 300)}",
                author=interaction.user,
            ),
        )
        await self.reply(
            interaction,
            success_embed(
                f"{describe_role(role)} granted to {target.mention} until "
                f"{timestamp(expires_at)}.\nIt is removed automatically â€— even "
                "if the bot restarts.",
                title="Temporary role granted",
                author=interaction.user,
            ),
        )

    @tasks.loop(minutes=2)
    async def temp_role_sweeper(self) -> None:
        """Revoke expired temporary roles. Runs on boot as well as on a timer."""
        try:
            await self._sweep_temp_roles()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a sweep must never kill the loop
            logger.error("Temporary role sweep failed: %s", exc)

    @temp_role_sweeper.before_loop
    async def _before_sweep(self) -> None:
        # wait_until_ready() raises RuntimeError when the client was never
        # logged in. selftest.py loads extensions to inspect the command tree
        # without connecting, so this loop must treat that as "stay idle"
        # rather than raising once per task per run.
        with contextlib.suppress(RuntimeError):
            await self.bot.wait_until_ready()

    @command_context("membertools:sweep_temp_roles")
    async def _sweep_temp_roles(self) -> int:
        db = get_database()
        now = utcnow()
        try:
            async with db.session() as session:
                rows = list(
                    (
                        await session.execute(
                            select(TempRoleGrant).where(TempRoleGrant.expires_at <= now)
                        )
                    ).scalars()
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not read expired temp roles: %s", exc)
            return 0

        revoked = 0
        for row in rows:
            guild = self.bot.get_guild(row.guild_id)
            member = guild.get_member(row.user_id) if guild else None
            role = guild.get_role(row.role_id) if guild else None
            if member is not None and role is not None:
                try:
                    await member.remove_roles(
                        role,
                        reason=build_audit_reason(None, self.bot.user, "Temp role expired"),
                    )
                    revoked += 1
                except discord.HTTPException as exc:
                    logger.info("Temp role revoke failed for %s: %s", row.user_id, exc)
            try:
                async with db.session() as session:
                    stale = await session.get(TempRoleGrant, row.id)
                    if stale is not None:
                        await session.delete(stale)
                        await session.commit()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not delete temp role row %s: %s", row.id, exc)
        if revoked:
            logger.info("Temporary role sweep removed %s role(s)", revoked)
        return revoked

    # ------------------------------------------------------------------ #
    # /dm
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="dm",
        description="Send a direct message to a member on the server's behalf.",
    )
    @app_commands.guild_only
    @app_commands.describe(
        target="The member to message.",
        message="What to send. Up to 2000 characters.",
    )
    async def dm(
        self,
        interaction: discord.Interaction,
        target: discord.Member,
        message: app_commands.Range[str, 1, 2000],
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction)

        try:
            await target.send(truncate(message, 2000))
        except discord.Forbidden:
            await self.reply(
                interaction,
                warning_embed(
                    f"**{target}** has DMs from server members closed, so the message "
                    "was not delivered. Their message link will not exist.",
                    title="Could not DM",
                    author=interaction.user,
                ),
            )
            return
        except discord.HTTPException as exc:
            raise ZagrosError(f"Discord rejected the DM: {exc}") from exc

        await self.reply(
            interaction,
            success_embed(
                f"Delivered to {target.mention}.", title="Message sent", author=interaction.user
            ),
        )

    # ------------------------------------------------------------------ #
    # /modstats
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="modstats",
        description="Moderation activity for a moderator, or for the whole server.",
    )
    @app_commands.guild_only
    @app_commands.describe(
        moderator="A specific moderator. Omit for a server-wide summary.",
        days="How far back to look.",
    )
    async def modstats(
        self,
        interaction: discord.Interaction,
        moderator: discord.Member | None = None,
        days: app_commands.Range[int, 1, 365] = 30,
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction)

        guild = interaction.guild
        cutoff = utcnow() - timedelta(days=days)

        stmt = select(ModCase).where(
            ModCase.guild_id == guild.id, ModCase.created_at >= cutoff
        )
        if moderator is not None:
            stmt = stmt.where(ModCase.moderator_id == moderator.id)
        rows = await self._cases(stmt)

        counts: dict[str, int] = {}
        revoked = 0
        for row in rows:
            counts[row.action] = counts.get(row.action, 0) + 1
            if not row.is_active:
                revoked += 1

        scope = f"Â· {moderator}" if moderator is not None else "Â· all moderators"
        embed = base_embed(
            title=f"Mod stats {scope}",
            author=interaction.user,
            description=(
                f"**{len(rows)}** action(s) in the last **{days}** day(s)."
                + (f"\n**{revoked}** of them have since been revoked." if revoked else "")
            ),
        )
        if counts:
            embed.add_field(
                name="Breakdown",
                value="\n".join(
                    f"`{action.replace('_', ' ')}` Ã— {count}"
                    for action, count in sorted(counts.items(), key=lambda kv: -kv[1])
                ),
                inline=False,
            )
        else:
            embed.add_field(name="Nothing", value="No actions in that window.", inline=False)

        if moderator is None:
            per_mod = await self._top_moderators(guild.id, cutoff)
            if per_mod:
                embed.add_field(
                    name="Most active",
                    value=truncate(
                        "\n".join(
                            f"**{guild.get_member(mid) or mid}** â€— {count}"
                            for mid, count in per_mod
                        ),
                        1024,
                    ),
                    inline=False,
                )
        await self.reply(interaction, embed)

    @staticmethod
    async def _cases(stmt: Select[tuple[ModCase]]) -> list[ModCase]:
        """Most recent matching cases. Capped: a lifetime view is never useful."""
        async with get_database().session() as session:
            result = await session.execute(
                stmt.order_by(ModCase.id.desc()).limit(5000)
            )
            return list(result.scalars())

    @staticmethod
    async def _top_moderators(guild_id: int, cutoff: datetime) -> list[tuple[int, int]]:
        async with get_database().session() as session:
            rows = (
                await session.execute(
                    select(ModCase.moderator_id, func.count())
                    .where(ModCase.guild_id == guild_id, ModCase.created_at >= cutoff)
                    .group_by(ModCase.moderator_id)
                    .order_by(func.count().desc())
                    .limit(5)
                )
            ).all()
            return [(int(mid), int(count)) for mid, count in rows]

    # ------------------------------------------------------------------ #
    # /ignore + /unignore
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="ignore",
        description="Exempt a channel, role or member from AutoMod and spam checks.",
    )
    @app_commands.guild_only
    @app_commands.describe(
        kind="What kind of target this is.",
        channel="The channel, when kind is channel.",
        role="The role, when kind is role.",
        member="The member, when kind is user.",
    )
    async def ignore(
        self,
        interaction: discord.Interaction,
        kind: Literal["channel", "role", "user"],
        channel: discord.TextChannel | None = None,
        role: discord.Role | None = None,
        member: discord.Member | None = None,
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction)

        target_id = self._ignore_target(kind, channel, role, member)
        db = get_database()
        async with db.session() as session:
            existing = (
                await session.execute(
                    select(GuildIgnore).where(
                        GuildIgnore.guild_id == interaction.guild_id,
                        GuildIgnore.kind == kind,
                        GuildIgnore.target_id == target_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                raise ZagrosError("That is already ignored.")
            session.add(
                GuildIgnore(
                    guild_id=interaction.guild_id,
                    kind=kind,
                    target_id=target_id,
                    created_by=interaction.user.id,
                )
            )
            await session.commit()

        await self.reply(
            interaction,
            success_embed(
                f"`{kind}` `{target_id}` is now exempt from AutoMod and spam checks.",
                title="Ignore added",
                author=interaction.user,
            ),
        )

    @app_commands.command(
        name="unignore",
        description="Remove an /ignore exemption.",
    )
    @app_commands.guild_only
    @app_commands.describe(
        kind="What kind of target this is.",
        channel="The channel, when kind is channel.",
        role="The role, when kind is role.",
        member="The member, when kind is user.",
    )
    async def unignore(
        self,
        interaction: discord.Interaction,
        kind: Literal["channel", "role", "user"],
        channel: discord.TextChannel | None = None,
        role: discord.Role | None = None,
        member: discord.Member | None = None,
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction)

        target_id = self._ignore_target(kind, channel, role, member)
        db = get_database()
        async with db.session() as session:
            row = (
                await session.execute(
                    select(GuildIgnore).where(
                        GuildIgnore.guild_id == interaction.guild_id,
                        GuildIgnore.kind == kind,
                        GuildIgnore.target_id == target_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                raise ZagrosError("That is not on the ignore list.")
            await session.delete(row)
            await session.commit()

        await self.reply(
            interaction,
            success_embed(
                f"`{kind}` `{target_id}` is enforced again.",
                title="Ignore removed",
                author=interaction.user,
            ),
        )

    @staticmethod
    def _ignore_target(
        kind: str,
        channel: discord.TextChannel | None,
        role: discord.Role | None,
        member: discord.Member | None,
    ) -> int:
        supplied = {"channel": channel, "role": role, "user": member}[kind]
        if supplied is None:
            raise ZagrosError(
                f"You chose `kind: {kind}`, so supply the matching "
                f"`{'channel' if kind == 'channel' else kind}` option."
            )
        return supplied.id


async def setup(bot: commands.Bot) -> None:
    """Extension entrypoint."""
    await bot.add_cog(MemberTools(bot))
    logger.info("Member tools cog loaded")
