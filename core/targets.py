"""Bulk-target parsing for the batch moderation commands.

Discord slash commands take a single ``user`` option, so a command that acts on
forty accounts has to accept them as text. Users paste whatever their client
gives them: raw snowflakes, ``<@123>`` pings, ``<@!123>`` legacy mentions, a
space/comma/semicolon soup of all three, usually with a couple of typos mixed in.

This module is the one place that knows how to read that soup. It is pure —
no Discord objects, no database, no network — so it is exhaustively testable
and can never fail a command handler for the wrong reason.

The contract that matters: **garbage is reported, never raised on.** A moderator
who pastes 40 valid IDs and one stray character must still get 40 bans and a
line explaining the 41st token, not a traceback.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = [
    "MAX_BULK_TARGETS",
    "SPLIT_PATTERN",
    "USABLE_SNOWFLAKE",
    "parse_user_ids",
]

#: Discord snowflakes are 64-bit; the shortest ones ever minted are 17 digits
#: and the longest are 20 (the epoch will not outlive us). Anything outside that
#: range is a typo, not a user.
USABLE_SNOWFLAKE: Final[re.Pattern[str]] = re.compile(r"^\d{15,20}$")

#: Whitespace, commas and semicolons all separate targets in practice.
SPLIT_PATTERN: Final[re.Pattern[str]] = re.compile(r"[\s,;]+")

#: Upper bound on one invocation. Discord allows at most 100 bans per bulk
#: action server-side, and a moderator pasting 400 IDs wants two runs, not a
#: rate-limited 400-request burst.
MAX_BULK_TARGETS: Final[int] = 100

#: ``<@123>`` / ``<@!123>`` / ``<@&123>`` style mentions.
_MENTION: Final[re.Pattern[str]] = re.compile(r"^<@!?(?P<id>\d{15,20})>$")


def parse_user_ids(
    raw: str, *, limit: int = MAX_BULK_TARGETS
) -> tuple[list[int], list[str]]:
    """Split a pasted blob into usable user IDs and the tokens we refused.

    Args:
        raw: Whatever the moderator pasted — mentions, bare IDs, mixed
            separators, arbitrary whitespace.
        limit: Hard ceiling on accepted IDs. Tokens beyond the ceiling are
            reported as rejected rather than silently dropped.

    Returns:
        ``(ids, rejected)`` where ``ids`` is de-duplicated in first-seen order
        and ``rejected`` holds the original tokens that were not usable, so the
        error message can quote back exactly what was ignored.

    Example:
        >>> parse_user_ids("<@!111> 222,222;oops")
        ([111, 222], ['oops'])
    """
    ids: list[int] = []
    rejected: list[str] = []
    seen: set[int] = set()

    for token in SPLIT_PATTERN.split(raw.strip()):
        if not token:
            continue

        match = _MENTION.match(token)
        candidate = match.group("id") if match else token
        # Tolerate ``<@123`` and ``@123>`` from a bad paste: strip the angle
        # brackets and try again before giving up on the token.
        if match is None and token.startswith("<@"):
            candidate = token.lstrip("<@!").rstrip(">")

        if not USABLE_SNOWFLAKE.match(candidate):
            rejected.append(token)
            continue

        value = int(candidate)
        if value in seen:
            continue
        if len(ids) >= limit:
            rejected.append(token)
            continue
        seen.add(value)
        ids.append(value)

    return ids, rejected
