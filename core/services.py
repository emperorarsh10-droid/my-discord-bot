"""Domain services shared by the cogs.

Command handlers should read like a list of decisions, not a list of API calls.
Anything that more than one command needs — resolving a guild's log channel,
writing the moderation ledger, DMing a member, building an audit-log reason
string — lives here, once.

Every function accepts an explicit :class:`~core.database.Database` so tests can
substitute a fixture; ``get_database()`` is only the default.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import discord
from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError

from config import get_settings
from core.automod import MAX_KEYWORD_LEN, normalize_phrase
from core.dashboard_state import runtime_state
from core.database import Database, get_database
from core.embeds import (
    COLOR_ERROR,
    add_action_fields,
    timestamp,
    truncate,
)
from core.errors import (
    DatabaseUnavailableError,
    HierarchyError,
    MissingTargetError,
    PermissionDeniedError,
)
from core.logging_setup import command_context, get_logger
from core.models import (
    CaseAction,
    ChannelSnapshot,
    GuildConfig,
    GuildFilter,
    GuildSetting,
    ModCase,
    utcnow,
)

__all__ = [
    "AUDIT_REASON_LIMIT",
    "MEDIATOR_ROLE_CANDIDATES",
    "add_filter",
    "build_audit_reason",
    "ensure_mute_role",
    "expire_stale_bans",
    "expire_stale_mutes",
    "fetch_active_cases",
    "get_or_create_guild_config",
    "has_moderation_access",
    "list_filters",
    "log_action",
    "notify_member",
    "permissions_from_names",
    "record_case",
    "remove_filter",
    "resolve_log_channel",
    "resolve_target_member",
    "utc_expiry",
]

logger = get_logger("zagrosian.services")

#: Discord truncates audit-log reasons at 512 characters.
AUDIT_REASON_LIMIT: Final[int] = 512

#: Fallback role names searched (case-insensitive) when a guild has not
#: configured its own moderator role.
MEDIATOR_ROLE_CANDIDATES: Final[tuple[str, ...]] = (
    "moderator",
    "mod",
    "mods",
    "staff",
    "helper",
)


# --------------------------------------------------------------------------- #
# Guild configuration
# --------------------------------------------------------------------------- #
async def get_or_create_guild_config(
    guild_id: int, database: Database | None = None
) -> GuildConfig | None:
    """Fetch a guild's config, creating the row on first use.

    Returns ``None`` when the database is unreachable — a missing config must
    never stop a command from working, it just means "no log channel set".
    """
    db = database or get_database()
    try:
        async with db.session() as session:
            config = await session.get(GuildConfig, guild_id)
            if config is None:
                config = GuildConfig(guild_id=guild_id)
                session.add(config)
                await session.flush()
                logger.info("Created configuration for guild %s", guild_id)
            return config
    except (SQLAlchemyError, RuntimeError) as exc:
        logger.warning("Guild config lookup failed for %s: %s", guild_id, exc)
        return None


# --------------------------------------------------------------------------- #
# Case ledger
# --------------------------------------------------------------------------- #
@command_context("service:record_case")
async def record_case(
    *,
    guild_id: int,
    user_id: int,
    moderator_id: int,
    action: CaseAction,
    reason: str | None,
    expires_at: datetime | None = None,
    database: Database | None = None,
) -> ModCase:
    """Append a row to the moderation ledger and return it.

    Raises:
        DatabaseUnavailableError: if the case could not be persisted. Callers
            treat the *action* as successful but must tell the moderator the
            record was not saved.
    """
    settings = get_settings()
    db = database or get_database()

    try:
        number = await db.next_case_number(guild_id)
    except (SQLAlchemyError, RuntimeError) as exc:
        logger.error("Case allocation failed for guild %s: %s", guild_id, exc)
        raise DatabaseUnavailableError(
            "The moderation ledger is unreachable, so this action was not recorded. "
            "The action itself still went through."
        ) from exc

    case_ref = f"{settings.case_prefix}-{number:06d}"

    try:
        async with db.session() as session:
            case = ModCase(
                guild_id=guild_id,
                case_number=number,
                case_ref=case_ref,
                user_id=user_id,
                moderator_id=moderator_id,
                action=action.value,
                reason=truncate(reason, 2000) if reason else None,
                expires_at=expires_at,
                is_active=True,
            )
            session.add(case)
            await session.flush()

            config = await session.get(GuildConfig, guild_id)
            if config is None:
                config = GuildConfig(guild_id=guild_id)
                session.add(config)
            config.action_count += 1
    except (SQLAlchemyError, RuntimeError) as exc:
        logger.error("Case persistence failed (%s): %s", case_ref, exc)
        raise DatabaseUnavailableError(
            f"The moderation ledger rejected case {case_ref}. The action itself "
            "still went through."
        ) from exc

    logger.info(
        "Case %s | action=%s guild=%s user=%s moderator=%s",
        case_ref,
        action.value,
        guild_id,
        user_id,
        moderator_id,
    )
    runtime_state.record_moderation_action()
    return case


@command_context("service:fetch_active_cases")
async def fetch_active_cases(
    guild_id: int,
    *,
    user_id: int | None = None,
    action: CaseAction | None = None,
    limit: int = 25,
    database: Database | None = None,
) -> list[ModCase]:
    """Newest-first active cases, optionally filtered by user and/or action."""
    db = database or get_database()
    try:
        async with db.session() as session:
            statement = (
                select(ModCase)
                .where(ModCase.guild_id == guild_id, ModCase.is_active.is_(True))
                .order_by(ModCase.case_number.desc())
                .limit(limit)
            )
            if user_id is not None:
                statement = statement.where(ModCase.user_id == user_id)
            if action is not None:
                statement = statement.where(ModCase.action == action.value)
            result = await session.execute(statement)
            return list(result.scalars().all())
    except (SQLAlchemyError, RuntimeError) as exc:
        logger.warning("Case history lookup failed for guild %s: %s", guild_id, exc)
        return []


# --------------------------------------------------------------------------- #
# Ledger queries shared by the infraction-history commands
# --------------------------------------------------------------------------- #
@command_context("service:fetch_case_history")
async def fetch_case_history(
    guild_id: int,
    *,
    user_id: int | None = None,
    action: str | None = None,
    include_revoked: bool = True,
    limit: int = 25,
    offset: int = 0,
    database: Database | None = None,
) -> list[ModCase]:
    """Newest-first cases, optionally filtered by user and/or action.

    Unlike :func:`fetch_active_cases` this can include revoked rows and skips
    over them with ``offset``, which is what the paginated ``/cases`` browser
    needs. Returns ``[]`` on any database failure rather than raising: a history
    view must still render when the ledger is down.
    """
    db = database or get_database()
    try:
        async with db.session() as session:
            statement = select(ModCase).where(ModCase.guild_id == guild_id)
            if user_id is not None:
                statement = statement.where(ModCase.user_id == user_id)
            if action:
                statement = statement.where(ModCase.action == action)
            if not include_revoked:
                statement = statement.where(ModCase.is_active.is_(True))
            statement = (
                statement.order_by(ModCase.case_number.desc())
                .offset(max(0, offset))
                .limit(max(1, min(limit, 100)))
            )
            result = await session.execute(statement)
            return list(result.scalars().all())
    except (SQLAlchemyError, RuntimeError) as exc:
        logger.warning("Case history lookup failed for guild %s: %s", guild_id, exc)
        return []


@command_context("service:count_case_history")
async def count_case_history(
    guild_id: int,
    *,
    user_id: int | None = None,
    action: str | None = None,
    include_revoked: bool = True,
    database: Database | None = None,
) -> int:
    """Total matching rows, for computing page counts."""
    db = database or get_database()
    try:
        async with db.session() as session:
            statement = select(func.count()).select_from(ModCase).where(
                ModCase.guild_id == guild_id
            )
            if user_id is not None:
                statement = statement.where(ModCase.user_id == user_id)
            if action:
                statement = statement.where(ModCase.action == action)
            if not include_revoked:
                statement = statement.where(ModCase.is_active.is_(True))
            return int((await session.execute(statement)).scalar_one())
    except (SQLAlchemyError, RuntimeError) as exc:
        logger.warning("Case history count failed for guild %s: %s", guild_id, exc)
        return 0


@command_context("service:find_case")
async def find_case(
    guild_id: int, needle: str, database: Database | None = None
) -> ModCase | None:
    """Resolve ``needle`` to a case.

    Accepts a case reference (``ZEYE-000042``), a bare case number, or the
    Discord user id — three spellings because moderators paste all three. Snowflake
    length disambiguates user ids from case numbers.
    """
    db = database or get_database()
    cleaned = (needle or "").strip().lstrip("#")
    if not cleaned:
        return None

    # Discord snowflakes are 17-20 digits; a case number is never that long, so
    # digit length alone disambiguates "user id" from "case number".
    is_snowflake = cleaned.isdigit() and len(cleaned) >= 15

    try:
        async with db.session() as session:
            if is_snowflake:
                criteria = ModCase.user_id == int(cleaned)
            elif cleaned.isdigit():
                criteria = ModCase.case_number == int(cleaned)
            else:
                criteria = ModCase.case_ref == cleaned

            result = await session.execute(
                select(ModCase)
                .where(ModCase.guild_id == guild_id, criteria)
                .order_by(ModCase.case_number.desc())
            )
            rows = list(result.scalars().all())
            return rows[0] if rows else None
    except (SQLAlchemyError, RuntimeError) as exc:
        logger.warning("Case lookup failed for %r in guild %s: %s", needle, guild_id, exc)
        return None


@command_context("service:revoke_cases")
async def revoke_cases(
    guild_id: int,
    *,
    user_id: int | None = None,
    case_ref: str | None = None,
    actions: tuple[CaseAction, ...] | None = None,
    moderator_id: int,
    reason: str,
    database: Database | None = None,
) -> int:
    """Mark matching active cases revoked. Returns the number changed.

    Revocation never deletes a row: the ledger is the audit trail, and a
    ``/clearwarns`` that erased the warning would erase the evidence that a
    moderator issued it.

    ``actions`` narrows the sweep to one kind of sanction. Lifting a mute must
    not silently clear an active timeout or ban for the same member — the member
    would read their mod history as clean while still being restricted.
    """
    db = database or get_database()
    try:
        async with db.session() as session:
            statement = select(ModCase).where(
                ModCase.guild_id == guild_id, ModCase.is_active.is_(True)
            )
            if case_ref is not None:
                statement = statement.where(ModCase.case_ref == case_ref)
            if user_id is not None:
                statement = statement.where(ModCase.user_id == user_id)
            if actions is not None:
                statement = statement.where(
                    ModCase.action.in_([action.value for action in actions])
                )
            rows = list((await session.execute(statement)).scalars().all())
            for row in rows:
                row.is_active = False
                row.revoked_by = moderator_id
                row.revoked_reason = truncate(reason, 2000) if reason else None
            return len(rows)
    except (SQLAlchemyError, RuntimeError) as exc:
        logger.warning("Case revocation failed for guild %s: %s", guild_id, exc)
        return 0


@command_context("service:count_active_warnings")
async def count_active_warnings(
    guild_id: int, user_id: int, database: Database | None = None
) -> int:
    """How many *active* warnings a member currently holds — the warn-ladder rung."""
    db = database or get_database()
    try:
        async with db.session() as session:
            statement = select(func.count()).select_from(ModCase).where(
                ModCase.guild_id == guild_id,
                ModCase.user_id == user_id,
                ModCase.action == CaseAction.WARN.value,
                ModCase.is_active.is_(True),
            )
            return int((await session.execute(statement)).scalar_one())
    except (SQLAlchemyError, RuntimeError) as exc:
        logger.warning("Warning count failed for %s/%s: %s", guild_id, user_id, exc)
        return 0


# --------------------------------------------------------------------------- #
# Channel permission snapshots
# --------------------------------------------------------------------------- #
#: Maps ``discord.abc.PermissionOverwrite.target_type`` to a short stable token.
#: The enum's *values* are an implementation detail of discord.py and are not
#: safe to persist, so the mapping is explicit and travels with the data.
_OVERWRITE_TARGETS: Final[dict[int, str]] = {
    0: "role",
    1: "member",
}


@command_context("service:overwrites_to_json")
def overwrites_to_json(channel: discord.abc.GuildChannel) -> str:
    """Serialise a channel's permission overwrites losslessly.

    Each overwrite becomes ``[kind, target_id, allow, deny]`` where ``allow`` and
    ``deny`` are the raw bitfields. Persisting the *mask* rather than the
    individual booleans is what makes a restore exact: ``Permissions`` carries
    several dozen independent flags and naming them one by one would silently
    drop every flag nobody remembered.
    """
    payload = [
        [
            _OVERWRITE_TARGETS.get(int(item.type), "role"),
            int(item.id),
            int(item.allow),
            int(item.deny),
        ]
        for item in channel.overwrites
    ]
    return json.dumps(payload)


@command_context("service:json_to_overwrites")
def json_to_overwrites(payload: str) -> list[tuple[str, int, int, int]]:
    """Inverse of :func:`overwrites_to_json`. Malformed rows are skipped, not fatal."""
    try:
        raw = json.loads(payload or "[]")
    except (TypeError, ValueError):
        logger.warning("Discarding unreadable snapshot payload")
        return []

    out: list[tuple[str, int, int, int]] = []
    for row in raw:
        try:
            kind, target_id, allow, deny = row
            if kind not in ("role", "member"):
                continue
            out.append((str(kind), int(target_id), int(allow), int(deny)))
        except (TypeError, ValueError):
            continue
    return out


@command_context("service:save_channel_snapshot")
async def save_channel_snapshot(
    guild_id: int,
    channel: discord.abc.GuildChannel,
    label: str,
    *,
    created_by: int | None = None,
    database: Database | None = None,
) -> ChannelSnapshot | None:
    """Record a channel's current permissions under ``label``.

    Re-using a label replaces the previous snapshot, which is the behaviour
    ``/panic`` wants: activating panic twice should not leave two stale copies
    and make ``/unpanic`` ambiguous about which one is authoritative.
    """
    db = database or get_database()
    meta = {
        "name": getattr(channel, "name", ""),
        "category_id": getattr(getattr(channel, "category", None), "id", None),
        "position": getattr(channel, "position", None),
        "topic": getattr(channel, "topic", None),
        "nsfw": getattr(channel, "nsfw", None),
    }
    try:
        async with db.session() as session:
            existing = (
                await session.execute(
                    select(ChannelSnapshot).where(
                        ChannelSnapshot.guild_id == guild_id,
                        ChannelSnapshot.channel_id == channel.id,
                        ChannelSnapshot.label == label,
                    )
                )
            ).scalar_one_or_none()

            if existing is not None:
                existing.overwrites = overwrites_to_json(channel)
                existing.channel_meta = json.dumps(meta)
                existing.created_by = created_by
                existing.created_at = utcnow()
                row = existing
            else:
                row = ChannelSnapshot(
                    guild_id=guild_id,
                    channel_id=channel.id,
                    label=label,
                    overwrites=overwrites_to_json(channel),
                    channel_meta=json.dumps(meta),
                    created_by=created_by,
                )
                session.add(row)
            await session.commit()
            return row
    except (SQLAlchemyError, RuntimeError) as exc:
        logger.error("Snapshot save failed for channel %s: %s", channel.id, exc)
        return None


@command_context("service:restore_channel_snapshots")
async def restore_channel_snapshots(
    guild: discord.Guild,
    label: str,
    *,
    database: Database | None = None,
) -> tuple[int, int]:
    """Re-apply every snapshot under ``label``. Returns ``(restored, missing)``.

    ``missing`` counts channels that no longer exist — deleted while locked
    down, which is exactly what ``/nuke`` does. Reporting the split matters:
    "restored 8, 3 gone" is a complete answer, "restored 8" alone looks like the
    other three were restored successfully.
    """
    db = database or get_database()
    try:
        async with db.session() as session:
            rows = list(
                (
                    await session.execute(
                        select(ChannelSnapshot).where(
                            ChannelSnapshot.guild_id == guild.id,
                            ChannelSnapshot.label == label,
                        )
                    )
                ).scalars()
            )
    except (SQLAlchemyError, RuntimeError) as exc:
        logger.error("Snapshot read failed for guild %s: %s", guild.id, exc)
        return (0, 0)

    restored = missing = 0
    for row in rows:
        channel = guild.get_channel(row.channel_id)
        if channel is None:
            missing += 1
            continue
        if not guild.me.guild_permissions.administrator and not (
            guild.me.permissions_in(channel).manage_channels
        ):
            missing += 1
            continue
        try:
            overwrites: dict[Any, discord.PermissionOverwrite] = {}
            for kind, target_id, allow, deny in json_to_overwrites(row.overwrites):
                target: Any
                if kind == "role":
                    target = guild.get_role(target_id)
                    if target is None:
                        continue
                else:
                    target = guild.get_member(target_id)
                    if target is None:
                        continue
                overwrites[target] = discord.PermissionOverwrite(
                    allow=discord.Permissions(allow), deny=discord.Permissions(deny)
                )
            await channel.edit(overwrites=overwrites, reason="Restoring saved permissions")
            restored += 1
        except discord.HTTPException as exc:
            logger.warning("Restore failed for channel %s: %s", row.channel_id, exc)
            missing += 1

    return (restored, missing)


@command_context("service:drop_snapshots")
async def drop_snapshots(
    guild_id: int, label: str, database: Database | None = None
) -> int:
    """Delete snapshots under ``label`` once they are no longer needed."""
    db = database or get_database()
    try:
        async with db.session() as session:
            rows = list(
                (
                    await session.execute(
                        select(ChannelSnapshot).where(
                            ChannelSnapshot.guild_id == guild_id,
                            ChannelSnapshot.label == label,
                        )
                    )
                ).scalars()
            )
            for row in rows:
                await session.delete(row)
            return len(rows)
    except (SQLAlchemyError, RuntimeError) as exc:
        logger.warning("Snapshot cleanup failed for guild %s: %s", guild_id, exc)
        return 0


# --------------------------------------------------------------------------- #
# Arbitrary per-guild settings
# --------------------------------------------------------------------------- #
#: Values longer than this are rejected rather than truncated: a setting that does
#: not fit is a bug in the caller, and silently truncating it produces a rule that
#: behaves differently from what the moderator typed.
MAX_SETTING_VALUE: Final[int] = 2000


@command_context("service:set_setting")
async def set_guild_config_flag(
    guild_id: int,
    column: str,
    value: bool,
    *,
    database: Database | None = None,
) -> bool:
    """Set one boolean column on ``GuildConfig``, creating the row if needed.

    Written as a targeted ``UPDATE`` rather than a read-modify-write: two
    moderators panicking and unpanicking at once must not lose one of the
    writes, and a detached ORM instance here is how the flag silently stops
    persisting. Returns whether the write landed.
    """
    target = getattr(GuildConfig, column, None)
    if target is None or not hasattr(target, "property"):
        raise ValueError(f"{column!r} is not a boolean GuildConfig column")

    db = database or get_database()
    try:
        async with db.session() as session:
            updated = await session.execute(
                update(GuildConfig)
                .where(GuildConfig.guild_id == guild_id)
                .values(**{column: bool(value)})
            )
            if not updated.rowcount:
                session.add(GuildConfig(guild_id=guild_id, **{column: bool(value)}))
            await session.commit()
        return True
    except (SQLAlchemyError, RuntimeError) as exc:
        logger.warning(
            "Could not set %s=%s for guild %s: %s", column, value, guild_id, exc
        )
        return False


async def set_setting(
    guild_id: int,
    key: str,
    value: str,
    *,
    created_by: int | None = None,
    database: Database | None = None,
) -> bool:
    """Upsert one setting. Returns ``False`` if the value was too long."""
    if len(value) > MAX_SETTING_VALUE:
        logger.warning("Refusing setting %r for guild %s: %d chars", key, guild_id, len(value))
        return False

    db = database or get_database()
    try:
        async with db.session() as session:
            row = (
                await session.execute(
                    select(GuildSetting).where(
                        GuildSetting.guild_id == guild_id, GuildSetting.key == key
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                session.add(
                    GuildSetting(
                        guild_id=guild_id, key=key, value=value, created_by=created_by
                    )
                )
            else:
                row.value = value
                row.created_by = created_by
            await session.commit()
        return True
    except (SQLAlchemyError, RuntimeError) as exc:
        logger.warning("Setting write failed for %r in guild %s: %s", key, guild_id, exc)
        return False


@command_context("service:get_setting")
async def get_setting(
    guild_id: int,
    key: str,
    default: str | None = None,
    *,
    database: Database | None = None,
) -> str | None:
    """Read one setting, or ``default`` when it is absent or unreadable."""
    db = database or get_database()
    try:
        async with db.session() as session:
            row = (
                await session.execute(
                    select(GuildSetting).where(
                        GuildSetting.guild_id == guild_id, GuildSetting.key == key
                    )
                )
            ).scalar_one_or_none()
            return row.value if row is not None else default
    except (SQLAlchemyError, RuntimeError) as exc:
        logger.warning("Setting read failed for %r in guild %s: %s", key, guild_id, exc)
        return default


@command_context("service:get_bool_setting")
async def get_bool_setting(
    guild_id: int, key: str, default: bool = False, *, database: Database | None = None
) -> bool:
    """Read a setting as a boolean. Anything unparseable falls back to ``default``."""
    raw = await get_setting(guild_id, key, None, database=database)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@command_context("service:get_settings_prefix")
async def get_settings_prefix(
    guild_id: int, prefix: str, *, database: Database | None = None
) -> dict[str, str]:
    """Every setting whose key starts with ``prefix``, with the prefix stripped."""
    db = database or get_database()
    try:
        async with db.session() as session:
            rows = list(
                (
                    await session.execute(
                        select(GuildSetting).where(
                            GuildSetting.guild_id == guild_id,
                            GuildSetting.key.like(f"{prefix}%"),
                        )
                    )
                ).scalars()
            )
            return {row.key[len(prefix):]: row.value for row in rows}
    except (SQLAlchemyError, RuntimeError) as exc:
        logger.warning("Setting prefix read failed for %s in guild %s: %s", prefix, guild_id, exc)
        return {}


# --------------------------------------------------------------------------- #
# Moderation log channel
# --------------------------------------------------------------------------- #
@command_context("service:resolve_log_channel")
async def resolve_log_channel(
    guild: discord.Guild, database: Database | None = None
) -> discord.abc.GuildChannel | None:
    """Find the channel moderation actions should be written to.

    Resolution order:
      1. the ``log_channel_id`` stored in the guild's configuration,
      2. a channel literally named ``mod-logs`` / ``moderation-logs``,
      3. the guild's system channel.

    Returns ``None`` when nothing suitable is visible to the bot.
    """
    config = await get_or_create_guild_config(guild.id, database)

    if config and config.log_channel_id:
        channel = guild.get_channel(config.log_channel_id)
        if isinstance(channel, discord.abc.GuildChannel):
            return channel
        logger.warning(
            "Guild %s config points at %s which is gone; reconfigure with /setup logs",
            guild.id,
            config.log_channel_id,
        )

    me = guild.me
    for name in ("mod-logs", "moderation-logs", "modlogs"):
        for channel in guild.text_channels:
            if channel.name.lower() == name and me.permissions_in(channel).view_channel:
                return channel

    if guild.system_channel and me.permissions_in(guild.system_channel).view_channel:
        return guild.system_channel
    return None


@command_context("service:log_action")
async def log_action(
    guild: discord.Guild,
    embed: discord.Embed,
    *,
    database: Database | None = None,
    channel: discord.abc.GuildChannel | None = None,
) -> bool:
    """Deliver a moderation embed to the log channel. Never raises.

    Returns:
        ``True`` if the embed was delivered.
    """
    target = channel or await resolve_log_channel(guild, database)
    if target is None:
        logger.warning("No moderation log channel available in guild %s", guild.id)
        return False

    me = guild.me
    perms = me.permissions_in(target)
    if not perms.send_messages:
        logger.warning(
            "Bot lacks Send Messages in log channel %s of guild %s",
            target.id,
            guild.id,
        )
        return False
    if not perms.embed_links:
        logger.warning(
            "Bot lacks Embed Links in log channel %s of guild %s", target.id, guild.id
        )
        return False

    try:
        await target.send(embed=embed)
    except discord.Forbidden as exc:
        logger.warning("Forbidden writing to log channel %s: %s", target.id, exc)
    except discord.HTTPException as exc:
        logger.error("HTTP failure writing to log channel %s: %s", target.id, exc)
    except Exception:
        logger.exception("Unexpected failure writing to log channel %s", target.id)
    else:
        return True
    return False


# --------------------------------------------------------------------------- #
# Member notification
# --------------------------------------------------------------------------- #
async def notify_member(
    member: discord.Member,
    embed: discord.Embed,
    *,
    database: Database | None = None,
    default: bool = True,
) -> bool:
    """DM a member about an action taken against them. Never raises.

    Returns:
        ``True`` if the DM was delivered.
    """
    settings = get_settings()
    # The global switch is the master; a per-guild override can only narrow it,
    # never re-enable something an operator turned off everywhere.
    enabled = settings.dm_members_on_punish and default
    if enabled:
        config = await get_or_create_guild_config(member.guild.id, database)
        if config is not None and config.dm_on_punish is not None:
            enabled = config.dm_on_punish

    if not enabled:
        return False

    try:
        await member.send(embed=embed)
    except discord.Forbidden:
        logger.info(
            "Member %s has DMs closed; skipping notification", member.id
        )
    except discord.HTTPException as exc:
        logger.warning("Failed to DM member %s: %s", member.id, exc)
    except Exception:
        logger.exception("Unexpected failure DMing member %s", member.id)
    else:
        return True
    return False


# --------------------------------------------------------------------------- #
# Audit-log reasons
# --------------------------------------------------------------------------- #
def build_audit_reason(
    case_ref: str | None,
    moderator: discord.abc.User,
    reason: str | None,
) -> str:
    """Compose the string that lands in the server's audit log.

    Shape: ``ZEYE-000142 · moderator#1234 · reason``. Discord shows this in the
    audit log, so it has to be self-explanatory without the bot.
    """
    actor = getattr(moderator, "display_name", None) or str(moderator)
    parts = [p for p in (case_ref, actor, reason) if p]
    return truncate(" · ".join(parts), AUDIT_REASON_LIMIT)


# --------------------------------------------------------------------------- #
# Authorisation
# --------------------------------------------------------------------------- #
@command_context("service:has_moderation_access")
def permissions_from_names(
    required: Iterable[str] | discord.Permissions,
) -> discord.Permissions:
    """Build a :class:`discord.Permissions` from permission names or pass one through.

    ``discord.Permissions`` only accepts a bitmask or keyword flags — a plain
    iterable of names (e.g. ``("manage_roles",)``) must be expanded into
    keywords rather than passed positionally, which would raise ``TypeError``.
    """
    if isinstance(required, discord.Permissions):
        return required
    return discord.Permissions(**dict.fromkeys(required, True))


async def has_moderation_access(
    interaction: discord.Interaction, required: Iterable[str] | None = None
) -> bool:
    """Whether the invoking user may run moderation commands in this guild.

    Grants access to the guild owner, to the bot's configured owners, to
    holders of a role named like a moderator, and to anyone holding every
    permission in ``required`` (defaults to the union of Discord's
    moderation-relevant permissions).
    """
    settings = get_settings()
    if interaction.user.id in settings.owner_ids:
        return True

    if not isinstance(interaction, discord.Interaction):
        raise PermissionDeniedError("This command can only be used inside a server.")
    if interaction.user.id == interaction.guild.owner_id:
        return True

    member = interaction.user
    if not isinstance(member, discord.Member):
        return False

    if required is None:
        return (
            member.guild_permissions.moderate_members
            or member.guild_permissions.kick_members
            or member.guild_permissions.ban_members
            or member.guild_permissions.manage_messages
        )

    permissions = member.guild_permissions
    needed = permissions_from_names(required)
    if needed.is_subset(permissions):
        return True

    lowered = {role.name.lower() for role in member.roles}
    return any(candidate in lowered for candidate in MEDIATOR_ROLE_CANDIDATES)


# --------------------------------------------------------------------------- #
# Mute role
# --------------------------------------------------------------------------- #
async def ensure_mute_role(
    guild: discord.Guild, database: Database | None = None
) -> discord.Role:
    """Return the guild's mute role, creating it when it does not exist.

    The role is positioned just below the bot's own highest role so the bot is
    always able to add and remove it.

    Raises:
        PermissionDeniedError: the bot cannot manage roles.
        HierarchyError: no role sits above the mute role, so it would be
            silently ineffective.
    """
    settings = get_settings()
    me = guild.me

    if not me.guild_permissions.manage_roles:
        raise PermissionDeniedError(
            "I need the **Manage Roles** permission to create or assign mute roles."
        )

    role: discord.Role | None = None
    config = await get_or_create_guild_config(guild.id, database)
    if config and config.muted_role_id:
        role = guild.get_role(config.muted_role_id)
        if role is None:
            logger.info(
                "Guild %s mute role %s no longer exists; recreating",
                guild.id,
                config.muted_role_id,
            )

    if role is None:
        with contextlib.suppress(discord.NotFound):
            role = discord.utils.get(guild.roles, name=settings.default_mute_role)

    created = False
    if role is None:
        try:
            role = await guild.create_role(
                name=settings.default_mute_role,
                colour=discord.Colour(0x4B4B4B),
                reason=build_audit_reason(None, me, "Mute role provisioned"),
            )
            created = True
        except discord.Forbidden as exc:
            raise PermissionDeniedError(
                "I could not create the mute role. Check that my role sits above "
                "the member roles and that I have **Manage Roles**."
            ) from exc
        except discord.HTTPException as exc:
            raise PermissionDeniedError(
                f"Discord rejected the mute role creation: {exc.status} {exc.text}"
            ) from exc

    # Position: highest managed role - 1, never above the bot itself.
    anchor = max(
        (r for r in guild.roles if r.managed and r < me.top_role),
        key=lambda r: r.position,
        default=None,
    )
    target_position = (anchor.position - 1) if anchor else (me.top_role.position - 1)
    target_position = max(1, target_position)

    if role.position != target_position or created:
        with contextlib.suppress(discord.Forbidden, discord.HTTPException):
            await guild.edit_role_positions(
                {role.id: target_position},
                reason="Mute role positioning",
            )

    if me.top_role.position <= role.position:
        raise HierarchyError(
            f"My highest role sits at or below the mute role **{role.name}**. "
            "Move my role above it, otherwise the mute will do nothing."
        )

    if config is None or config.muted_role_id != role.id:
        try:
            async with (database or get_database()).session() as session:
                stored = await session.get(GuildConfig, guild.id)
                if stored is None:
                    stored = GuildConfig(guild_id=guild.id)
                    session.add(stored)
                stored.muted_role_id = role.id
        except (SQLAlchemyError, RuntimeError) as exc:
            logger.warning(
                "Could not persist mute role for guild %s: %s", guild.id, exc
            )

    return role


# --------------------------------------------------------------------------- #
# Shared embed builders
# --------------------------------------------------------------------------- #
def punishment_embed(
    *,
    action: str,
    guild: discord.Guild,
    member: discord.abc.User,
    moderator: discord.abc.User,
    reason: str | None,
    case_ref: str | None = None,
    duration: timedelta | None = None,
    dm_message: str | None = None,
) -> discord.Embed:
    """Build the log-channel embed for a moderation action."""
    from core.embeds import base_embed, describe_member

    embed = base_embed(
        title=f"{action} · {member}",
        colour=COLOR_ERROR if action in {"Ban", "Kick"} else 0xB26A00,
        author=moderator,
    )
    add_action_fields(
        embed,
        action=action,
        target=(
            describe_member(member)
            if isinstance(member, discord.Member)
            else f"**{member}** (`{member.id}`)"
        ),
        moderator=f"**{moderator}** (`{moderator.id}`)",
        reason=reason,
        case_ref=case_ref,
        duration=duration,
        extra=[("Server", guild.name, True)],
    )
    if dm_message:
        embed.description = dm_message
    return embed


def dm_notice_embed(
    *,
    action: str,
    guild: discord.Guild,
    reason: str | None,
    case_ref: str | None = None,
    duration: timedelta | None = None,
    moderator: discord.abc.User,
) -> discord.Embed:
    """Build the private notice sent to a punished member."""
    from core.embeds import COLOR_ERROR, base_embed, format_duration

    embed = base_embed(
        title=f"You were {action.lower()} in **{guild.name}**",
        colour=COLOR_ERROR,
        author=moderator,
    )
    embed.description = (
        f"A moderator in **{guild.name}** has taken action against your account."
    )
    add_action_fields(
        embed,
        action=action,
        target=f"**{guild.name}**",
        moderator=f"**{getattr(moderator, 'display_name', moderator)}**",
        reason=reason,
        case_ref=case_ref,
        duration=duration,
    )
    if duration is not None:
        embed.set_footer(
            text=f"This expires at {timestamp(utcnow() + duration)} · {format_duration(duration)}"
        )
    return embed


async def expire_stale_mutes(guild: discord.Guild, database: Database | None = None) -> int:
    """Remove the mute role from members whose mute window has elapsed.

    Returns the number of members released. Called on cog load and on interval
    by ``cogs.moderation``; also invoked opportunistically by the mute command
    so a restart does not leave stale roles in place.
    """
    config = await get_or_create_guild_config(guild.id, database)
    if config is None or config.muted_role_id is None:
        return 0
    role = guild.get_role(config.muted_role_id)
    if role is None or role.members == 0:
        return 0

    try:
        async with (database or get_database()).session() as session:
            statement = select(ModCase).where(
                ModCase.guild_id == guild.id,
                ModCase.action == CaseAction.MUTE.value,
                ModCase.is_active.is_(True),
                ModCase.expires_at.is_not(None),
            )
            result = await session.execute(statement)
            mutes = list(result.scalars().all())
    except (SQLAlchemyError, RuntimeError) as exc:
        logger.warning("Could not load expiring mutes for %s: %s", guild.id, exc)
        return 0

    released = 0
    for case in mutes:
        if not case.is_expired:
            continue
        member = guild.get_member(case.user_id)
        if member is not None and role in member.roles:
            try:
                await member.remove_roles(
                    role, reason=f"Mute expired · {case.case_ref}"
                )
                released += 1
            except discord.Forbidden:
                logger.warning("Cannot unmute %s in guild %s", member.id, guild.id)
            except discord.HTTPException as exc:
                logger.error("HTTP failure lifting mute for %s: %s", member.id, exc)
        # Mark the case resolved either way: the member may have left the guild,
        # and leaving the row active would retry forever.
        try:
            async with (database or get_database()).session() as session:
                stored = await session.get(ModCase, case.id)
                if stored is not None:
                    stored.is_active = False
                    stored.revoked_reason = "expired"
        except (SQLAlchemyError, RuntimeError) as exc:
            logger.warning("Could not close case %s: %s", case.case_ref, exc)

    if released:
        logger.info("Released %s expired mute(s) in guild %s", released, guild.id)
    return released


def utc_expiry(duration: timedelta | None) -> datetime | None:
    """Absolute expiry timestamp for a duration, or ``None`` for permanent."""
    if duration is None:
        return None
    return datetime.now(UTC) + duration


# --------------------------------------------------------------------------- #
# Temporary bans
# --------------------------------------------------------------------------- #
async def expire_stale_bans(
    guild: discord.Guild, database: Database | None = None
) -> list[int]:
    """Lift temporary bans whose window has elapsed.

    Discord has no "ban until" of its own — ``/tempban`` is a ledger row plus a
    background sweep, so this function *is* the feature. It is also what keeps
    the ledger honest after a long downtime: a ban whose expiry passed while the
    process was down is lifted on the next pass, not left hanging.

    Returns:
        The user IDs actually unbanned, for logging and the moderation channel.
    """
    store = database or get_database()
    try:
        async with store.session() as session:
            statement = select(ModCase).where(
                ModCase.guild_id == guild.id,
                ModCase.action == CaseAction.TEMPBAN.value,
                ModCase.is_active.is_(True),
                ModCase.expires_at.is_not(None),
            )
            result = await session.execute(statement)
            bans = list(result.scalars().all())
    except (SQLAlchemyError, RuntimeError) as exc:
        logger.warning("Could not load expiring bans for %s: %s", guild.id, exc)
        return []

    lifted: list[int] = []
    for case in bans:
        if not case.is_expired:
            continue
        # A member who already left needs ``fetch_ban``; ``get_ban`` only sees
        # the guild cache, which does not carry bans at all.
        try:
            entry = await guild.fetch_ban(case.user_id)
        except discord.NotFound:
            entry = None
        except discord.Forbidden:
            logger.warning("Cannot read bans in guild %s", guild.id)
            break
        except discord.HTTPException as exc:
            logger.error("HTTP failure reading ban %s: %s", case.user_id, exc)
            continue

        if entry is not None:
            try:
                await guild.unban(
                    case.user_id, reason=f"Tempban expired · {case.case_ref}"
                )
                lifted.append(case.user_id)
            except discord.NotFound:
                pass
            except discord.Forbidden:
                logger.warning("Cannot unban %s in guild %s", case.user_id, guild.id)
            except discord.HTTPException as exc:
                logger.error("HTTP failure lifting ban for %s: %s", case.user_id, exc)
                continue

        # Close the row either way: a user unbanned by hand must not leave an
        # active case behind forever.
        try:
            async with store.session() as session:
                stored = await session.get(ModCase, case.id)
                if stored is not None:
                    stored.is_active = False
                    stored.revoked_reason = "expired"
        except (SQLAlchemyError, RuntimeError) as exc:
            logger.warning("Could not close case %s: %s", case.case_ref, exc)

    if lifted:
        logger.info("Lifted %s expired tempban(s) in guild %s", len(lifted), guild.id)
    return lifted


# --------------------------------------------------------------------------- #
# Phrase filters
# --------------------------------------------------------------------------- #
async def list_filters(guild_id: int, database: Database | None = None) -> list[str]:
    """Every blocked phrase for a guild, normalized and alphabetical."""
    store = database or get_database()
    try:
        async with store.session() as session:
            statement = (
                select(GuildFilter.phrase)
                .where(GuildFilter.guild_id == guild_id)
                .order_by(GuildFilter.phrase)
            )
            result = await session.execute(statement)
            return list(result.scalars().all())
    except (SQLAlchemyError, RuntimeError) as exc:
        logger.warning("Could not list filters for guild %s: %s", guild_id, exc)
        return []


async def add_filter(
    guild_id: int, phrase: str, *, created_by: int | None = None,
    database: Database | None = None,
) -> bool:
    """Add one normalized phrase. Returns ``True`` when a row was inserted.

    Adding the same phrase twice is a no-op, not an error — moderators retry
    commands, and a duplicate-insert crash would be reported as a failure even
    though the filter is working.
    """
    canonical = normalize_phrase(phrase)[:MAX_KEYWORD_LEN].strip()
    if not canonical:
        return False

    store = database or get_database()
    try:
        async with store.session() as session:
            existing = await session.scalar(
                select(GuildFilter.id).where(
                    GuildFilter.guild_id == guild_id,
                    GuildFilter.phrase == canonical,
                )
            )
            if existing is not None:
                return False
            session.add(
                GuildFilter(
                    guild_id=guild_id,
                    phrase=canonical,
                    created_by=created_by,
                )
            )
        return True
    except (SQLAlchemyError, RuntimeError) as exc:
        logger.warning("Could not add filter %r to guild %s: %s", canonical, guild_id, exc)
        raise


async def remove_filter(
    guild_id: int, phrase: str, *, database: Database | None = None
) -> bool:
    """Remove one phrase. Returns ``True`` when a row was deleted."""
    canonical = normalize_phrase(phrase)[:MAX_KEYWORD_LEN].strip()
    if not canonical:
        return False

    store = database or get_database()
    try:
        async with store.session() as session:
            row = await session.scalar(
                select(GuildFilter).where(
                    GuildFilter.guild_id == guild_id,
                    GuildFilter.phrase == canonical,
                )
            )
            if row is None:
                return False
            await session.delete(row)
        return True
    except (SQLAlchemyError, RuntimeError) as exc:
        logger.warning(
            "Could not remove filter %r from guild %s: %s", canonical, guild_id, exc
        )
        raise


def resolve_target_member(
    guild: discord.Guild, user_id: int
) -> discord.Member:
    """Fetch a member or raise a user-facing error."""
    member = guild.get_member(user_id)
    if member is None:
        raise MissingTargetError(
            "That user is not in this server, so I cannot inspect their roles. "
            "Use a member mention rather than an ID."
        )
    return member
