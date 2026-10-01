"""Guild configuration backups: serialize, seal, restore.

A backup is a *configuration* snapshot, not a Discord object dump. Roles and
channels are recreated by name; permissions and overwrites are recorded but
**never** re-applied on restore, because a restore that silently hands everyone
*Manage Server* back is a worse outcome than an incomplete one.

The envelope is an HMAC-SHA256 keystream XOR over the JSON, then base64. That is
obfuscation, not encryption — and it is honest about it: the key is the bot
token, which is already in the process, so the threat being defended against is
"a stray ``.zeye`` file pasted in a public channel", not a determined attacker.
The MAC is the part that matters — it is what proves a file came from this bot
and has not been edited.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
from pathlib import Path
from typing import Any, Final

import discord

from core.embeds import truncate

__all__ = [
    "BACKUP_SUFFIX",
    "FORMAT",
    "MAX_BACKUP_NAME",
    "SAFE_NAME",
    "backup_path",
    "decode_payload",
    "encode_payload",
    "list_backup_names",
    "restore_guild",
    "serialize_guild",
    "summarize_backup",
]

logger = logging.getLogger("zagrosian.backup")

#: Envelope marker, bumped whenever the payload shape changes incompatibly.
FORMAT: Final[str] = "zeye-backup-1"

#: On-disk suffix for exported files.
BACKUP_SUFFIX: Final[str] = ".zeye"

#: Discord role/channel name bound; anything longer cannot be restored anyway.
MAX_BACKUP_NAME: Final[str] = 64

#: Filesystem-safe characters for an operator-supplied backup name.
SAFE_NAME: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9._-]+")

#: Channel types that carry no restoreable configuration of their own.
_SKIP_CHANNEL_TYPES: Final[frozenset[discord.ChannelType]] = frozenset(
    {discord.ChannelType.voice, discord.ChannelType.stage_voice}
)


# --------------------------------------------------------------------------- #
# Envelope
# --------------------------------------------------------------------------- #
def _keystream(key: str, length: int) -> bytes:
    """HMAC-SHA256 counter-mode keystream: block *i* is ``HMAC(key, i)``.

    Counter mode rather than one long hash so the output length is arbitrary and
    independent of the digest size.
    """
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hmac.new(
            key.encode("utf-8"),
            counter.to_bytes(8, "big"),
            hashlib.sha256,
        ).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def encode_payload(payload: dict[str, Any], key: str) -> str:
    """Seal ``payload`` into a portable, tamper-evident string.

    Args:
        payload: JSON-serializable backup body.
        key: Sealing key. The bot token, so backups die with the bot.

    Returns:
        Base64 text of ``magic || version || mac || ciphertext``.

    Raises:
        ValueError: The payload is not JSON-serializable, or the key is empty.
    """
    if not key:
        raise ValueError("a sealing key is required")

    try:
        plain = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"payload is not JSON-serializable: {exc}") from exc

    ciphertext = bytes(
        a ^ b for a, b in zip(plain, _keystream(key, len(plain)), strict=True)
    )
    mac = hmac.new(key.encode("utf-8"), ciphertext, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(FORMAT.encode("ascii") + mac + ciphertext).decode("ascii")


def decode_payload(envelope: str, key: str) -> dict[str, Any]:
    """Open an envelope produced by :func:`encode_payload`.

    Raises:
        ValueError: The envelope is truncated, not ours, keyed with a different
            key, or has been edited. All four are reported the same way on
            purpose — a caller should not learn *which* check failed.
    """
    if not key:
        raise ValueError("a sealing key is required")

    try:
        raw = base64.urlsafe_b64decode(envelope.strip().encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError("backup file is not valid base64") from exc

    header = len(FORMAT) + 32
    if len(raw) <= header or raw[: len(FORMAT)].decode("ascii", "replace") != FORMAT:
        raise ValueError(f"not a {FORMAT} envelope")

    mac = raw[len(FORMAT) : header]
    ciphertext = raw[header:]
    expected = hmac.new(key.encode("utf-8"), ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected):
        raise ValueError("integrity check failed: wrong key, or the file was edited")

    plain = bytes(
        a ^ b for a, b in zip(ciphertext, _keystream(key, len(ciphertext)), strict=True)
    )
    try:
        return json.loads(plain.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"backup body is corrupt: {exc}") from exc


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #
def serialize_guild(guild: discord.Guild) -> dict[str, Any]:
    """Capture the restoreable configuration of ``guild``.

    Roles and channels are recorded by *name and relative position* only, which
    is what makes the snapshot portable across a bot that rejoins, a rebuild, or
    a migration to a different server.
    """
    default_role = guild.default_role
    roles = [
        {
            "name": role.name,
            "position": role.position,
            "colour": role.colour.value,
            "hoist": role.hoist,
            "mentionable": role.mentionable,
            "permissions": role.permissions.value,
            "managed": role.managed,
            "is_default": role.id == default_role.id,
        }
        for role in sorted(guild.roles, key=lambda r: r.position)
    ]

    channels = [
        {
            "name": channel.name,
            "type": channel.type.value,
            "category": channel.category.name if channel.category else None,
            "position": channel.position,
            "nsfw": getattr(channel, "nsfw", False),
            "overwrites": _serialize_overwrites(channel),
        }
        for channel in sorted(guild.channels, key=lambda c: (c.position, c.name))
        if channel.type not in _SKIP_CHANNEL_TYPES
    ]

    return {
        "format": FORMAT,
        "guild": {
            "id": guild.id,
            "name": guild.name,
            "description": guild.description,
        },
        "roles": roles,
        "channels": channels,
        "counts": {
            "roles": len(roles),
            "channels": len(channels),
            "members": guild.member_count or 0,
        },
    }


def _serialize_overwrites(channel: discord.abc.GuildChannel) -> list[dict[str, Any]]:
    """Record per-target permission pairs as plain ints.

    :meth:`PermissionOverwrite.pair` yields the ``(allow, deny)`` pair in a
    stable order, which is what makes these values comparable across processes
    and readable without importing the library.
    """
    rows: list[dict[str, Any]] = []
    for target, overwrite in channel.overwrites.items():
        allow, deny = overwrite.pair()
        rows.append(
            {
                "type": type(target).__name__,
                "id": target.id,
                "allow": allow.value,
                "deny": deny.value,
            }
        )
    return rows


def summarize_backup(payload: dict[str, Any]) -> dict[str, Any]:
    """Flatten a payload into the few numbers a reply can show."""
    guild = payload.get("guild") or {}
    counts = payload.get("counts") or {}
    return {
        "guild_name": guild.get("name") or "unknown",
        "guild_id": guild.get("id"),
        "roles": int(counts.get("roles", len(payload.get("roles") or []))),
        "channels": int(counts.get("channels", len(payload.get("channels") or []))),
        "members": int(counts.get("members", 0)),
    }


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def sanitize_name(name: str | None, *, fallback: str = "backup") -> str:
    """Reduce an operator-supplied label to a filesystem-safe token."""
    cleaned = SAFE_NAME.sub("_", (name or "").strip())[:MAX_BACKUP_NAME].strip("._-")
    return cleaned or fallback


def backup_path(directory: Path, guild_id: int, name: str) -> Path:
    """Where the export for ``(guild_id, name)`` lives on disk."""
    return Path(directory) / f"{guild_id}_{sanitize_name(name)}{BACKUP_SUFFIX}"


def list_backup_names(directory: Path, guild_id: int) -> list[str]:
    """Every backup label stored for ``guild_id``, alphabetically.

    Ordered by label rather than mtime on purpose: callers that need recency read
    ``guild_backups.created_at``, which the index records, whereas an mtime sort
    here would churn on file copies and restores.
    """
    prefix = f"{guild_id}_"
    if not Path(directory).is_dir():
        return []
    return sorted(
        path.name[len(prefix) : -len(BACKUP_SUFFIX)]
        for path in Path(directory).glob(f"{prefix}*{BACKUP_SUFFIX}")
    )


# --------------------------------------------------------------------------- #
# Restore
# --------------------------------------------------------------------------- #
async def restore_guild(
    guild: discord.Guild, payload: dict[str, Any], *, reason: str = "restored from backup"
) -> dict[str, int]:
    """Apply the conservative half of a backup to a live ``guild``.

    Restores: guild name/description, and role name/colour/hoist/mentionable
    plus the positions of roles that already exist. Deliberately **not**
    restored: permissions, channel creation, deletion, and channel overwrites.
    A restore must never widen anyone's access; a snapshot is a name list, not
    an authority.

    Returns:
        Counts of what actually changed, for the command reply.
    """
    changed = {"guild": 0, "roles": 0, "channels": 0, "skipped": 0}
    snapshot = payload.get("guild") or {}

    desired_name = snapshot.get("name")
    if desired_name and guild.name != desired_name:
        try:
            await guild.edit(
                name=truncate(desired_name, MAX_BACKUP_NAME), reason=reason
            )
            changed["guild"] = 1
        except discord.HTTPException as exc:
            logger.warning("Restore guild edit failed in %s: %s", guild.id, exc)
            changed["skipped"] += 1

    existing = {role.name: role for role in guild.roles}
    edits: list[tuple[discord.Role, dict[str, Any]]] = []
    for entry in payload.get("roles") or []:
        role = existing.get(entry.get("name") or "")
        if role is None or role.managed or entry.get("is_default"):
            changed["skipped"] += 1
            continue
        attrs: dict[str, Any] = {}
        if role.colour.value != entry.get("colour"):
            attrs["colour"] = discord.Colour(entry.get("colour") or 0)
        if role.hoist != bool(entry.get("hoist")):
            attrs["hoist"] = bool(entry.get("hoist"))
        if role.mentionable != bool(entry.get("mentionable")):
            attrs["mentionable"] = bool(entry.get("mentionable"))
        if attrs:
            edits.append((role, attrs))

    # Batched: one HTTP request instead of one per role, which is the difference
    # between a restore that works and one that trips the guild-edit rate limit.
    if edits:
        try:
            await guild.edit(roles=edits, reason=reason)
            changed["roles"] = len(edits)
        except discord.Forbidden:
            logger.warning("Restore denied in guild %s: missing Manage Roles", guild.id)
            changed["skipped"] += len(edits)
            edits = []
        except discord.HTTPException as exc:
            logger.warning("Restore role edit failed in guild %s: %s", guild.id, exc)
            changed["skipped"] += len(edits)
            edits = []

    # Channels are matched, never created or deleted.
    live = {channel.name for channel in guild.channels}
    changed["channels"] = sum(
        1 for entry in payload.get("channels") or [] if entry.get("name") in live
    )
    return changed
