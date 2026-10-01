"""Raid control — the commands used when the server is actively under attack.

These are the most dangerous commands in the bot, so they share three rules that
override convenience:

**Snapshot before mutating.** ``/panic``, ``/lockdownall``, ``/nuke`` and
``/slowmodeall`` all capture the exact prior state first
(:func:`core.services.save_channel_snapshot`). None of them reconstruct the old
state from a formula, because a formula is a guess and a guess here means a server
whose channels stay locked after the raid is over.

**Never restore by assumption.** :func:`core.services.restore_channel_snapshots`
reports ``(restored, missing)`` and the command surfaces both. "Restored 8" alone
would read as success on three channels that no longer exist.

**Bulk work is bounded and resumable.** Every sweep is rate-limited, reports
progress, and never holds a single request open past Discord's interaction budget
— the command defers first and edits the same message as it goes.

``/nuke`` is additionally gated on an explicit confirmation, because it deletes
every message in a channel and is not something a mis-click should be able to do.
"""

from __future__ import annotations

import asyncio
import re
from datetime import timedelta
from typing import Final, Literal

import discord
from discord import app_commands
from sqlalchemy import select

from cogs._base import FeatureCog, require_text
from core.database import get_database
from core.embeds import (
    base_embed,
    describe_channel,
    success_embed,
    truncate,
    warning_embed,
)
from core.errors import PermissionDeniedError, ZagrosError
from core.logging_setup import command_context, get_logger
from core.models import BlacklistEntry
from core.ratelimit import RateLimit
from core.services import (
    build_audit_reason,
    drop_snapshots,
    get_or_create_guild_config,
    log_action,
    restore_channel_snapshots,
    save_channel_snapshot,
    set_guild_config_flag,
    set_setting,
)

logger = get_logger("zagrosian.cog.raid")

#: Snapshot labels. Distinct names so ``/unpanic`` cannot restore a ``/nuke``
#: snapshot and vice versa.
PANIC_LABEL: Final[str] = "panic"
LOCKDOWN_LABEL: Final[str] = "lockdown"
NUKE_LABEL: Final[str] = "before-nuke"

#: Setting keys owned by this cog.
ANTIINVITE_ACTION_KEY: Final[str] = "antiinvite.action"
BLACKLIST_DELETE_PREFIX: Final[str] = "blacklist.delete:"

#: How long a sweep waits between bulk operations. Discord's per-route bucket is
#: shared across the whole bot, so an unthrottled /nuke on a busy server starves
#: every other command for minutes.
SWEEP_DELAY: Final[float] = 0.9

#: The invite forms Discord itself blocks natively, plus the common obfuscations.
INVITE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?:discord(?:app)?\.com/invite|discord\.gg|discord\.me|discord\.li|dsc\.gg|invite\.gg)"
    r"[/\s]*[A-Za-z0-9-]{2,}",
    re.IGNORECASE,
)

#: Matches a message that is *mostly* capitals, ignoring digits and punctuation.
#: Requiring at least one letter and a length floor keeps acronyms and short
#: interjections ("OK", "LOL") from tripping the rule.
def capitals_share(text: str) -> float:
    """Fraction of ``text``'s letters that are uppercase. ``0.0`` if no letters."""
    letters = [char for char in text if char.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for char in letters if char.isupper()) / len(letters)


#: ``spam_max_messages`` above this is refused: the point of the rule is to catch
#: floods, and a threshold in the thousands is a denial of service the guild wrote
#: for itself.
MAX_SPAM_THRESHOLD: Final[int] = 30


