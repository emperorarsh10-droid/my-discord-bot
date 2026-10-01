"""Community features — polls, giveaways, sticky messages and reaction roles.

Four features that all share one shape: the command creates state, and a
**listener** later acts on someone else's event. That shape forces the decisions
this module is explicit about.

**Durability.** Every one of these stores its state in the database, never in a
``dict`` on the cog. A bot redeploy is a routine event, and an in-memory sticky
message or giveaway would vanish without a trace. The sweepers that consume this
state run on ``before_loop`` so a deploy mid-giveaway still pays out.

**Idempotency.** Listeners fire on *every* relevant event, including replays and
duplicate deliveries, so each handler checks current state before acting. A
``raw_reaction_add`` for a role the member already holds must not fire a second
``add_roles`` call; a message delete must not try to repost a sticky whose channel
is gone.

**One message, one owner.** Every listener resolves the message id it cares
about and does nothing if the table has no row for it. A guild with no giveaways
pays one indexed ``SELECT`` per giveaway event, which is the floor for
correctness without denormalising the message id into a cache.

**Timeout.** Every HTTP call is bounded. A bot that hangs on a webhook waiting for
Discord eventually stops responding to moderation commands, which is the failure
that matters.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
from datetime import timedelta
from typing import Final

import discord
from discord import app_commands
from discord.ext import commands, tasks
from sqlalchemy import select

from cogs._base import FeatureCog
from core.database import get_database
from core.embeds import (
    COLOR_INFO,
    COLOR_SUCCESS,
    base_embed,
    success_embed,
    timestamp,
    truncate,
)
from core.errors import ZagrosError
from core.logging_setup import get_logger
from core.models import (
    Giveaway,
    GiveawayEntry,
    Poll,
    PollVote,
    ReactionRoleRule,
    StickyMessage,
    utcnow,
)
from core.services import build_audit_reason

logger = get_logger("zagrosian.cog.community")

#: Discord rejects polls past this many options.
POLL_MAX_OPTIONS: Final[int] = 10
POLL_MIN_OPTIONS: Final[int] = 2
#: Discord's own message-content ceiling.
POLL_MAX_OPTION_LEN: Final[int] = 100

#: Emojis Discord renders as text buttons. Unicode emoji buttons are supported by
#: the API but rate-limit far harder on burst, so polls use text buttons.
VOTE_BUTTON_TIMEOUT: Final[float] = 15.0


def _to_naive(moment):
    """SQLite hands back naive datetimes; normalise before comparing."""
    return moment.replace(tzinfo=None) if moment.tzinfo is not None else moment


class CommunityFeatures(FeatureCog):
    """Polls, giveaways, sticky messages and reaction roles."""

    feature_name = "community"

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__(bot)
        self._poll_closers: set[asyncio.Task[None]] = set()
        self.giveaway_sweeper.start()
        self.poll_sweeper.start()

    def cog_unload(self) -> None:
        self.giveaway_sweeper.cancel()
        self.poll_sweeper.cancel()
        for task in list(self._poll_closers):
            task.cancel()

    @staticmethod
    async def _close_poll_after(cog: CommunityFeatures, poll_id: int, seconds: float) -> None:
        """Sleep out the poll's window, then close it.

        This is a convenience path only. A restart drops the task, so the
        ``poll_sweeper`` below is what actually guarantees a poll closes on time.
        """
        await asyncio.sleep(seconds)
        await cog._finalise_poll(poll_id)

    async def _sweep_expired_polls(self) -> None:
        """Close every poll whose window passed while the bot was offline."""
        db = get_database()
        now = utcnow()
        async with db.session() as session:
            stale = (
                await session.execute(
                    select(Poll.id).where(
                        Poll.closed.is_(False), Poll.ends_at <= now
                    )
                )
            ).scalars().all()
        for poll_id in stale:
            with contextlib.suppress(Exception):
                await self._finalise_poll(poll_id)

    @tasks.loop(minutes=2)
    async def poll_sweeper(self) -> None:
        await self._sweep_expired_polls()

    @poll_sweeper.before_loop
    async def _before_poll_sweep(self) -> None:
        # RuntimeError means the client was never logged in - selftest.py loads
        # extensions offline to inspect the command tree. Stay idle rather than
        # raising on every tick.
        with contextlib.suppress(RuntimeError):
            await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ #
    # /poll
    # ------------------------------------------------------------------ #
    @app_commands.command(name="poll", description="Post a poll members vote on with buttons.")
    @app_commands.guild_only
    @app_commands.describe(
        question="What are you asking?",
        options="Comma-separated answers. Two to ten.",
        multiple="True to let members pick several.",
        channel="Where to post it. Defaults to this channel.",
        duration="Minutes voting stays open. Leave empty to close it by hand.",
    )
    async def poll(
        self,
        interaction: discord.Interaction,
        question: app_commands.Range[str, 1, 300],
        options: app_commands.Range[str, 3, 1000],
        multiple: bool = False,
        channel: discord.TextChannel | None = None,
        duration: app_commands.Range[int, 5, 10080] | None = None,
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction)

        guild = interaction.guild
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            raise ZagrosError("Polls can only be posted in a text channel.")
        if not target.permissions_for(guild.me).embed_links:
            raise ZagrosError(
                f"I need **Embed Links** in {target.mention} to post a poll."
            )

        choices = [part.strip() for part in options.split(",") if part.strip()]
        if not POLL_MIN_OPTIONS <= len(choices) <= POLL_MAX_OPTIONS:
            raise ZagrosError(
                f"Give between {POLL_MIN_OPTIONS} and {POLL_MAX_OPTIONS} options — "
                f"you gave {len(choices)}."
            )
        if len({c.lower() for c in choices}) != len(choices):
            raise ZagrosError("Two options are identical. Make each one distinct.")
        for choice in choices:
            if len(choice) > POLL_MAX_OPTION_LEN:
                raise ZagrosError(
                    f"Every option must be {POLL_MAX_OPTION_LEN} characters or "
                    f"shorter. Yours: `{truncate(choice, 40)}`"
                )

        db = get_database()
        ends_at = utcnow() + timedelta(minutes=duration) if duration else None
        row = Poll(
            guild_id=guild.id,
            channel_id=target.id,
            question=question.strip(),
            options=json.dumps(choices),
            multiple=multiple,
            ends_at=ends_at,
            created_by=interaction.user.id,
        )
        async with db.session() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
            poll_id = row.id

        view = _PollView(self, poll_id, choices)
        message = await target.send(
            embed=self._poll_embed(poll_id, question, choices, None, multiple, False)
        )
        await message.edit(view=view)

        if ends_at is not None:
            # Held on the cog so it is not garbage-collected mid-sleep, and so
            # cog_unload can cancel it on a hot reload.
            task = asyncio.create_task(
                self._close_poll_after(self, poll_id, (ends_at - utcnow()).total_seconds()),
                name=f"poll-close-{poll_id}",
            )
            self._poll_closers.add(task)
            task.add_done_callback(self._poll_closers.discard)

        async with db.session() as session:
            stored = await session.get(Poll, poll_id)
            if stored is not None:
                stored.message_id = message.id
                await session.commit()

        await self.reply(
            interaction,
            success_embed(
                f"Poll posted in {target.mention}.",
                title="Poll created",
                author=interaction.user,
            ),
        )

    async def _cast_vote(self, poll_id: int, user_id: int, index: int) -> str | None:
        """Record one vote, replacing a previous choice. Returns user-facing text."""
        db = get_database()
        async with db.session() as session:
            row = await session.get(Poll, poll_id)
            if row is None or row.closed:
                return "This poll is closed."
            choices = json.loads(row.options)
            if not 0 <= index < len(choices):
                return "That option is no longer available."
            mine = (
                await session.execute(
                    select(PollVote).where(
                        PollVote.poll_id == poll_id, PollVote.user_id == user_id
                    )
                )
            ).scalars().all()
            picked = {vote.option_index for vote in mine}
            if index in picked:
                return f"You already voted for **{choices[index]}**."
            if row.multiple:
                # Multi-choice keeps every selection; the unique key is per
                # option, so re-picking the same one cannot add a second row.
                session.add(PollVote(poll_id=poll_id, user_id=user_id, option_index=index))
            else:
                # Single-choice replaces the previous vote.
                for vote in mine:
                    await session.delete(vote)
                session.add(PollVote(poll_id=poll_id, user_id=user_id, option_index=index))
            await session.commit()

        # Repaint so the bars reflect the new tally.
        message = await self._poll_message(poll_id)
        if message is not None:
            with contextlib.suppress(discord.HTTPException):
                async with db.session() as session:
                    row = await session.get(Poll, poll_id)
                    votes = (
                        await session.execute(
                            select(PollVote).where(PollVote.poll_id == poll_id)
                        )
                    ).scalars()
                    tally: dict[int, int] = {}
                    for vote in votes:
                        tally[vote.option_index] = tally.get(vote.option_index, 0) + 1
                await message.edit(
                    embed=self._poll_embed(
                        poll_id,
                        row.question,
                        json.loads(row.options),
                        tally,
                        row.multiple,
                        row.closed,
                    )
                )
        return None

    async def _poll_message(self, poll_id: int) -> discord.Message | None:
        db = get_database()
        async with db.session() as session:
            row = await session.get(Poll, poll_id)
            if row is None:
                return None
            guild_id, channel_id, message_id = row.guild_id, row.channel_id, row.message_id
        return self._message(guild_id, channel_id, message_id)

    def _poll_embed(
        self,
        poll_id: int,
        question: str,
        choices: list[str],
        tally: dict[int, int] | None,
        multiple: bool,
        closed: bool,
    ) -> discord.Embed:
        counts = tally or {}
        total = sum(counts.values())
        lines = []
        for index, choice in enumerate(choices):
            count = counts.get(index, 0)
            # Always show at least a filled block so the row does not read as broken.
            share = (count / total * 100) if total else 0.0
            bar = "█" * round(share / 5) if total else "░"
            lines.append(
                f"{bar} **{count}** ({share:.0f}%) — {truncate(choice, 80)}"
            )
        embed = base_embed(
            title=f"Poll · {truncate(question, 200)}",
            colour=COLOR_INFO,
            footer="Closed" if closed else "Multiple choice" if multiple else "Single choice",
        )
        embed.description = "\n".join(lines)
        if total:
            embed.set_footer(text=f"{total} vote(s) · {'Closed' if closed else 'Vote above'}")
        return embed

    async def _finalise_poll(self, poll_id: int) -> tuple[list[str], dict[int, int], int] | None:
        """Close a poll and repaint it with the result. Idempotent."""
        db = get_database()
        async with db.session() as session:
            row = await session.get(Poll, poll_id)
            if row is None or row.closed:
                return None
            row.closed = True
            choices = json.loads(row.options)
            rows = (
                await session.execute(select(PollVote).where(PollVote.poll_id == poll_id))
            ).scalars()
            tally: dict[int, int] = {}
            for vote in rows:
                tally[vote.option_index] = tally.get(vote.option_index, 0) + 1
            guild_id, channel_id, message_id = row.guild_id, row.channel_id, row.message_id
            multiple, question = row.multiple, row.question
            await session.commit()

        message = self._message(guild_id, channel_id, message_id)
        if message is not None:
            with contextlib.suppress(discord.HTTPException):
                await message.edit(
                    embed=self._poll_embed(
                        poll_id, question, choices, tally, multiple, closed=True
                    ),
                    view=None,
                )
        return choices, tally, sum(tally.values())

    # ------------------------------------------------------------------ #
    # /giveaway
    # ------------------------------------------------------------------ #
    @app_commands.command(name="giveaway", description="Run a timed giveaway with button entries.")
    @app_commands.guild_only
    @app_commands.describe(
        prize="What is being given away.",
        winners="How many winners to draw.",
        duration="How long it runs, in minutes.",
        channel="Where to post it. Defaults to this channel.",
    )
    async def giveaway(
        self,
        interaction: discord.Interaction,
        prize: app_commands.Range[str, 1, 256],
        winners: app_commands.Range[int, 1, 20] = 1,
        duration: app_commands.Range[int, 1, 10080] = 60,
        channel: discord.TextChannel | None = None,
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction)

        guild = interaction.guild
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            raise ZagrosError("Giveaways can only be posted in a text channel.")
        if not target.permissions_for(guild.me).embed_links:
            raise ZagrosError(f"I need **Embed Links** in {target.mention}.")

        ends_at = utcnow() + timedelta(minutes=duration)
        db = get_database()
        row = Giveaway(
            guild_id=guild.id,
            channel_id=target.id,
            prize=prize.strip(),
            winners=winners,
            ends_at=ends_at,
            created_by=interaction.user.id,
        )
        async with db.session() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
            giveaway_id = row.id

        embed = self._giveaway_embed(prize, winners, ends_at, 0)
        view = _GiveawayView(self, giveaway_id)
        message = await target.send(embed=embed, view=view)

        async with db.session() as session:
            stored = await session.get(Giveaway, giveaway_id)
            if stored is not None:
                stored.message_id = message.id
                await session.commit()

        await self.reply(
            interaction,
            success_embed(
                f"Giveaway posted in {target.mention}, drawing {winners} winner(s) at "
                f"{timestamp(ends_at)}.",
                title="Giveaway started",
                author=interaction.user,
            ),
        )

    @staticmethod
    def _giveaway_embed(prize: str, winners: int, ends_at, entries: int, ended: bool = False):
        embed = base_embed(
            title="Giveaway ended" if ended else "Giveaway",
            colour=COLOR_INFO if not ended else COLOR_SUCCESS,
        )
        embed.description = (
            f"**Prize:** {truncate(prize, 200)}\n"
            f"**Winners:** {winners}\n"
            f"**Entries:** {entries}\n"
            f"**{'Drew' if ended else 'Draws'}:** {timestamp(ends_at)}"
        )
        if not ended:
            embed.set_footer(text="Click below to enter")
        return embed

    async def _enter_giveaway(self, giveaway_id: int, user_id: int) -> bool:
        """Record one entry. Returns False if they were already in."""
        db = get_database()
        async with db.session() as session:
            giveaway = await session.get(Giveaway, giveaway_id)
            if giveaway is None or giveaway.ended:
                return False
            if _to_naive(giveaway.ends_at) <= utcnow():
                return False
            existing = (
                await session.execute(
                    select(GiveawayEntry).where(
                        GiveawayEntry.giveaway_id == giveaway_id,
                        GiveawayEntry.user_id == user_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return False
            session.add(GiveawayEntry(giveaway_id=giveaway_id, user_id=user_id))
            await session.commit()
        return True

    # ------------------------------------------------------------------ #
    # Giveaway sweeper
    # ------------------------------------------------------------------ #
    @tasks.loop(minutes=1)
    async def giveaway_sweeper(self) -> None:
        """Draw every giveaway whose end time has passed.

        Runs on boot as well: a redeploy during a draw must still pay out.
        """
        try:
            await self._draw_overdue()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("Giveaway sweep failed: %s", exc)

    @giveaway_sweeper.before_loop
    async def _before_sweep(self) -> None:
        # RuntimeError here means the client was never logged in - selftest.py
        # loads extensions offline to inspect the command tree. Stay idle rather
        # than raising on every loop tick.
        with contextlib.suppress(RuntimeError):
            await self.bot.wait_until_ready()

    async def _draw_overdue(self) -> int:
        db = get_database()
        now = utcnow()
        async with db.session() as session:
            rows = list(
                (
                    await session.execute(
                        select(Giveaway).where(
                            Giveaway.ended.is_(False), Giveaway.ends_at <= now
                        )
                    )
                ).scalars()
            )

        drawn = 0
        for row in rows:
            if await self._draw(row.id):
                drawn += 1
        return drawn

    async def _draw(self, giveaway_id: int) -> bool:
        """Finalise one giveaway. Returns True if this call did the drawing."""
        db = get_database()
        async with db.session() as session:
            row = await session.get(Giveaway, giveaway_id)
            if row is None or row.ended:
                return False
            row.ended = True
            entry_ids = list(
                (
                    await session.execute(
                        select(GiveawayEntry.user_id)
                        .where(GiveawayEntry.giveaway_id == giveaway_id)
                        .order_by(GiveawayEntry.id)
                    )
                ).scalars()
            )
            guild_id, channel_id, message_id = row.guild_id, row.channel_id, row.message_id
            prize, winners, ends_at = row.prize, row.winners, row.ends_at
            await session.commit()

        # random.sample on a deterministic id-ordered list keeps the draw
        # reproducible from the database alone, which matters for disputes.
        picked = random.sample(entry_ids, min(winners, len(entry_ids))) if entry_ids else []
        guild = self.bot.get_guild(guild_id)
        channel = guild.get_channel(channel_id) if guild else None

        mentions = ", ".join(f"<@{uid}>" for uid in picked)
        embed = self._giveaway_embed(prize, winners, ends_at, len(entry_ids), ended=True)
        if picked:
            embed.description += f"\n\n**Winner(s):** {mentions}"
        else:
            embed.description += "\n\n**No entries — nobody won.**"

        if channel is not None:
            with contextlib.suppress(discord.HTTPException):
                await channel.send(embed=embed)

        message = self._message(guild_id, channel_id, message_id)
        if message is not None:
            with contextlib.suppress(discord.HTTPException):
                await message.edit(embed=embed, view=None)

        if picked and channel is not None:
            with contextlib.suppress(discord.HTTPException):
                await channel.send(
                    f"Congratulations {mentions} — you won **{truncate(prize, 200)}**!"
                )
        return True

    # ------------------------------------------------------------------ #
    # /sticky + /unsticky
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="sticky", description="Pin a message to the bottom of a channel by reposting it."
    )
    @app_commands.guild_only
    @app_commands.describe(
        message="What the sticky says. Mention and emoji are preserved.",
        channel="Which channel to pin it in.",
    )
    async def sticky(
        self,
        interaction: discord.Interaction,
        message: app_commands.Range[str, 1, 2000],
        channel: discord.TextChannel | None = None,
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction, required=("manage_messages",))

        guild = interaction.guild
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            raise ZagrosError("Sticky messages only work in text channels.")

        db = get_database()
        async with db.session() as session:
            existing = (
                await session.execute(
                    select(StickyMessage).where(
                        StickyMessage.guild_id == guild.id,
                        StickyMessage.channel_id == target.id,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                raise ZagrosError(
                    f"{target.mention} already has a sticky message. Use `/unsticky` "
                    "first."
                )
            session.add(
                StickyMessage(
                    guild_id=guild.id,
                    channel_id=target.id,
                    content=message,
                    created_by=interaction.user.id,
                )
            )
            await session.commit()

        self._sticky_cache[(guild.id, target.id)] = message
        message_id = await self._post_sticky(guild, target.id)
        async with db.session() as session:
            row = (
                await session.execute(
                    select(StickyMessage).where(
                        StickyMessage.guild_id == guild.id,
                        StickyMessage.channel_id == target.id,
                    )
                )
            ).scalar_one_or_none()
            if row is not None:
                row.message_id = message_id
                await session.commit()
        await self.reply(
            interaction,
            success_embed(
                f"A sticky message is now pinned to the bottom of {target.mention}.",
                title="Sticky set",
                author=interaction.user,
            ),
        )

    @app_commands.command(
        name="unsticky", description="Remove a channel's sticky message."
    )
    @app_commands.guild_only
    @app_commands.describe(channel="Which channel to clear.")
    async def unsticky(
        self, interaction: discord.Interaction, channel: discord.TextChannel | None = None
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction, required=("manage_messages",))

        guild = interaction.guild
        target = channel or interaction.channel
        db = get_database()
        async with db.session() as session:
            row = (
                await session.execute(
                    select(StickyMessage).where(
                        StickyMessage.guild_id == guild.id,
                        StickyMessage.channel_id == target.id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                raise ZagrosError(f"{target.mention} has no sticky message.")
            sticky_message_id = row.message_id
            await session.delete(row)
            await session.commit()

        self._sticky_cache.pop((guild.id, target.id), None)

        # Delete exactly the post we made. A "newest bot message in this channel"
        # guess takes an audit log or a moderation receipt down with the sticky.
        if sticky_message_id is not None:
            live = self._message(guild.id, target.id, sticky_message_id)
            if live is not None:
                with contextlib.suppress(discord.HTTPException):
                    await live.delete()

        await self.reply(
            interaction,
            success_embed(
                f"{target.mention} no longer reposts a sticky message.",
                title="Sticky removed",
                author=interaction.user,
            ),
        )

    async def _sticky_text(self, guild_id: int, channel_id: int) -> str | None:
        """The sticky's text, or ``None`` when the channel has none."""
        db = get_database()
        try:
            async with db.session() as session:
                return (
                    await session.execute(
                        select(StickyMessage.content).where(
                            StickyMessage.guild_id == guild_id,
                            StickyMessage.channel_id == channel_id,
                        )
                    )
                ).scalar_one_or_none()
        except Exception as exc:  # noqa: BLE001 - a listener must never raise
            logger.warning("Sticky lookup failed for %s: %s", channel_id, exc)
            return None

    async def _post_sticky(self, guild: discord.Guild, channel_id: int) -> int | None:
        """Put the sticky up immediately. Returns the new message id, or ``None``.

        The id is what ``/unsticky`` deletes later, so it has to be recorded rather
        than rediscovered by guessing which of my messages was the sticky.
        """
        key = (guild.id, channel_id)
        content = self._sticky_cache.get(key)
        if content is None:
            content = await self._sticky_text(guild.id, channel_id)
            if content is None:
                return None
            self._sticky_cache[key] = content
        channel = guild.get_channel(channel_id)
        if channel is None or not channel.permissions_for(guild.me).send_messages:
            return None
        try:
            sent = await channel.send(content)
        except discord.HTTPException:
            return None
        await self._record_sticky_message_id(guild.id, channel_id, sent.id)
        return sent.id

    async def _record_sticky_message_id(
        self, guild_id: int, channel_id: int, message_id: int
    ) -> None:
        db = get_database()
        with contextlib.suppress(Exception):
            async with db.session() as session:
                row = (
                    await session.execute(
                        select(StickyMessage).where(
                            StickyMessage.guild_id == guild_id,
                            StickyMessage.channel_id == channel_id,
                        )
                    )
                ).scalar_one_or_none()
                if row is not None:
                    row.message_id = message_id
                    await session.commit()

    async def _retire_sticky(
        self, guild_id: int, channel_id: int, content: str
    ) -> None:
        """Delete the previous sticky post, if it is still around.

        Falls back to a content match only for rows created before the message id
        was stored; the content is then checked so an unrelated bot message is
        never taken out.
        """
        db = get_database()
        known_id: int | None = None
        async with db.session() as session:
            row = (
                await session.execute(
                    select(StickyMessage).where(
                        StickyMessage.guild_id == guild_id,
                        StickyMessage.channel_id == channel_id,
                    )
                )
            ).scalar_one_or_none()
            if row is not None:
                known_id = row.message_id

        if known_id is not None:
            previous = self._message(guild_id, channel_id, known_id)
            if previous is not None:
                with contextlib.suppress(discord.HTTPException):
                    await previous.delete()
                return

        guild = self.bot.get_guild(guild_id)
        channel = guild.get_channel(channel_id) if guild is not None else None
        if not isinstance(channel, discord.TextChannel):
            return
        async for msg in channel.history(limit=20):
            if msg.author.id == self.bot.user.id and msg.content == content:
                with contextlib.suppress(discord.HTTPException):
                    await msg.delete()
                return

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Repost the sticky after a normal message, then delete the old copy."""
        if message.author.bot or message.guild is None:
            return
        channel = message.channel
        if not isinstance(channel, discord.TextChannel):
            return
        if not channel.permissions_for(message.guild.me).send_messages:
            return

        key = (message.guild.id, channel.id)
        if key not in self._sticky_cache:
            content = await self._sticky_text(key[0], key[1])
            if content is None:
                # Cache the miss so a busy channel without a sticky stops hitting
                # the database on every single message.
                self._sticky_cache[key] = None
                return
            self._sticky_cache[key] = content

        content = self._sticky_cache[key]
        if content is None:
            return

        # Retire the previous copy first: if the repost fails we would otherwise
        # leave two stickies and no way to tell which is live.
        await self._retire_sticky(message.guild.id, channel.id, content)

        sent = None
        with contextlib.suppress(discord.HTTPException):
            sent = await channel.send(content)
        if sent is not None:
            await self._record_sticky_message_id(message.guild.id, channel.id, sent.id)

    # ------------------------------------------------------------------ #
    # /reactionrole
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="reactionrole", description="Let a reaction on a message grant a role."
    )
    @app_commands.guild_only
    @app_commands.describe(
        message="The message people react to. Use its id or a link.",
        emoji="The emoji, e.g. 🎉 or a custom emoji.",
        role="The role to grant.",
    )
    async def reactionrole(
        self,
        interaction: discord.Interaction,
        message: str,
        emoji: str,
        role: discord.Role,
    ) -> None:
        await self.begin(interaction)
        await self.authorise(interaction, required=("manage_roles",))

        guild = interaction.guild
        channel_id, message_id = self._message_id(message)
        if message_id is None:
            raise ZagrosError(
                "Give a message link or its id. Right-click the message, Copy Link."
            )
        if not role.is_assignable():
            raise ZagrosError(f"`{role.name}` is managed by an integration.")
        if role >= guild.me.top_role:
            raise ZagrosError(
                f"`{role.name}` is at or above my highest role, so I could never "
                "assign it."
            )

        # Fetched for real, not assumed: a partial handle would let this succeed for a
        # deleted message or a channel I cannot read.
        target = await self._fetch_message(guild.id, channel_id, message_id)
        if target is None:
            raise ZagrosError(
                "That message is not in a channel I can see. Run this in the channel, "
                "or make sure I can read that channel's history."
            )

        db = get_database()
        async with db.session() as session:
            existing = (
                await session.execute(
                    select(ReactionRoleRule).where(
                        ReactionRoleRule.message_id == message_id,
                        ReactionRoleRule.emoji == emoji,
                        ReactionRoleRule.role_id == role.id,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                raise ZagrosError("That reaction already grants that role.")
            session.add(
                ReactionRoleRule(
                    guild_id=guild.id,
                    channel_id=target.channel.id,
                    message_id=message_id,
                    emoji=emoji,
                    role_id=role.id,
                    created_by=interaction.user.id,
                )
            )
            await session.commit()

        # Pre-cache the emoji so the listener does not parse it on every click.
        with contextlib.suppress(discord.HTTPException):
            await target.add_reaction(emoji)

        await self.reply(
            interaction,
            success_embed(
                f"Reacting with {emoji} on [this message]({target.jump_url}) now grants "
                f"{role.mention}.",
                title="Reaction role added",
                author=interaction.user,
            ),
        )

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        await self._toggle_reaction(payload, add=True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        await self._toggle_reaction(payload, add=False)

    async def _toggle_reaction(self, payload, *, add: bool) -> None:
        if payload.member is None or payload.member.bot:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        emoji = str(payload.emoji)
        role_id = await self._rule_role(payload.message_id, emoji)
        if role_id is None:
            return
        member = guild.get_member(payload.user_id) or payload.member
        role = guild.get_role(role_id)
        if role is None:
            return

        if add:
            # Skip members we could not grant it to anyway: a bot whose role sits
            # below the target's must not get a permission error on every click.
            if role in member.roles:
                return
            if member.top_role >= role or role >= guild.me.top_role:
                with contextlib.suppress(discord.HTTPException):
                    await member.send(
                        f"I could not give you `{role.name}` because your highest role "
                        "is above it. Ask a moderator to move the role."
                    )
                return
            with contextlib.suppress(discord.HTTPException):
                await member.add_roles(
                    role, reason=build_audit_reason(None, self.bot.user, "Reaction role")
                )
        else:
            if role not in member.roles:
                return
            with contextlib.suppress(discord.HTTPException):
                await member.remove_roles(
                    role, reason=build_audit_reason(None, self.bot.user, "Reaction role")
                )

    async def _rule_role(self, message_id: int, emoji: str) -> int | None:
        db = get_database()
        try:
            async with db.session() as session:
                role_id = (
                    await session.execute(
                        select(ReactionRoleRule.role_id).where(
                            ReactionRoleRule.message_id == message_id,
                            ReactionRoleRule.emoji == emoji,
                        )
                    )
                ).scalars().first()
                return int(role_id) if role_id is not None else None
        except Exception as exc:  # noqa: BLE001 - a listener must never raise
            logger.warning("Reaction role lookup failed for %s: %s", message_id, exc)
            return None

    # ------------------------------------------------------------------ #
    # Shared helpers
    # ------------------------------------------------------------------ #
    def _message(
        self, guild_id: int, channel_id: int | None, message_id: int | None
    ) -> discord.Message | None:
        """A partial message handle, for editing something whose id we stored.

        This never proves the message exists: ``get_partial_message`` builds an
        object from an id alone. Callers that must be sure (a command promising
        "that message is not in a channel I can see") need ``_fetch_message``.
        """
        if message_id is None:
            return None
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return None
        if channel_id is not None:
            channel = guild.get_channel(channel_id)
            if isinstance(channel, discord.Messageable):
                return channel.get_partial_message(message_id)
        return None

    async def _fetch_message(
        self, guild_id: int, channel_id: int | None, message_id: int | None
    ) -> discord.Message | None:
        """A real message, or ``None`` if it is gone or unreadable.

        Falls back to asking each text channel when the id came from a link with
        no usable channel id. One failed fetch per channel is cheap next to
        telling a moderator their reaction role is set up when it is not.
        """
        partial = self._message(guild_id, channel_id, message_id)
        if partial is not None:
            with contextlib.suppress(discord.HTTPException):
                return await partial.fetch()
            return None

        guild = self.bot.get_guild(guild_id)
        if guild is None or message_id is None:
            return None
        for channel in guild.text_channels:
            with contextlib.suppress(discord.HTTPException):
                return await channel.fetch_message(message_id)
        return None

    @staticmethod
    def _message_id(reference: str) -> tuple[int | None, int | None]:
        """``(channel_id, message_id)`` from an id or a message link.

        The channel id is ``None`` for a bare id, which is what makes the command
        fall back to scanning rather than guessing a channel.
        """
        cleaned = reference.strip().rstrip("/")
        if cleaned.isdigit():
            return None, int(cleaned)
        # https://discord.com/channels/<guild>/<channel>/<message>
        parts = cleaned.split("/")
        if len(parts) >= 2 and parts[-1].isdigit() and parts[-2].isdigit():
            return int(parts[-2]), int(parts[-1])
        return None, None

    @property
    def _sticky_channels(self) -> set[int]:
        if not hasattr(self, "_sticky_cache"):
            self._sticky_cache: dict[int, str | None] = {}
        return self._sticky_cache

    @property
    def _sticky_absent(self) -> set[int]:
        if not hasattr(self, "_sticky_absent_set"):
            self._sticky_absent_set: set[int] = set()
        return self._sticky_absent_set


class _PollView(discord.ui.View):
    """Vote buttons, one per option.

    discord.py binds a ``custom_id`` to a decorated callback *at class-creation
    time*, so a view whose option count is only known per-poll cannot declare its
    buttons with the decorator. The supported way out is one decorated button
    whose ``custom_id`` is shared, plus plain buttons that reuse it and an
    identity map back to the option index — dispatch then finds the callback, and
    the map recovers which option was pressed.

    The decorator also forces a custom_id, so every plain button must repeat
    ``VOTE_ID`` exactly or the click raises ``CheckFailure``.
    """

    #: Shared custom_id. See the class docstring for why it cannot be per-option.
    VOTE_ID = "poll:vote"

    def __init__(self, cog: CommunityFeatures, poll_id: int, options: list[str]) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.poll_id = poll_id
        self._index: dict[discord.ui.Button, int] = {}

    def add_options(self, options: list[str]) -> None:
        for index, choice in enumerate(options[:POLL_MAX_OPTIONS]):
            button = discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                label=f"{index + 1}. {truncate(choice, 60)}",
                custom_id=self.VOTE_ID,
            )
            self._index[button] = index
            # Four per row matches Discord's own poll layout; 10 options then
            # occupy three rows instead of one unreadable strip.
            self.add_item(button, row=index // 4)

    @discord.ui.button(style=discord.ButtonStyle.secondary, label="0", custom_id=VOTE_ID)
    async def vote(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        index = self._index.get(button)
        if index is None:
            await interaction.response.send("That option is no longer available.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        message = await self.cog._cast_vote(self.poll_id, interaction.user.id, index)
        if message is not None:
            await interaction.followup.send(message, ephemeral=True)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """A vote must not resolve past its deadline."""
        db = get_database()
        try:
            async with db.session() as session:
                row = await session.get(Poll, self.poll_id)
        except Exception:  # noqa: BLE001 - never block a click on a db blip
            return True
        if row is None or row.closed:
            await interaction.response.send("This poll is closed.", ephemeral=True)
            return False
        return True


class _GiveawayView(discord.ui.View):
    """A single enter button, persistent so it survives past the timeout."""

    def __init__(self, cog: CommunityFeatures, giveaway_id: int) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.giveaway_id = giveaway_id

    @discord.ui.button(label="Enter", style=discord.ButtonStyle.primary, custom_id="giveaway:enter")
    async def enter(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok = await self.cog._enter_giveaway(self.giveaway_id, interaction.user.id)
        if ok:
            await interaction.followup.send(
                "You are in. Good luck.", ephemeral=True, thinking=True
            )
        else:
            await interaction.followup.send(
                "You are already entered, or this giveaway has closed.", ephemeral=True
            )


async def setup(bot: commands.Bot) -> None:
    """Extension entrypoint."""
    await bot.add_cog(CommunityFeatures(bot))
    logger.info("Community features cog loaded")
