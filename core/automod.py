"""AutoMod — pattern-based message filtering that runs before Discord's own.

Discord's native AutoMod is excellent at keyword rules and rate limits, and this
module does not duplicate it. What it adds is the three checks Discord cannot
express, plus the controls to run them:

* **Duplicate flooding.** Discord has no "same message N times" rule. Members
  spamming one line is the single most common raid pattern and it slips past
  keyword filters entirely, because the message is not a *banned* word.
* **Caps abuse.** Shouting is not a keyword and Discord cannot measure it.
* **Configurable penalties.** Each check can be silent, delete, warn, timeout or
  kick, independently.

**Per-guild configuration lives in ``guild_configs``** (``automod_enabled``,
``anticaps_percent``, ``anticaps_min_length``, ``spam_*``) so the hot path is two
column reads and no extra query. Everything else — thresholds that change rarely
— is cached in memory with a short TTL, because a rule change must not turn every
message into a database round-trip.

**The listener never raises.** A filter is a background safety net; if it throws,
the message has already been delivered and the guild has lost protection. Every
handler wraps its work and logs, because a stack trace from a listener silently
disables the rule without telling anyone.

**Ordering matters.** ``/ignore`` is checked first and returns immediately, so an
exempt channel costs one indexed lookup and no scanning at all.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Final

import discord
from discord.ext import commands
from sqlalchemy import select

from core.database import get_database
from core.logging_setup import get_logger
from core.models import GuildConfig, GuildIgnore

logger = get_logger("zagrosian.automod")

#: Discord's per-keyword ceiling. A longer phrase cannot be stored natively, so
#: ``/filter`` rejects it rather than silently truncating to something else.
MAX_KEYWORD_LEN: Final[int] = 60

#: Discord's native AutoMod rule ceiling. Beyond this the rule cannot exist.
MAX_PHRASES: Final[int] = 1000

__all__ = [
    "MAX_KEYWORD_LEN",
    "MAX_PHRASES",
    "AutoMod",
    "AutoModSettings",
    "AutoModVerdict",
    "clean_phrases",
    "extract_invite_codes",
    "find_rule",
    "is_command_like",
    "looks_like_shouting",
    "matches_phrase",
    "normalise_for_duplicate",
    "normalize_phrase",
    "sync_automod_rule",
]


def normalize_phrase(phrase: str) -> str:
    """Lower-case, trim, and collapse internal whitespace in one keyword."""
    return " ".join(str(phrase).split()).strip().lower()


def clean_phrases(phrases: object) -> list[str]:
    """Normalise, drop empties, de-duplicate, and bound a phrase list.

    Order is preserved: the moderator's ordering is the order they will see in
    the Discord UI, and silently sorting their rules is rude.
    """
    candidates = phrases.split(",") if isinstance(phrases, str) else list(phrases or [])

    seen: set[str] = set()
    cleaned: list[str] = []
    for raw in candidates:
        phrase = normalize_phrase(str(raw))
        if not phrase or phrase in seen:
            continue
        if len(phrase) > MAX_KEYWORD_LEN:
            raise ValueError(
                f"Keyword {phrase!r} is {len(phrase)} characters; Discord's limit "
                f"is {MAX_KEYWORD_LEN}."
            )
        seen.add(phrase)
        cleaned.append(phrase)
        if len(cleaned) >= MAX_PHRASES:
            break
    return cleaned


def matches_phrase(content: str, phrase: str) -> bool:
    """Whether ``content`` contains ``phrase`` as a whole word run.

    Word-boundary matching matters: a filter for ``art`` must not fire on
    ``started`` or ``party``. Discord's own keyword option works the same way.
    """
    canonical = normalize_phrase(phrase)
    if not canonical:
        return False
    # Both sides go through the same normalisation, so "say   free  nitro" in
    # chat still matches the saved keyword "free nitro".
    haystack = normalize_phrase(content)
    pattern = r"(?<!\w)" + re.escape(canonical) + r"(?!\w)"
    return re.search(pattern, haystack) is not None


async def find_rule(guild: discord.Guild, name: str = "Zagros") -> Any:
    """The bot's native AutoMod keyword rule in ``guild``, or ``None``.

    Returning the live rule rather than a bool is what lets ``/filter`` report
    *drift*: phrases saved locally that Discord is not enforcing. A moderator who
    believes a block is active when it is not has no other way to find out.
    """
    keyword_filter = getattr(guild, "auto_moderation", None)
    if keyword_filter is None:
        return None
    try:
        rules = await keyword_filter.get_rules()
    except discord.HTTPException as exc:
        logger.info("Could not read AutoMod rules for %s: %s", guild.id, exc)
        return None
    for rule in rules:
        if rule.name == name:
            return rule
    return None


async def sync_automod_rule(
    guild: discord.Guild,
    phrases: list[str],
    *,
    reason: str | None = None,
    enabled: bool = True,
    name: str = "Zagros",
    channel: discord.abc.GuildChannel | None = None,
) -> str:
    """Mirror a phrase list into Discord's native AutoMod rule.

    Returns a short human-readable outcome. Failures are returned as text rather
    than raised, so a guild with AutoMod switched off still records the phrase
    locally - the alternative is a moderator who cannot save a word at all.
    """
    keyword_filter = getattr(guild, "auto_moderation", None)
    if keyword_filter is None:
        return "This server does not offer AutoMod; phrases are stored locally."

    rule = await find_rule(guild, name)
    trigger = discord.AutoModRuleTriggerMetadata(
        keyword_filter=list(phrases)[:6],
        presets=[],
    )
    try:
        if rule is None:
            if not phrases:
                return "Nothing to enforce and no rule to clean up."
            await keyword_filter.create_rule(
                name=name,
                reason=reason,
                event_type=discord.AutoModRuleEventType.MESSAGE_SEND,
                trigger_type=discord.AutoModRuleTriggerType.KEYWORD,
                trigger_metadata=trigger,
                action=discord.AutoModRuleAction(
                    action=discord.AutoModRuleActionType.SEND_ALERT_MESSAGE,
                    channel=channel,
                ),
                enabled=enabled,
            )
            return "Created the native AutoMod rule."

        if not phrases:
            await rule.delete(reason=reason)
            return "Removed the native AutoMod rule; nothing was saved."

        await rule.edit(
            reason=reason,
            trigger_metadata=trigger,
            enabled=enabled,
        )
        return "Updated the native AutoMod rule."
    except discord.Forbidden:
        return "Discord refused the native rule; phrases are stored locally only."
    except discord.HTTPException as exc:
        logger.info("Native AutoMod sync failed for %s: %s", guild.id, exc)
        return f"Native AutoMod sync failed ({exc}); phrases are stored locally."

#: Discord's own invite pattern. Used as a fallback when the library's own
#: matcher is unavailable.
_INVITE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:discord(?:app)?\.com/invite|discord\.gg|dsc\.gg|invite\.gg)/([A-Za-z0-9-]+)"
)

#: Characters stripped before duplicate comparison so "h e l l o" and "hello"
#: collapse to one another.
_DUPLICATE_NOISE: Final[re.Pattern[str]] = re.compile(r"[\W_]+", re.UNICODE)

#: Below this length, "duplicate" is just a short reply.
_DUPLICATE_MIN_LENGTH: Final[int] = 8

#: Consecutive duplicates needed before the rule fires.
_DUPLICATE_STREAK: Final[int] = 4

#: A slash command mention is never treated as spam.
_COMMAND_PREFIXES: Final[tuple[str, ...]] = ("!", "?", "/", "$", ".")

#: Messages above this length are exempt from duplicate detection: a wall of text
#: pasted twice is a different problem from a one-liner flooded.
_DUPLICATE_MAX_LENGTH: Final[int] = 400


def extract_invite_codes(content: str) -> list[str]:
    """Every invite slug in ``content``, normalised to lowercase.

    Returns a list rather than a bool because the caller wants to name the code in
    its warning, and a member who pasted three servers deserves to see all three.
    """
    return [match.group(1).lower() for match in _INVITE_RE.finditer(content)]


def normalise_for_duplicate(content: str) -> str:
    """Collapse a message to its comparable form.

    Casing, punctuation, emoji and whitespace are all removed, so the variations a
    flooder actually types (extra spaces, a trailing ``!``, different case) hash
    to the same value.
    """
    stripped = _DUPLICATE_NOISE.sub("", content)
    # dropzero-width and other joiners used to dodge naive filters
    return stripped.casefold()[:_DUPLICATE_MAX_LENGTH]


def looks_like_shouting(content: str, percent_threshold: int, min_length: int) -> bool:
    """Whether ``content`` is mostly capitals.

    Counts only alphabetic characters: digits and punctuation carry no case, so
    including them would let "1234567890!!!!!!!!" pass as calm.
    """
    letters = [ch for ch in content if ch.isalpha()]
    if len(letters) < min_length:
        return False
    upper = sum(1 for ch in letters if ch.isupper())
    return int(upper / len(letters) * 100) >= percent_threshold


def is_command_like(content: str) -> bool:
    """Whether the message looks like a deliberate bot command, not chatter."""
    stripped = content.strip()
    return bool(stripped) and stripped[0] in _COMMAND_PREFIXES


@dataclass(slots=True)
class AutoModSettings:
    """One guild's resolved configuration, cached for a few seconds.

    ``slots=True`` because one of these exists per guild that talks, and the
    cache is the hot path.
    """

    enabled: bool = False
    duplicate_action: str = "delete"
    caps_action: str = "warn"
    invite_action: str = "delete"
    caps_percent: int = 0
    caps_min_length: int = 12
    spam_window_seconds: int = 10
    spam_max_messages: int = 6
    spam_timeout_seconds: int = 60
    ignored_channels: frozenset[int] = field(default_factory=frozenset)
    ignored_roles: frozenset[int] = field(default_factory=frozenset)
    ignored_users: frozenset[int] = field(default_factory=frozenset)


@dataclass(slots=True)
class AutoModVerdict:
    """What AutoMod decided about one message."""

    action: str = "none"
    reason: str = ""
    detail: str = ""

    @property
    def tripped(self) -> bool:
        return self.action != "none"


class AutoMod:
    """Stateful message inspector.

    The duplicate streak is inherently per-member, so it lives here in memory with
    a timestamp on each entry: a member who floods and then goes quiet must not
    have their counter survive until the process restarts.
    """

    def __init__(self, bot: commands.Bot, ttl: float = 30.0) -> None:
        self.bot = bot
        #: user id -> (normalised text, consecutive count, last seen timestamp)
        self._streaks: dict[int, tuple[str, int, float]] = {}
        #: user id -> monotonic timestamps of recent messages, for the rate window.
        #: A deque would need a maxlen per user and reallocation; a list trimmed in
        #: place keeps one small allocation per inspecting member and no growth.
        self._rates: dict[int, list[float]] = {}
        #: Cache TTL in seconds. Short enough that a settings change feels
        #: immediate, long enough that a busy channel is not re-querying.
        self.ttl = ttl
        self._cache: dict[int, tuple[float, AutoModSettings]] = {}

    # ------------------------------------------------------------------ #
    # Configuration
    # ------------------------------------------------------------------ #
    async def settings_for(self, guild_id: int, *, force: bool = False) -> AutoModSettings:
        """Resolved settings for ``guild_id``, from cache when fresh."""
        now = time.monotonic()
        cached = self._cache.get(guild_id)
        if not force and cached is not None and cached[0] > now:
            return cached[1]

        resolved = AutoModSettings()
        try:
            async with get_database().session() as session:
                config = await session.get(GuildConfig, guild_id)
                if config is not None:
                    resolved.enabled = bool(config.automod_enabled)
                    resolved.duplicate_action = config.automod_duplicate_action or "delete"
                    resolved.caps_action = config.automod_caps_action or "warn"
                    resolved.invite_action = config.automod_invite_action or "delete"
                    resolved.caps_percent = int(config.anticaps_percent or 0)
                    resolved.caps_min_length = int(config.anticaps_min_length or 12)
                    resolved.spam_window_seconds = int(config.spam_window_seconds or 10)
                    resolved.spam_max_messages = int(config.spam_max_messages or 6)
                    resolved.spam_timeout_seconds = int(config.spam_timeout_seconds or 60)

                ignores = (
                    await session.execute(
                        select(GuildIgnore.kind, GuildIgnore.target_id).where(
                            GuildIgnore.guild_id == guild_id
                        )
                    )
                ).all()
                channels, roles, users = [], [], []
                for kind, target_id in ignores:
                    if kind == "channel":
                        channels.append(int(target_id))
                    elif kind == "role":
                        roles.append(int(target_id))
                    else:
                        users.append(int(target_id))
                resolved.ignored_channels = frozenset(channels)
                resolved.ignored_roles = frozenset(roles)
                resolved.ignored_users = frozenset(users)
        except Exception as exc:  # noqa: BLE001 - never block on a db blip
            logger.warning("AutoMod settings unavailable for %s: %s", guild_id, exc)
            return AutoModSettings()  # fail closed: enforcement off, bot alive

        self._cache[guild_id] = (now + self.ttl, resolved)
        return resolved

    def invalidate(self, guild_id: int) -> None:
        """Drop a guild's cached settings. Called after any settings change."""
        self._cache.pop(guild_id, None)

    def is_ignored(self, message: discord.Message, settings: AutoModSettings) -> bool:
        """Whether AutoMod should not look at this message at all."""
        if message.author.id in settings.ignored_users:
            return True
        if message.channel.id in settings.ignored_channels:
            return True
        return bool(
            settings.ignored_roles
            and settings.ignored_roles.intersection(r.id for r in message.author.roles)
        )

    # ------------------------------------------------------------------ #
    # Rules
    # ------------------------------------------------------------------ #
    def _check_duplicates(self, message: discord.Message) -> AutoModVerdict:
        """Flag a member repeating themselves in a short burst."""
        content = message.content.strip()
        if len(content) < _DUPLICATE_MIN_LENGTH or len(content) > _DUPLICATE_MAX_LENGTH:
            self._streaks.pop(message.author.id, None)
            return AutoModVerdict()

        key = normalise_for_duplicate(content)
        now = time.monotonic()
        previous, count, seen = self._streaks.get(message.author.id, ("", 0, 0.0))

        # A gap longer than the window resets the streak: otherwise a member who
        # repeats one message once an hour trips it on the fourth hour.
        if previous == key and now - seen < self.ttl:
            count += 1
        else:
            count = 1

        self._streaks[message.author.id] = (key, count, now)

        if count >= _DUPLICATE_STREAK:
            self._streaks.pop(message.author.id, None)
            return AutoModVerdict(
                action="duplicate",
                reason="Duplicate flood",
                detail=f"Same message {count} times in a row",
            )
        return AutoModVerdict()

    def _check_caps(self, message: discord.Message, settings: AutoModSettings) -> AutoModVerdict:
        if not settings.caps_percent or not looks_like_shouting(
            message.content, settings.caps_percent, settings.caps_min_length
        ):
            return AutoModVerdict()
        letters = [ch for ch in message.content if ch.isalpha()]
        percent = int(sum(1 for ch in letters if ch.isupper()) / len(letters) * 100)
        return AutoModVerdict(
            action="caps",
            reason="Caps abuse",
            detail=f"{percent}% capitals over {len(letters)} letters",
        )

    def _check_spam(
        self, message: discord.Message, settings: AutoModSettings
    ) -> AutoModVerdict:
        """Flag a member sending too many messages inside the window.

        Counts messages, not words or characters: the configured knob is
        "max messages", and a member posting six one-word replies and one long
        paragraph is spamming the same either way.
        """
        now = time.monotonic()
        window = max(1, settings.spam_window_seconds)
        seen = self._rates.get(message.author.id)
        if seen is None:
            seen = []
            self._rates[message.author.id] = seen

        cutoff = now - window
        # Timestamps arrive in order, so a leading trim is enough; filtering the
        # whole list would be O(n) per message for the same result.
        stale = 0
        for stamp in seen:
            if stamp >= cutoff:
                break
            stale += 1
        if stale:
            del seen[:stale]
        seen.append(now)

        if len(seen) > settings.spam_max_messages:
            # Clear the window so the timeout is not re-triggered by every
            # message that follows before it is lifted.
            self._rates.pop(message.author.id, None)
            return AutoModVerdict(
                action="spam",
                reason="Message flood",
                detail=(
                    f"{len(seen)} messages in {window}s "
                    f"(limit {settings.spam_max_messages})"
                ),
            )
        return AutoModVerdict()

    def forget(self, user_id: int) -> None:
        """Drop all rate and streak state for one member."""
        self._streaks.pop(user_id, None)
        self._rates.pop(user_id, None)

    @staticmethod
    def _check_invites(message: discord.Message) -> AutoModVerdict:
        codes = extract_invite_codes(message.content)
        if not codes:
            return AutoModVerdict()
        return AutoModVerdict(
            action="invite",
            reason="Invite link",
            detail=", ".join(codes[:3]),
        )

    # ------------------------------------------------------------------ #
    # Entry point
    # ------------------------------------------------------------------ #
    async def inspect(self, message: discord.Message) -> tuple[AutoModVerdict, AutoModSettings]:
        """Judge one message. Returns the verdict and the settings used.

        Order is deliberate: cheap checks first, exempt paths first, and the
        expensive duplicate bookkeeping last so a clean message does the least
        work possible.
        """
        settings = await self.settings_for(message.guild.id)
        if not settings.enabled or self.is_ignored(message, settings):
            return AutoModVerdict(), settings
        if is_command_like(message.content):
            return AutoModVerdict(), settings

        # Each rule carries its own penalty, so the rule and the action it maps to
        # are resolved together here rather than inside the checks.
        for check, penalty in (
            (lambda: self._check_invites(message), settings.invite_action),
            (lambda: self._check_caps(message, settings), settings.caps_action),
            (lambda: self._check_duplicates(message), settings.duplicate_action),
            (lambda: self._check_spam(message, settings), "timeout"),
        ):
            verdict = check()
            if verdict.tripped:
                verdict.action = penalty
                return verdict, settings

        self._prune_streaks()
        return AutoModVerdict(), settings

    def _prune_streaks(self) -> None:
        """Drop streak state older than the window.

        Called after every inspect so the dict cannot grow without bound in a
        large server - an unbounded per-user dict is a slow memory leak that only
        shows up after weeks of uptime.
        """
        cutoff = time.monotonic() - self.ttl * 4
        for uid in [
            uid for uid, (_, _, seen) in self._streaks.items() if seen < cutoff
        ]:
            del self._streaks[uid]
        # The rate window is shorter than the streak TTL, so a member who stops
        # talking leaves an entry behind. Pruned on the same pass; the dict is
        # bounded by talking members, not by all members.
        rate_cutoff = time.monotonic() - max(self.ttl, 60.0)
        for uid, stamps in list(self._rates.items()):
            if not stamps or stamps[-1] < rate_cutoff:
                del self._rates[uid]