class RaidControl(FeatureCog):
    """Emergency levers: panic, lockdown, nuke, rate limits and pattern rules."""

    feature_name = "raidcontrol"
    bulk_limit = RateLimit(max_calls=3, window=60.0)

    # ------------------------------------------------------------------ #
    # /panic
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="panic",
        description="Silence every text channel at once. /unpanic reverses it.",
    )
    @app_commands.guild_only
    @app_commands.describe(
        reason="Why panic mode is being activated.",
        channel="Only lock this channel instead of the whole server.",
    )
    async def panic(
        self,
        interaction: discord.Interaction,
        reason: app_commands.Range[str, 1, 512] | None = None,
        channel: discord.TextChannel | None = None,
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction, require=("manage_channels",))
        self.throttle("panic", interaction.guild_id, interaction.user.id)

        guild = interaction.guild
        reason_text = require_text(reason, "Raid / incident")

        targets: list[discord.TextChannel] = (
            [channel]
            if channel is not None
            else [c for c in guild.text_channels if c.permissions_for(guild.me).send_messages]
        )
        if not targets:
            raise ZagrosError("There are no text channels I can lock down here.")

        # Snapshot first. If this fails we abort rather than lock channels whose
        # original permissions we would not be able to restore.
        for target in targets:
            snapshot = await save_channel_snapshot(
                guild.id,
                target,
                PANIC_LABEL if channel is None else f"{PANIC_LABEL}-{channel.id}",
                created_by=interaction.user.id,
            )
            if snapshot is None:
                raise ZagrosError(
                    "I could not record the current permissions, so I refused to "
                    "lock anything down — otherwise there would be no way back."
                )

        locked = 0
        failures: list[str] = []
        for target in targets:
            try:
                await target.edit(
                    overwrites={
                        guild.default_role: discord.PermissionOverwrite(
                            send_messages=False, add_reactions=False
                        )
                    },
                    reason=build_audit_reason(f"Panic: {reason_text}"),
                )
                locked += 1
                await asyncio.sleep(0.4)
            except discord.Forbidden:
                failures.append(target.name)
            except discord.HTTPException as exc:
                logger.warning("Panic lock failed for %s: %s", target.id, exc)
                failures.append(target.name)

        await self._set_panic_flag(interaction, True)
        await log_action(
            guild,
            warning_embed(
                f"**Panic mode activated** by {interaction.user}.\n"
                f"{locked} channel(s) silenced. Reason: {truncate(reason_text, 400)}\n"
                f"Run `/unpanic` to restore every permission exactly.",
                title="Panic mode",
                author=interaction.user,
            ),
        )

        embed = success_embed(
            f"Locked **{locked}** channel(s).\n"
            f"Reason: {truncate(reason_text, 400)}\n\n"
            f"`/unpanic` restores the exact prior permissions from the snapshot.",
            title="Panic mode on",
            author=interaction.user,
        )
        if failures:
            embed.add_field(
                name="Could not lock",
                value=truncate(", ".join(failures), 1000),
                inline=False,
            )
        await self.reply(interaction, embed)

    # ------------------------------------------------------------------ #
    # /unpanic
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="unpanic",
        description="Restore every permission captured by /panic.",
    )
    @app_commands.guild_only
    async def unpanic(self, interaction: discord.Interaction) -> None:
        await self.begin(interaction)
        await self.authorise(interaction, require=("manage_channels",))
        self.throttle("unpanic", interaction.guild_id, interaction.user.id)

        guild = interaction.guild
        restored, missing = await restore_channel_snapshots(guild, PANIC_LABEL)
        # Also clear any per-channel panic snapshots from a scoped activation.
        for channel in guild.text_channels:
            scoped_restored, scoped_missing = await restore_channel_snapshots(
                guild, f"{PANIC_LABEL}-{channel.id}"
            )
            restored += scoped_restored
            missing += scoped_missing
            # Dropped per channel: leaving these rows behind means the next
            # `/panic` in that channel reuses a stale snapshot, and `/unpanic`
            # reports a restore that has nothing left to restore.
            await drop_snapshots(guild.id, f"{PANIC_LABEL}-{channel.id}")

        if restored == 0 and missing == 0:
            raise ZagrosError(
                "There is no panic snapshot to restore. `/unpanic` only works "
                "after `/panic` ran in this server."
            )

        await drop_snapshots(guild.id, PANIC_LABEL)
        await self._set_panic_flag(interaction, False)
        await log_action(
            guild,
            success_embed(
                f"**Panic mode lifted** by {interaction.user}. "
                f"{restored} channel(s) restored.",
                author=interaction.user,
            ),
        )

        embed = success_embed(
            f"Restored **{restored}** channel(s) to their exact previous permissions.",
            title="Panic mode off",
            author=interaction.user,
        )
        if missing:
            embed.add_field(
                name="Not restored",
                value=(
                    f"{missing} channel(s) no longer exist or are beyond my "
                    "permissions. Nothing else was affected."
                ),
                inline=False,
            )
        await self.reply(interaction, embed)

    # ------------------------------------------------------------------ #
    # /lockdownall + /unlockall
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="lockdownall",
        description="Stop @everyone posting in every text channel. /unlockall reverses.",
    )
    @app_commands.guild_only
    @app_commands.describe(reason="Why the server is being locked down.")
    async def lockdownall(
        self,
        interaction: discord.Interaction,
        reason: app_commands.Range[str, 1, 512] | None = None,
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction, require=("manage_channels",))
        self.throttle("lockdownall", interaction.guild_id, interaction.user.id)

        guild = interaction.guild
        reason_text = require_text(reason, "Scheduled lockdown")
        targets = [
            c
            for c in guild.text_channels
            if c.permissions_for(guild.me).send_messages
        ]
        if not targets:
            raise ZagrosError("There are no text channels I can lock here.")

        for target in targets:
            if await save_channel_snapshot(
                guild.id, target, LOCKDOWN_LABEL, created_by=interaction.user.id
            ) is None:
                raise ZagrosError(
                    "I could not record current permissions, so I refused to lock "
                    "anything down."
                )

        locked, failures = 0, []
        for target in targets:
            try:
                await target.edit(
                    overwrites={
                        guild.default_role: discord.PermissionOverwrite(send_messages=False)
                    },
                    reason=build_audit_reason(f"Lockdown: {reason_text}"),
                )
                locked += 1
                await asyncio.sleep(0.4)
            except discord.HTTPException:
                failures.append(target.name)

        await log_action(
            guild,
            warning_embed(
                f"**Lockdown** by {interaction.user}: {locked} channel(s).\n"
                f"Reason: {truncate(reason_text, 400)}",
                author=interaction.user,
            ),
        )
        embed = success_embed(
            f"Locked **{locked}** channel(s). `/unlockall` restores them exactly.",
            title="Server locked down",
            author=interaction.user,
        )
        if failures:
            embed.add_field(
                name="Could not lock", value=truncate(", ".join(failures), 1000), inline=False
            )
        await self.reply(interaction, embed)

    @app_commands.command(
        name="unlockall",
        description="Restore every permission captured by /lockdownall.",
    )
    @app_commands.guild_only
    async def unlockall(self, interaction: discord.Interaction) -> None:
        await self.begin(interaction)
        await self.authorise(interaction, require=("manage_channels",))

        guild = interaction.guild
        restored, missing = await restore_channel_snapshots(guild, LOCKDOWN_LABEL)
        if restored == 0 and missing == 0:
            raise ZagrosError("There is no lockdown snapshot to restore.")
        await drop_snapshots(guild.id, LOCKDOWN_LABEL)

        await log_action(
            guild,
            success_embed(
                f"**Lockdown lifted** by {interaction.user}: {restored} restored.",
                author=interaction.user,
            ),
        )
        embed = success_embed(
            f"Restored **{restored}** channel(s).", title="Server unlocked", author=interaction.user
        )
        if missing:
            embed.add_field(
                name="Not restored",
                value=f"{missing} channel(s) are gone or beyond my permissions.",
                inline=False,
            )
        await self.reply(interaction, embed)

    # ------------------------------------------------------------------ #
    # /slowmodeall
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="slowmodeall",
        description="Apply one slowmode to every text channel.",
    )
    @app_commands.guild_only
    @app_commands.describe(
        seconds="Slowmode length, 0-21600. 0 clears it.",
        channel="Only apply to this channel.",
    )
    async def slowmodeall(
        self,
        interaction: discord.Interaction,
        seconds: app_commands.Range[int, 0, 21600],
        channel: discord.TextChannel | None = None,
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction, require=("manage_channels",))
        self.throttle("slowmodeall", interaction.guild_id, interaction.user.id)

        guild = interaction.guild
        targets = [channel] if channel is not None else list(guild.text_channels)
        targets = [c for c in targets if c.permissions_for(guild.me).send_messages]
        if not targets:
            raise ZagrosError("There are no text channels I can set slowmode on here.")

        # No snapshot is taken here, unlike /lockdownall: Discord already holds
        # each channel's current slowmode in ``slowmode_delay``, so the restore
        # path is a read rather than a guess. Persisting it would be redundant
        # state that can disagree with the real channel.
        changed, failures = 0, []
        for target in targets:
            try:
                await target.edit(
                    slowmode_delay=timedelta(seconds=seconds),
                    reason=build_audit_reason(f"Slowmode {seconds}s"),
                )
                changed += 1
                await asyncio.sleep(0.4)
            except discord.HTTPException:
                failures.append(target.name)

        # Discord refuses slowmode on some channel types (announcements, forums
        # need different handling), so a partial success is normal.
        await self.reply(
            interaction,
            success_embed(
                f"Slowmode set to **{seconds}s** on **{changed}** channel(s)."
                + ("" if seconds else " (cleared)"),
                title="Slowmode applied",
                author=interaction.user,
            ),
        )

    # ------------------------------------------------------------------ #
    # /antiinvite
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="antiinvite",
        description="Block or allow Discord invite links in chat.",
    )
    @app_commands.guild_only
    @app_commands.describe(
        enabled="On to block invite links, off to allow them.",
        action="What to do when one is posted: delete the message, or time out the author.",
    )
    async def antiinvite(
        self,
        interaction: discord.Interaction,
        enabled: bool,
        action: Literal["delete", "timeout"] = "delete",
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction)

        config = await get_or_create_guild_config(interaction.guild_id)
        if config is None:
            raise ZagrosError("The settings database is unreachable, so nothing changed.")
        config.anti_invite = enabled
        await set_setting(
            interaction.guild_id,
            ANTIINVITE_ACTION_KEY,
            action,
            created_by=interaction.user.id,
        )
        db = get_database()
        async with db.session() as session:
            session.add(config)
            await session.commit()

        await self.reply(
            interaction,
            success_embed(
                ("Invite links are now blocked" if enabled else "Invite links are allowed")
                + (f" — offending authors get a {int(config.spam_timeout_seconds)}s timeout."
                   if enabled and action == "timeout" else "."),
                title="Anti-invite updated",
                author=interaction.user,
            ),
        )

    # ------------------------------------------------------------------ #
    # /antispam
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="antispam",
        description="Set the message flood threshold and the timeout it applies.",
    )
    @app_commands.guild_only
    @app_commands.describe(
        threshold="Messages allowed inside the window. 0 disables the rule.",
        window="Length of the window in seconds.",
        penalty="Seconds of timeout applied when it trips.",
    )
    async def antispam(
        self,
        interaction: discord.Interaction,
        threshold: app_commands.Range[int, 0, 100],
        window: app_commands.Range[int, 2, 60] = 10,
        penalty: app_commands.Range[int, 0, 2419200] = 60,
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction)

        if threshold > MAX_SPAM_THRESHOLD:
            raise ZagrosError(
                f"The threshold is capped at **{MAX_SPAM_THRESHOLD}** messages. "
                "Higher than that and the rule stops being anti-spam."
            )

        config = await get_or_create_guild_config(interaction.guild_id)
        if config is None:
            raise ZagrosError("The settings database is unreachable, so nothing changed.")
        config.spam_max_messages = threshold
        config.spam_window_seconds = window
        config.spam_timeout_seconds = penalty

        db = get_database()
        async with db.session() as session:
            session.add(config)
            await session.commit()

        await self.reply(
            interaction,
            success_embed(
                (
                    f"Spam protection now trips at **{threshold}** messages in "
                    f"**{window}s**, applying a **{penalty}s** timeout."
                    if threshold
                    else "Spam protection is off."
                ),
                title="Anti-spam updated",
                author=interaction.user,
            ),
        )

    # ------------------------------------------------------------------ #
    # /blacklist
    # ------------------------------------------------------------------ #
    blacklist_group = app_commands.Group(
        name="blacklist",
        description="Bot-enforced blocked patterns (phrases or regular expressions).",
    )

    @blacklist_group.command(name="add", description="Block a phrase or regex.")
    @app_commands.guild_only
    @app_commands.describe(
        pattern="The phrase, or a regex when regex is true.",
        regex="Treat the pattern as a Python regular expression.",
        delete_messages="Delete matching messages instead of only timing out.",
    )
    async def blacklist_add(
        self,
        interaction: discord.Interaction,
        pattern: app_commands.Range[str, 2, 200],
        regex: bool = False,
        delete_messages: bool = True,
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction)

        cleaned = pattern.strip()
        if regex and not self._valid_regex(cleaned):
            raise ZagrosError(
                f"`{truncate(cleaned, 60)}` is not a valid regular expression, "
                "so I will not store a rule that can never match."
            )

        db = get_database()
        async with db.session() as session:
            existing = (
                await session.execute(
                    select(BlacklistEntry).where(
                        BlacklistEntry.guild_id == interaction.guild_id,
                        BlacklistEntry.pattern == cleaned,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                raise ZagrosError(f"`{truncate(cleaned, 60)}` is already blacklisted.")
            session.add(
                BlacklistEntry(
                    guild_id=interaction.guild_id,
                    pattern=cleaned,
                    is_regex=regex,
                    created_by=interaction.user.id,
                )
            )
            await session.commit()
            await set_setting(
                interaction.guild_id,
                f"{BLACKLIST_DELETE_PREFIX}{cleaned}",
                "1" if delete_messages else "0",
                created_by=interaction.user.id,
            )

        await self.reply(
            interaction,
            success_embed(
                f"Now blocking `{truncate(cleaned, 200)}`"
                + (" (regular expression)" if regex else "")
                + (" and deleting matches." if delete_messages else " and timing out the author."),
                title="Blacklist rule added",
                author=interaction.user,
            ),
        )

    @blacklist_group.command(name="remove", description="Stop blocking a pattern.")
    @app_commands.guild_only
    @app_commands.describe(pattern="The exact pattern that was added.")
    async def blacklist_remove(
        self, interaction: discord.Interaction, pattern: app_commands.Range[str, 2, 200]
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction)

        cleaned = pattern.strip()
        db = get_database()
        async with db.session() as session:
            row = (
                await session.execute(
                    select(BlacklistEntry).where(
                        BlacklistEntry.guild_id == interaction.guild_id,
                        BlacklistEntry.pattern == cleaned,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                raise ZagrosError(f"`{truncate(cleaned, 60)}` is not on the blacklist.")
            await session.delete(row)
            await session.commit()

        await self.reply(
            interaction,
            success_embed(f"No longer blocking `{truncate(cleaned, 200)}`.",
                          title="Blacklist rule removed", author=interaction.user),
        )

    @blacklist_group.command(name="list", description="Show every blacklist rule.")
    @app_commands.guild_only
    async def blacklist_list(self, interaction: discord.Interaction) -> None:
        await self.begin(interaction)
        await self.authorise(interaction)

        rows = await self._blacklist_rows(interaction.guild_id)
        embed = base_embed(
            title="Blacklist", author=interaction.user, description=f"**{len(rows)}** rule(s)."
        )
        if not rows:
            embed.add_field(
                name="Empty", value="No patterns are blocked by the bot.", inline=False
            )
        for row in rows[:25]:
            kind = "regex" if row.is_regex else "phrase"
            embed.add_field(
                name=f"`{truncate(row.pattern, 120)}`",
                value=f"type: {kind} · added by `{row.created_by}`",
                inline=False,
            )
        await self.reply(interaction, embed)

    @staticmethod
    def _valid_regex(pattern: str) -> bool:
        try:
            re.compile(pattern)
        except re.error:
            return False
        return True

    @staticmethod
    async def _blacklist_rows(guild_id: int) -> list[BlacklistEntry]:
        db = get_database()
        async with db.session() as session:
            return list(
                (
                    await session.execute(
                        select(BlacklistEntry)
                        .where(BlacklistEntry.guild_id == guild_id)
                        .order_by(BlacklistEntry.id)
                    )
                ).scalars()
            )

    # ------------------------------------------------------------------ #
    # /nuke
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="nuke",
        description="Delete a channel and recreate it with the same settings and permissions.",
    )
    @app_commands.guild_only
    @app_commands.describe(
        channel="The channel to recreate.",
        reason="Why this is happening — it goes in the audit log.",
        confirm="Must be true. This deletes every message in the channel.",
    )
    async def nuke(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        reason: app_commands.Range[str, 1, 512] | None = None,
        confirm: bool = False,
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction, require=("manage_channels",))
        self.throttle("nuke", interaction.guild_id, interaction.user.id)

        if not confirm:
            raise ZagrosError(
                "This deletes **every message** in the channel. Re-run with "
                "`confirm: true` once you are sure — the channel itself, its "
                "permissions and its settings are recreated afterwards."
            )

        guild = interaction.guild
        reason_text = require_text(reason, "Channel nuked")
        if channel.id == interaction.channel_id:
            raise ZagrosError(
                "I cannot nuke the channel you are standing in. Run it from "
                "another channel."
            )
        if channel is guild.system_channel:
            raise ZagrosError("The system channel cannot be deleted.")

        snapshot = await save_channel_snapshot(
            guild.id, channel, NUKE_LABEL, created_by=interaction.user.id
        )
        if snapshot is None:
            raise ZagrosError(
                "I could not record the channel's permissions, so I refused to "
                "delete it."
            )

        meta = await self._channel_meta(channel)
        name, position, category_id = meta

        try:
            await channel.delete(reason=build_audit_reason(f"Nuke: {reason_text}"))
        except discord.Forbidden as exc:
            raise PermissionDeniedError(
                "Discord refused to delete the channel. I need **Manage Channels**."
            ) from exc

        # The old channel id is gone, so the freshly created one has to have its
        # permissions applied explicitly rather than inherited from a template.
        new_channel = await self._recreate(
            guild,
            name=name,
            position=position,
            category_id=category_id,
            reason_text=reason_text,
        )
        if new_channel is None:
            raise ZagrosError(
                "The channel was deleted but recreating it failed. Check "
                "**Manage Channels** and my role position, then use "
                "`/serverinfo` to see the current layout."
            )

        await self._reapply(new_channel, snapshot, meta)
        await drop_snapshots(guild.id, NUKE_LABEL)
        await log_action(
            guild,
            warning_embed(
                f"**Channel nuked** by {interaction.user}.\n"
                f"`#{name}` was deleted and recreated as {describe_channel(new_channel)} "
                f"with its permissions restored.\nReason: {truncate(reason_text, 400)}",
                title="Channel nuked",
                author=interaction.user,
            ),
        )

        await self.reply(
            interaction,
            success_embed(
                f"Deleted and recreated {describe_channel(new_channel)}.\n"
                f"Permissions, category, position, slowmode and topic restored.\n"
                f"New link: {new_channel.mention}",
                title="Channel nuked",
                author=interaction.user,
            ),
            public=True,
        )

    # ------------------------------------------------------------------ #
    # /invites
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="invites",
        description="List active invites with their channel and approximate use count.",
    )
    @app_commands.guild_only
    async def invites(self, interaction: discord.Interaction) -> None:
        await self.begin(interaction)
        await self.authorise(interaction, require=("manage_guild",))

        guild = interaction.guild
        self.assert_bot_permissions(guild.me, None, ("manage_guild",), verb="list invites")

        data = await self._fetch_invites(guild)
        if data is None:
            raise ZagrosError("Discord would not return the invite list for this server.")
        entries = data.get("invites") or []

        embed = base_embed(
            title="Active invites",
            author=interaction.user,
            description=f"**{len(entries)}** live invite(s).",
        )
        if not entries:
            embed.add_field(name="None", value="Nobody can invite right now.", inline=False)
        for entry in entries[:15]:
            code = entry.get("code", "?")
            channel = guild.get_channel(int(entry.get("channel", {}).get("id", 0) or 0))
            uses = entry.get("uses")
            inviter_id = entry.get("inviter", {}).get("id")
            inviter = guild.get_member(int(inviter_id)) if inviter_id else None
            embed.add_field(
                name=f"discord.gg/{code}",
                value=(
                    f"Channel: {describe_channel(channel) if channel else '`unknown`'}\n"
                    f"Uses: {'unlimited' if uses is None else uses} · "
                    f"Expires: {entry.get('expires_at') or 'never'}\n"
                    f"Inviter: {inviter or 'unknown'}"
                ),
                inline=False,
            )
        await self.reply(interaction, embed)

    # ------------------------------------------------------------------ #
    # /clean invites
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="clean",
        description="Revoke invites — all of them, or only the ones matching a channel.",
    )
    @app_commands.guild_only
    @app_commands.describe(
        target="Only revoke invites pointing at this channel. Omit for every invite.",
        confirm="Must be true. Revoking an invite breaks every link already shared.",
    )
    async def clean(
        self,
        interaction: discord.Interaction,
        target: discord.TextChannel | None = None,
        confirm: bool = False,
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction, require=("manage_guild",))
        self.throttle("clean", interaction.guild_id, interaction.user.id)

        if not confirm:
            scope = f"those pointing at {target.mention}" if target else "**every invite**"
            raise ZagrosError(
                f"This revokes {scope}. Re-run with `confirm: true` — anyone "
                "holding a revoked link can no longer use it."
            )

        guild = interaction.guild
        self.assert_bot_permissions(guild.me, None, ("manage_guild",), verb="revoke invites")

        data = await self._fetch_invites(guild)
        if data is None:
            raise ZagrosError("Discord would not return the invite list for this server.")
        entries = data.get("invites") or []

        wanted: list[str] = []
        for entry in entries:
            code = entry.get("code")
            if not code:
                continue
            if target is not None:
                channel_id = (entry.get("channel") or {}).get("id")
                if str(channel_id) != str(target.id):
                    continue
            wanted.append(code)

        if not wanted:
            raise ZagrosError("There were no invites matching that to revoke.")

        revoked, failed = 0, []
        for code in wanted:
            try:
                await self._delete_invite(guild, code)
                revoked += 1
            except discord.HTTPException as exc:
                logger.warning("Invite revoke failed for %s: %s", code, exc)
                failed.append(code)
            await asyncio.sleep(SWEEP_DELAY)

        await log_action(
            guild,
            warning_embed(
                f"**{revoked}** invite(s) revoked by {interaction.user}"
                + (f" (scope: {target.mention})" if target else " (all invites)")
                + ".",
                author=interaction.user,
            ),
        )

        embed = success_embed(
            f"Revoked **{revoked}** of {len(wanted)} invite(s).",
            title="Invites cleaned",
            author=interaction.user,
        )
        if failed:
            embed.add_field(
                name="Failed", value=truncate(", ".join(failed), 1000), inline=False
            )
        await self.reply(interaction, embed)

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    async def _fetch_invites(guild: discord.Guild) -> dict | None:
        """Call the invites endpoint directly.

        discord.py has no typed API for listing a guild's invites, and the
        alternative — reading the audit log — only surfaces events, not the live
        invite objects. The HTTP route is stable and the response shape is small.
        """
        return await guild.http.request("GET", f"/guilds/{guild.id}/invites")

    @staticmethod
    async def _delete_invite(guild: discord.Guild, code: str) -> None:
        """Revoke one invite.

        Must go through the bot's own HTTP client: constructing a fresh
        ``HTTPClient`` here would issue an unauthenticated request and every
        revoke would 401.
        """
        await guild.http.request("DELETE", f"/guilds/{guild.id}/invites/{code}")

    @staticmethod
    async def _channel_meta(channel: discord.TextChannel) -> tuple[str, int, int | None]:
        return (channel.name, channel.position, channel.category_id)

    @command_context("raid:recreate_channel")
    async def _recreate(
        self,
        guild: discord.Guild,
        *,
        name: str,
        position: int,
        category_id: int | None,
        reason_text: str,
    ) -> discord.TextChannel | None:
        kwargs: dict[str, object] = {
            "name": name,
            "reason": build_audit_reason(f"Nuke: {reason_text}"),
        }
        if category_id is not None and guild.get_category(category_id) is not None:
            kwargs["category"] = guild.get_category(category_id)
        try:
            return await guild.create_text_channel(**kwargs)  # type: ignore[arg-type]
        except discord.Forbidden:
            logger.warning("Nuke recreate refused: missing Manage Channels")
        except discord.HTTPException as exc:
            logger.warning("Nuke recreate failed: %s", exc)
        return None

    @command_context("raid:reapply_snapshot")
    async def _reapply(
        self,
        channel: discord.TextChannel,
        snapshot,
        meta: tuple[str, int, int | None],
    ) -> None:
        """Copy a nuked channel's saved state onto its replacement."""
        from core.services import json_to_overwrites

        try:
            overwrites: dict[object, discord.PermissionOverwrite] = {}
            for kind, target_id, allow, deny in json_to_overwrites(snapshot.overwrites):
                target = (
                    channel.guild.get_role(target_id)
                    if kind == "role"
                    else channel.guild.get_member(target_id)
                )
                if target is not None:
                    overwrites[target] = discord.PermissionOverwrite(
                        allow=discord.Permissions(allow), deny=discord.Permissions(deny)
                    )
            await channel.edit(
                overwrites=overwrites,
                position=meta[1],
                reason="Restoring nuked channel state",
            )
        except discord.HTTPException as exc:
            logger.warning("Could not fully restore nuked channel %s: %s", channel.id, exc)

    @staticmethod
    async def _set_panic_flag(self, interaction: discord.Interaction, active: bool) -> None:
        # Persisted, not just logged: /status and any recovery tooling need to
        # know a server is still in panic after a restart, and a flag that lives
        # only in a log line is indistinguishable from one that was never set.
        saved = await set_guild_config_flag(
            interaction.guild_id, "panic_active", active
        )
        logger.info(
            "panic flag for guild %s -> %s (persisted=%s)",
            interaction.guild_id,
            active,
            saved,
        )
        if not saved:
            await log_action(
                interaction.guild,
                warning_embed(
                    "I could not record the panic state in the database. The "
                    "channels are still locked, but `/status` will not show panic "
                    "as active after a restart.",
                    title="Panic flag not saved",
                    author=interaction.user,
                ),
            )


async def setup(bot) -> None:
    """Extension entrypoint."""
    await bot.add_cog(RaidControl(bot))
    logger.info("Raid control cog loaded")
