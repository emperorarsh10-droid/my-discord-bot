"""Custom ``app_commands`` transformers.

A transformer converts a raw string into a typed Python value *before* the
command body runs, which means:

*   the signature stays honest (``timedelta``, not ``str``),
*   invalid input is rejected by discord.py itself, so the body never has to
    branch on parsing,
*   the failure surfaces as a clean :class:`app_commands.TransformerError`,
    which ``core.errors`` already translates for the user.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Final

import discord
from discord import app_commands

__all__ = ["DURATION_PATTERN", "Duration", "parse_duration"]

#: ``1d``, ``2h30m``, ``90m 15s``, ``1w 2d 3h`` — order-independent, repeatable.
DURATION_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?P<amount>\d{1,4})\s*(?P<unit>mo|[smhdwy])", re.IGNORECASE
)

_UNIT_SECONDS: Final[dict[str, int]] = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
    "mo": 2592000,  # 30 days; calendar months are ambiguous in a TTL
    "y": 31536000,  # 365 days
}

#: Hard ceiling — a mute longer than a year is a ban in disguise.
MAX_DURATION: Final[timedelta] = timedelta(days=365)


def parse_duration(raw: str) -> timedelta:
    """Parse a human duration into a ``timedelta``.

    Accepts compound forms: ``"1d 12h"``, ``"90m"``, ``"2h30m"``.

    Raises:
        app_commands.BadArgument: if the string is empty, malformed, zero-length
            or beyond :data:`MAX_DURATION`.
    """
    text = raw.strip().lower()
    if not text:
        raise app_commands.BadArgument("duration is empty")

    matches = list(DURATION_PATTERN.finditer(text))
    if not matches:
        raise app_commands.BadArgument(
            f"`{raw}` is not a duration I understand. Try `30m`, `12h`, `7d` or `1w 2d`."
        )

    # Reject leftovers such as "1h banana" so typos are never silently ignored.
    consumed = "".join(m.group(0) for m in matches)
    if re.sub(r"\s+", "", consumed) != re.sub(r"\s+", "", text):
        raise app_commands.BadArgument(
            f"`{raw}` contains characters I cannot parse. Supported units: "
            "`s`, `m`, `h`, `d`, `w`, `mo`, `y`."
        )

    total = 0
    for match in matches:
        total += int(match.group("amount")) * _UNIT_SECONDS[match.group("unit").lower()]

    delta = timedelta(seconds=total)
    if delta <= timedelta(0):
        raise app_commands.BadArgument("duration must be greater than zero")
    if delta > MAX_DURATION:
        raise app_commands.BadArgument("duration cannot exceed 1y")
    return delta


class Duration(app_commands.Transformer):
    """``timedelta`` transformer for slash command parameters.

    discord.py instantiates a transformer class with **no arguments** while it
    walks the callback signature, so the instance state here is only ever the
    per-call value; the option's name and description come from the parameter
    itself (see ``@app_commands.describe`` at each call site).

    Usage::

        @app_commands.command()
        @app_commands.describe(duration="A duration such as 30m, 12h or 7d")
        async def mute(
            self,
            interaction: discord.Interaction,
            target: discord.Member,
            duration: Duration,
        ) -> None:
            ...
    """

    def __init__(self) -> None:
        super().__init__()
        self.value: timedelta = timedelta()

    @property
    def type(self) -> discord.AppCommandOptionType:
        return discord.AppCommandOptionType.string

    @classmethod
    async def transform(
        cls, interaction: discord.Interaction, value: str
    ) -> timedelta:
        return parse_duration(value)

    def __str__(self) -> str:
        return str(self.value)

    def __repr__(self) -> str:
        return f"Duration({self.value!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Duration):
            return self.value == other.value
        return NotImplemented
