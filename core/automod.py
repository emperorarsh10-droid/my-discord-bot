"""Native AutoMod rule management.

The bot does **not** read message content. ``message_content`` is a privileged
intent, and requesting it without the portal toggle kills login outright — which
is exactly the wrong failure for a moderation bot. So message filtering is done
by Discord itself, and this module is the thin layer that keeps our local
blacklist and one native keyword rule in agreement.

Why a local table at all? Because a rule cannot answer "what is filtered in this
server?" without a ``GET /guilds/{id}/auto-moderation/rules`` round trip per
invocation, and because the moderation ledger needs to remember what the bot was
asked to block. The table is the source of truth for *intent*; the native rule
is the source of truth for *enforcement*.

The sync is deliberately total: one rule, named :data:`RULE_NAME`, holding the
whole phrase list. Partial keyword rules would let ``/filter remove`` silently
stop working as soon as a second rule existed.
"""

from __future__ import annotations

import logging
from typing import Final

import discord

__all__ = [
    "MAX_KEYWORD_LEN",
    "MAX_PHRASES",
    "RULE_NAME",
    "RULE_REASON",
    "clean_phrases",
    "find_rule",
    "matches_phrase",
    "normalize_phrase",
    "sync_automod_rule",
]

logger = logging.getLogger("zagrosian.automod")

#: The single rule this bot owns. Anything else in the server is the user's.
RULE_NAME: Final[str] = "Zagrosian Eye · blocked phrases"

#: Shown in the Discord audit log for the rule, so a server admin can see who
#: changed it and why.
RULE_REASON: Final[str] = "Managed by The Zagrosian Eye via /filter"

#: Discord's own keyword rule limit is 1000 per rule. Staying one below leaves
#: room for a phrase that only becomes invalid at submit time.
MAX_PHRASES: Final[int] = 1000

#: Discord rejects keywords longer than 60 characters.
MAX_KEYWORD_LEN: Final[int] = 60

#: Shown on the blocked message, so the author knows why and who to ask.
BLOCK_MESSAGE: Final[str] = (
    "That phrase is blocked in this server. Remove it, or ask a moderator to "
    "unblock it with `/filter remove`."
)


def normalize_phrase(raw: str) -> str:
    """Canonical form of a phrase: collapsed whitespace, case-folded.

    Casing is dropped deliberately — Discord's keyword filter is
    case-insensitive, so storing two spellings of the same word would spend the
    rule's budget on a duplicate that never matches anything new.
    """
    return " ".join(raw.split()).casefold()


def clean_phrases(
    phrases: list[str], *, limit: int = MAX_PHRASES
) -> list[str]:
    """Normalize, drop empties, de-duplicate and bound a phrase list.

    Order is first-seen, so removing the newest phrase from the command surface
    removes the newest row from the rule too.
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in phrases:
        phrase = normalize_phrase(raw)[:MAX_KEYWORD_LEN].strip()
        if not phrase or phrase in seen:
            continue
        seen.add(phrase)
        cleaned.append(phrase)
        if len(cleaned) >= limit:
            break
    return cleaned


def matches_phrase(text: str, phrases: list[str]) -> bool:
    """Case-insensitive substring match, used for local mirrors and tests.

    Native AutoMod is what actually blocks messages in production; this exists so
    the same rule can be verified offline, and so a caller that already holds the
    text does not need a second network round trip to ask "would this hit?".
    """
    if not text or not phrases:
        return False
    haystack = text.casefold()
    return any(phrase in haystack for phrase in phrases)


async def find_rule(guild: discord.Guild) -> discord.AutoModRule | None:
    """Return the bot's rule in ``guild``, or ``None`` if it has none.

    Filters on the local cache, not on a fetch: the rule list is part of the
    guild payload, so this is a synchronous attribute read.
    """
    for rule in guild.automod_rules:
        if rule.name == RULE_NAME:
            return rule
    return None


def _live_phrases(rule: discord.AutoModRule) -> list[str]:
    """The keyword list Discord is actually enforcing for ``rule``."""
    return list(rule.trigger.keyword_filter or [])


async def sync_automod_rule(
    guild: discord.Guild, phrases: list[str], *, reason: str = RULE_REASON
) -> str:
    """Make the native rule in ``guild`` hold exactly ``phrases``.

    Args:
        guild: The server whose rule is being reconciled.
        phrases: Already-normalized phrases. Run them through
            :func:`clean_phrases` first.
        reason: Audit-log text.

    Returns:
        A short human sentence describing what happened, for the command reply.

    Raises:
        discord.Forbidden: The bot lacks the *Manage Guild* permission in this
            server. Callers must handle this — the local blacklist is still
            written, so the phrases are remembered and the native rule can be
            re-synced later once the permission is granted.
    """
    cleaned = clean_phrases(phrases)
    rule = await find_rule(guild)

    if not cleaned:
        if rule is None:
            return "No phrases are filtered and no rule exists — nothing to change."
        await rule.delete(reason=reason)
        logger.info("Deleted automod rule %s in guild %s", rule.id, guild.id)
        return f"Filter is now empty; removed the rule `{RULE_NAME}`."

    trigger = discord.AutoModTrigger(
        type=discord.AutoModRuleTriggerType.keyword,
        keyword_filter=cleaned,
    )
    # The action type is inferred from which field is set, so a bare
    # ``custom_message`` is a block-with-a-reason, not a timeout.
    action = discord.AutoModRuleAction(custom_message=BLOCK_MESSAGE)

    if rule is None:
        created = await guild.create_automod_rule(
            name=RULE_NAME,
            event_type=discord.AutoModRuleEventType.message_send,
            trigger=trigger,
            actions=[action],
            enabled=True,
            reason=reason,
        )
        logger.info(
            "Created automod rule %s in guild %s with %d phrase(s)",
            created.id, guild.id, len(cleaned),
        )
        return f"Created `{RULE_NAME}` with **{len(cleaned)}** blocked phrase(s)."

    already = rule.enabled and _live_phrases(rule) == cleaned
    if already:
        return f"Native rule is already in sync with **{len(cleaned)}** phrase(s)."

    was_enabled = rule.enabled
    await rule.edit(
        trigger=trigger,
        actions=[action],
        enabled=True,
        reason=reason,
    )
    logger.info(
        "Synced automod rule %s in guild %s to %d phrase(s)",
        rule.id, guild.id, len(cleaned),
    )
    verb = "re-enabled and updated" if not was_enabled else "updated"
    return f"Rule {verb}: **{len(cleaned)}** blocked phrase(s) live on Discord."
