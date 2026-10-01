"""Visual identity: colours, embed factories and shared formatting helpers.

Every user-facing message in the bot is built here so the presentation stays
consistent across cogs. If a message needs a new shape, add a factory — do not
hand-roll ``discord.Embed`` inside a command.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import discord

__all__ = [
    "BRAND_COLOR",
    "BRAND_NAME",
    "COLOR_ERROR",
    "COLOR_INFO",
    "COLOR_NEUTRAL",
    "COLOR_SUCCESS",
    "COLOR_WARNING",
    "EMBED_FOOTER",
    "add_action_fields",
    "base_embed",
    "describe_channel",
    "describe_member",
    "describe_role",
    "duration_from_timedelta",
    "error_embed",
    "format_duration",
    "info_embed",
    "parse_hex_color",
    "set_footer",
    "success_embed",
    "timestamp",
    "truncate",
    "warning_embed",
]

BRAND_NAME: Final[str] = "The Zagrosian Eye"
BRAND_COLOR: Final[int] = 0x1F6F6B
COLOR_SUCCESS: Final[int] = 0x2E7D32
COLOR_ERROR: Final[int] = 0xB3261E
COLOR_WARNING: Final[int] = 0xB26A00
COLOR_INFO: Final[int] = 0x2F5D8C
COLOR_NEUTRAL: Final[int] = 0x2C3338

EMBED_FOOTER: Final[str] = f"{BRAND_NAME} • the eye never blinks"


def timestamp(value: datetime | None = None) -> discord.utils.MISSING | str:
    """Discord-native timestamp markup; a relative one by default."""
    moment = value or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return f"<t:{int(moment.timestamp())}:R>"


def truncate(text: str, limit: int = 1024) -> str:
    """Clip text to a Discord field limit, marking the cut."""
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1]}…"


#: ``#1F6F6B``, ``1f6f6b`` and ``0x1F6F6B`` are the three shapes people paste.
_HEX_COLOR: Final[re.Pattern[str]] = re.compile(r"^(?:#|0x)?([0-9a-fA-F]{6})$")


def parse_hex_color(raw: str) -> int | None:
    """Turn a pasted hex colour into a Discord colour int.

    Accepts ``#RRGGBB``, ``RRGGBB`` and ``0xRRGGBB``, case-insensitively. Three
    digit shorthand is rejected on purpose — it is a CSS convention, and
    ``#fff`` in a Discord embed is a typo worth reporting, not a guess.

    Returns:
        The colour as an int, or ``None`` when the input is not a hex colour.
    """
    match = _HEX_COLOR.match((raw or "").strip())
    if match is None:
        return None
    return int(match.group(1), 16)


def format_duration(delta: timedelta | None) -> str:
    """Render a ``timedelta`` compactly: ``3d 04h 12m``."""
    if delta is None:
        return "permanent"
    total = int(delta.total_seconds())
    sign = "-" if total < 0 else ""
    total = abs(total)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs and not parts:
        parts.append(f"{secs}s")
    return sign + (" ".join(parts) if parts else "0s")


def duration_from_timedelta(delta: timedelta | None) -> str:
    """Alias kept for call sites that read better with the explicit name."""
    return format_duration(delta)


def base_embed(
    *,
    title: str | None = None,
    description: str | None = None,
    colour: int = BRAND_COLOR,
    author: discord.abc.User | discord.Member | None = None,
    footer: str | None = None,
    thumbnail: str | None = None,
) -> discord.Embed:
    """Construct an embed pre-wired with the brand identity."""
    embed = discord.Embed(
        title=title,
        description=description,
        colour=colour,
        timestamp=datetime.now(UTC),
    )
    if author is not None:
        embed.set_author(
            name=getattr(author, "display_name", None) or str(author),
            icon_url=getattr(author, "display_avatar", None) and author.display_avatar.url,
        )
    if thumbnail is not None:
        embed.set_thumbnail(url=thumbnail)
    set_footer(embed, footer)
    return embed


def set_footer(embed: discord.Embed, text: str | None = None) -> discord.Embed:
    embed.set_footer(text=text or EMBED_FOOTER)
    return embed


def success_embed(description: str, **kwargs: Any) -> discord.Embed:
    kwargs.setdefault("colour", COLOR_SUCCESS)
    return base_embed(description=description, **kwargs)


def error_embed(description: str, **kwargs: Any) -> discord.Embed:
    kwargs.setdefault("colour", COLOR_ERROR)
    return base_embed(description=description, **kwargs)


def warning_embed(description: str, **kwargs: Any) -> discord.Embed:
    kwargs.setdefault("colour", COLOR_WARNING)
    return base_embed(description=description, **kwargs)


def info_embed(description: str, **kwargs: Any) -> discord.Embed:
    kwargs.setdefault("colour", COLOR_INFO)
    return base_embed(description=description, **kwargs)


def describe_member(member: discord.Member) -> str:
    """``Name (ID) · Top role`` — the format used in every action log."""
    top_role = member.top_role.name if member.roles and member.top_role else "no role"
    return f"**{member}** (`{member.id}`) · {top_role}"


def describe_role(role: discord.Role) -> str:
    return f"**{role.name}** (`{role.id}`)"


def describe_channel(channel: discord.abc.GuildChannel) -> str:
    return f"#{channel.name} (`{channel.id}`)"


def add_action_fields(
    embed: discord.Embed,
    *,
    action: str,
    target: str,
    moderator: str,
    reason: str | None,
    case_ref: str | None = None,
    duration: timedelta | None = None,
    extra: Sequence[tuple[str, str, bool]] | Iterable[tuple[str, str, bool]] = (),
) -> discord.Embed:
    """Standard field layout for a moderation action log."""
    embed.add_field(name="Action", value=action, inline=True)
    if case_ref:
        embed.add_field(name="Case", value=case_ref, inline=True)
    embed.add_field(name="Target", value=truncate(target, 1024), inline=False)
    embed.add_field(name="Moderator", value=truncate(moderator, 1024), inline=True)
    embed.add_field(
        name="Reason",
        value=truncate(reason or "No reason provided", 1024),
        inline=True,
    )
    if duration is not None:
        embed.add_field(name="Expires", value=timestamp(datetime.now(UTC) + duration), inline=True)
    for name, value, inline in extra:
        embed.add_field(name=name, value=truncate(value, 1024), inline=inline)
    return embed
