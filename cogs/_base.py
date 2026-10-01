"""Shared plumbing for the feature cogs.

Every command in ``cogs/moderation2``, ``cogs/raidcontrol``, ``cogs/membertools``,
``cogs/voice``, ``cogs/community`` and ``cogs/manual`` inherits :class:`FeatureCog`
rather than repeating the same six lines. The contract every one of them owes the
user is identical, so it lives in one place:

1.  **Defer before anything else.** Discord voids an unacknowledged interaction
    after three seconds. ``FeatureCog.begin`` is the first statement in every
    command callback and must stay there — adding a permission check or a settings
    read *above* it is the single most common way to reintroduce a "This
    interaction failed" in production.
2.  **Authorise.** :meth:`FeatureCog.authorise` translates the bot's own
    permission checks and role hierarchy into an explanation a moderator can act
    on, rather than a bare ``Forbidden``.
3.  **Rate-limit.** Bulk commands that can each issue hundreds of API calls share
    a limiter so one moderator cannot spend the guild's whole REST budget.
4.  **Answer privately by default.** A moderation result is the moderator's
    business; :meth:`FeatureCog.reply` is ephemeral unless told otherwise.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

import discord
from discord.ext import commands

from core.embeds import error_embed, truncate
from core.errors import (
    HierarchyError,
    PermissionDeniedError,
)
from core.logging_setup import get_logger
from core.ratelimit import RateLimit
from core.services import has_moderation_access

__all__ = ["NO_REASON", "FeatureCog", "require_text", "timestamp_phrase"]

logger = get_logger("zagrosian.cog.feature")

NO_REASON = "No reason provided"

#: Discord's own ceiling. Anything above this is a bug, not a request.
DISCORD_SNOWFLOOR_MIN = 17
DISCORD_SNOWFLOOR_MAX = 20


def require_text(value: str | None, fallback: str = NO_REASON) -> str:
    """Normalise an optional reason string.

    A reason that is whitespace-only is the same as no reason, and moderators
    should not end up with a blank audit-log entry because they hit spacebar.
    """
    cleaned = (value or "").strip()
    return cleaned or fallback


def timestamp_phrase(moment: discord.utils.Markup | None) -> str:
    """Render a discord.py ``<t:...>`` stamp as ``in 2h`` / ``2h ago``."""
    if moment is None:
        return "unknown"
    return f"until {moment} ({discord.utils.format_dt(moment, 'R')})"


class FeatureCog(commands.Cog):
    """Base cog providing the shared command contract.

    Subclasses set :attr:`feature_name` for the log tag and call
    :meth:`begin` first in every callback.
    """

    #: Overridden by each subclass for its log tag.
    feature_name: str = "feature"

    #: Shared limiter for commands that issue many API calls per invocation.
    bulk_limit: RateLimit | None = None

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------ #
    # Step 1 — acknowledge
    # ------------------------------------------------------------------ #
    async def begin(self, interaction: discord.Interaction) -> None:
        """Acknowledge the interaction.

        MUST be the first await in any command callback that talks to the
        database or the Discord API. Deferring privately means the eventual
        answer stays moderator-only without every call site having to remember
        the ``ephemeral=True`` flag on the follow-up.
        """
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException as exc:
            # Already acknowledged, or the interaction expired before we got
            # here. Nothing useful is left to say to the user, so log and let
            # the command continue rather than masking the original failure.
            logger.warning(
                "Defer failed for %s in guild %s: %s",
                interaction.command.qualified_name if interaction.command else "?",
                interaction.guild_id,
                exc,
            )

    # ------------------------------------------------------------------ #
    # Step 2 — authorise
    # ------------------------------------------------------------------ #
    async def authorise(
        self,
        interaction: discord.Interaction,
        required: Iterable[str] | None = None,
    ) -> None:
        """Raise unless the caller may moderate here.

        ``required`` names Discord permissions that the *bot* also needs, which
        is checked against the target channel by :meth:`assert_bot_permissions`.
        """
        if not await has_moderation_access(interaction, required):
            raise PermissionDeniedError(
                "You need to be a moderator here — hold **Manage Messages**, "
                "**Kick Members**, **Ban Members** or **Manage Server**, or a "
                "role named `moderator`, `mod`, `mods`, `staff` or `helper`."
            )

    @staticmethod
    def assert_bot_permissions(
        me: discord.Member,
        channel: discord.abc.GuildChannel | None,
        required: Sequence[str],
        *,
        verb: str = "do that",
    ) -> None:
        """Verify the *bot* holds ``required`` in ``channel``.

        Catching this locally turns an opaque ``Forbidden`` into a message naming
        the exact permission to grant, which is the difference between a
        moderator fixing the problem in ten seconds and filing a bug.
        """
        permissions = me.permissions_in(channel) if channel is not None else me.guild_permissions
        missing = [name.replace("_", " ").title() for name in required
                   if not getattr(permissions, name, False)]
        if not missing:
            return
        where = f" in {channel.mention}" if channel is not None else ""
        raise PermissionDeniedError(
            f"I cannot {verb}{where}: I am missing "
            f"**{'**, **'.join(missing)}**."
        )

    @staticmethod
    def assert_hierarchy(
        actor: discord.Member,
        target: discord.Member,
        bot: discord.Member,
        *,
        verb: str = "act on",
    ) -> None:
        """Refuse an action the bot or the moderator is not senior enough to take.

        Discord's own role ordering only protects the *bot*. Without the actor
        check, a moderator with ``Moderate Members`` could ban the whole admin
        team and every one of those bans would succeed.
        """
        if actor.guild_permissions.administrator or actor.id == actor.guild.owner_id:
            pass
        elif target.top_role >= actor.top_role:
            raise HierarchyError(
                f"**{target}** holds `{target.top_role.name}`, which is at or above "
                f"your own highest role (`{actor.top_role.name}`). Pick someone "
                "below you in the role list."
            )

        if target.id == bot.id:
            raise PermissionDeniedError("I cannot act on myself.")
        if target.id == actor.guild.owner_id:
            raise PermissionDeniedError("The server owner is protected.")
        if target.top_role >= bot.top_role:
            raise HierarchyError(
                f"**{target}** holds `{target.top_role.name}`, which is at or above "
                f"my highest role (`{bot.top_role.name}`). Move my role above theirs "
                "first."
            )

    # ------------------------------------------------------------------ #
    # Step 3 — throttle
    # ------------------------------------------------------------------ #
    def throttle(
        self, action: str, scope: object, subject: object | None = None
    ) -> None:
        """Apply the shared bulk limiter, if this cog declares one."""
        if self.bulk_limit is not None:
            self.bulk_limit.apply(action, scope, subject)

    # ------------------------------------------------------------------ #
    # Step 4 — answer
    # ------------------------------------------------------------------ #
    @staticmethod
    async def reply(
        interaction: discord.Interaction,
        embed: discord.Embed,
        *,
        public: bool = False,
        content: str | None = None,
    ) -> None:
        """Send the deferred answer.

        Ephemeral unless the caller explicitly wants the server to see it, which
        is what a nuke or a panic-mode activation needs.
        """
        await interaction.followup.send(
            embed=embed, content=content, ephemeral=not public
        )

    @staticmethod
    async def fail(interaction: discord.Interaction, message: str) -> None:
        """Report a handled failure on an already-deferred interaction."""
        await interaction.followup.send(
            embed=error_embed(truncate(message, 1800)), ephemeral=True
        )

    # ------------------------------------------------------------------ #
    # Shared helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def is_snowflake(value: str) -> bool:
        """Whether ``value`` is shaped like a Discord id."""
        return value.isdigit() and DISCORD_SNOWFLOOR_MIN <= len(value) <= DISCORD_SNOWFLOOR_MAX

    @staticmethod
    async def resolve_member(
        interaction: discord.Interaction, needle: str
    ) -> discord.Member:
        """Resolve a mention, id, or ``name#1234`` to a member of this guild.

        Raises :class:`MissingTargetError` with a useful message rather than
        letting a ``None`` escape and explode somewhere less obvious.
        """
        from core.errors import MissingTargetError

        guild = interaction.guild
        cleaned = needle.strip().strip("<@!>")

        if cleaned.isdigit():
            member = guild.get_member(int(cleaned))
        else:
            member = discord.utils.get(
                guild.members, lambda m: m.name.lower() == cleaned.lower()
            ) or discord.utils.get(
                guild.members, lambda m: str(m).lower() == cleaned.lower()
            )
            if member is None and "#" in cleaned:
                name, _, discriminator = cleaned.rpartition("#")
                if discriminator.isdigit():
                    member = discord.utils.get(
                        guild.members,
                        lambda m: m.name.lower() == name.lower()
                        and getattr(m, "discriminator", None) == discriminator,
                    )

        if member is None:
            raise MissingTargetError(
                f"I could not find **{needle}** in this server. Use a mention, "
                "their user id, or their exact username."
            )
        return member

    @staticmethod
    def progress_bar(done: int, total: int, width: int = 20) -> str:
        """A block-character progress bar for long-running jobs."""
        if total <= 0:
            return "`" + "░" * width + "`"
        filled = max(0, min(width, round(width * done / total)))
        return "`" + "█" * filled + "░" * (width - filled) + f"` {done}/{total}"
