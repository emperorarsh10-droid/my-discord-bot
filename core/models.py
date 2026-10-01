"""SQLAlchemy ORM models — the persistence layer for moderation state.

Five tables, each with one job:

*   ``guild_configs``   — per-guild settings (log channel, mute role, policy).
*   ``case_counters``   — atomic per-guild case-number sequence.
*   ``mod_cases``       — the append-only moderation ledger (bans, kicks, mutes,
    warnings). Rows are never mutated in place except to revoke a case, so the
    table doubles as an audit trail.
*   ``guild_filters``   — the blocked-phrase blacklist, mirrored into a native
    AutoMod rule by ``core.automod``.
*   ``guild_backups``   — sealed guild configuration snapshots.
*   ``guild_notes``     — private staff context that is deliberately *not* an
    infraction, so it can never inflate the warn ladder.
*   ``channel_snapshots`` — exact permission overwrites captured before
    ``/panic``, ``/nuke`` and ``/lockdownall`` change anything.
*   ``guild_ignores``   — channels, roles and members exempt from AutoMod.
*   ``blacklist_entries`` — bot-enforced phrase/regex patterns.
*   ``temp_role_grants`` — roles with an expiry, so a redeploy cannot strand one.
*   ``sticky_messages``, ``reaction_role_rules`` — persistent channel automation.
*   ``giveaways`` / ``giveaway_entries`` / ``polls`` / ``poll_votes`` — community
    features whose deadlines outlive the process.

New *tables* are added freely: ``create_all`` only ever creates what is missing,
so a deployment picks them up on the next boot. Adding a *column* to an existing
table is a different story — ``create_all`` will not add it, and every SELECT
that names it fails until a migration runs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = [
    "Base",
    "BlacklistEntry",
    "CaseAction",
    "CaseCounter",
    "ChannelSnapshot",
    "Giveaway",
    "GiveawayEntry",
    "GuildBackup",
    "GuildConfig",
    "GuildFilter",
    "GuildIgnore",
    "GuildNote",
    "GuildSetting",
    "ModCase",
    "Poll",
    "PollVote",
    "ReactionRoleRule",
    "StickyMessage",
    "TempRoleGrant",
    "utcnow",
]


def utcnow() -> datetime:
    """Timezone-aware ``now`` used as a Python-side default."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base with a readable ``__repr__`` for debugging."""


class CaseAction(StrEnum):
    """Canonical vocabulary for moderation actions.

    Stored as the plain string value so a new member is a one-line migration
    rather than a schema change.
    """

    BAN = "ban"
    UNBAN = "unban"
    KICK = "kick"
    MUTE = "mute"
    UNMUTE = "unmute"
    WARN = "warn"
    PURGE = "purge"
    ROLE_ADD = "role_add"
    ROLE_REMOVE = "role_remove"
    #: Discord-native communication timeout (the 28-day max), distinct from the
    #: mute *role* used by ``/mute``.
    TIMEOUT = "timeout"
    #: A timeout lifted early by ``/untimeout``.
    UNTIMEOUT = "untimeout"
    #: A role granted with an expiry, revoked by the sweep.
    TEMP_ROLE = "temp_role"
    #: An infraction reversed: ``/clearwarns``, ``/reason`` rewrite, ``/unban``.
    REVOKED = "revoked"
    #: One ban issued as part of a ``/massban`` batch. Recorded per user so the
    #: ledger stays a per-account audit trail rather than a run summary.
    MASSBAN = "massban"
    #: Ban used purely to purge recent messages, then immediately lifted.
    SOFTBAN = "softban"
    #: Ban with a ledger expiry, lifted by the expiry sweep.
    TEMPBAN = "tempban"
    #: A message Discord's native AutoMod blocked on our rule.
    AUTOMOD = "automod"


class GuildConfig(Base):
    """Per-guild configuration. Created lazily on first interaction."""

    __tablename__ = "guild_configs"

    guild_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=False
    )
    log_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    muted_role_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    dm_on_punish: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True, default=True
    )
    #: Warn-ladder rung that silences a member. ``0`` disables the rung.
    warn_timeout_at: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3"
    )
    warn_kick_at: Mapped[int] = mapped_column(
        Integer, nullable=False, default=5, server_default="5"
    )
    #: How long the ladder's automatic timeout lasts.
    warn_timeout_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=60, server_default="60"
    )
    #: Master switch for ``core.automod``. Off by default: enabling it silently
    #: starts deleting members' messages, which must be a deliberate act.
    automod_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    #: Per-rule penalty, one of ``none``/``delete``/``warn``/``timeout``/``kick``.
    #: Stored as text rather than an enum so a new penalty is a one-line change
    #: instead of a migration, matching :class:`CaseAction`'s convention.
    automod_duplicate_action: Mapped[str] = mapped_column(
        String(16), nullable=False, default="delete", server_default="delete"
    )
    automod_caps_action: Mapped[str] = mapped_column(
        String(16), nullable=False, default="warn", server_default="warn"
    )
    automod_invite_action: Mapped[str] = mapped_column(
        String(16), nullable=False, default="delete", server_default="delete"
    )
    #: Anti-caps: flag messages whose letters exceed this share of the text.
    #: ``0`` disables the rule.
    anticaps_percent: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    #: Anti-caps ignores messages shorter than this, which are usually acronyms
    #: and short interjections rather than shouting.
    anticaps_min_length: Mapped[int] = mapped_column(
        Integer, nullable=False, default=12, server_default="12"
    )
    #: Spam window in seconds and the message count that trips inside it.
    spam_window_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=10, server_default="10"
    )
    spam_max_messages: Mapped[int] = mapped_column(
        Integer, nullable=False, default=6, server_default="6"
    )
    #: Seconds of automatic timeout applied to a spam trip.
    spam_timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=60, server_default="60"
    )
    #: Block Discord invite links in chat.
    anti_invite: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    #: Silence every text channel at once while a raid is active.
    panic_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    #: Total actions this guild's moderator log has recorded, denormalised for
    #: cheap display in /serverinfo.
    action_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<GuildConfig guild_id={self.guild_id} "
            f"log_channel_id={self.log_channel_id} muted_role_id={self.muted_role_id}>"
        )

    def __init__(self, **kwargs: Any) -> None:
        # SQLAlchemy applies column defaults at INSERT time, which leaves the
        # Python object holding ``None`` until then — so ``action_count += 1``
        # on a freshly constructed row would explode. Materialising the
        # defaults eagerly makes the object honest from the start.
        kwargs.setdefault("action_count", 0)
        kwargs.setdefault("dm_on_punish", True)
        kwargs.setdefault("warn_timeout_at", 3)
        kwargs.setdefault("warn_kick_at", 5)
        kwargs.setdefault("warn_timeout_minutes", 60)
        kwargs.setdefault("automod_enabled", False)
        kwargs.setdefault("automod_duplicate_action", "delete")
        kwargs.setdefault("automod_caps_action", "warn")
        kwargs.setdefault("automod_invite_action", "delete")
        kwargs.setdefault("anticaps_percent", 0)
        kwargs.setdefault("anticaps_min_length", 12)
        kwargs.setdefault("spam_window_seconds", 10)
        kwargs.setdefault("spam_max_messages", 6)
        kwargs.setdefault("spam_timeout_seconds", 60)
        kwargs.setdefault("anti_invite", False)
        kwargs.setdefault("panic_active", False)
        super().__init__(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "guild_id": self.guild_id,
            "log_channel_id": self.log_channel_id,
            "muted_role_id": self.muted_role_id,
            "dm_on_punish": self.dm_on_punish,
            "action_count": self.action_count,
            "warn_timeout_at": self.warn_timeout_at,
            "warn_kick_at": self.warn_kick_at,
            "warn_timeout_minutes": self.warn_timeout_minutes,
            "automod_enabled": self.automod_enabled,
            "automod_duplicate_action": self.automod_duplicate_action,
            "automod_caps_action": self.automod_caps_action,
            "automod_invite_action": self.automod_invite_action,
            "anticaps_percent": self.anticaps_percent,
            "anticaps_min_length": self.anticaps_min_length,
            "spam_window_seconds": self.spam_window_seconds,
            "spam_max_messages": self.spam_max_messages,
            "spam_timeout_seconds": self.spam_timeout_seconds,
            "anti_invite": self.anti_invite,
            "panic_active": self.panic_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class CaseCounter(Base):
    """Atomic per-guild case sequence.

    ``last_number`` is bumped with ``INSERT ... ON CONFLICT DO UPDATE ...
    RETURNING`` inside the caller's transaction, which is the only portable way
    to get a gap-free, race-free counter across SQLite and PostgreSQL.
    """

    __tablename__ = "case_counters"

    guild_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=False
    )
    last_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<CaseCounter guild_id={self.guild_id} last={self.last_number}>"


class ModCase(Base):
    """One row per moderation action. Append-only by design."""

    __tablename__ = "mod_cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    case_number: Mapped[int] = mapped_column(Integer, nullable=False)
    case_ref: Mapped[str] = mapped_column(String(16), nullable=False, index=True)

    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    moderator_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Absolute expiry for timed mutes; ``NULL`` for permanent or instant actions.
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    #: Set when the case was undone (unban, unmute, purge-log entry reversal).
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, index=True
    )
    revoked_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    __table_args__ = (
        # A guild can never issue the same case number twice.
        UniqueConstraint("guild_id", "case_number", name="uq_case_guild_number"),
        # Covers the default history query: newest cases for one guild.
        Index("ix_mod_cases_guild_created", "guild_id", "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<ModCase {self.case_ref} guild={self.guild_id} "
            f"user={self.user_id} action={self.action} active={self.is_active}>"
        )

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("is_active", True)
        super().__init__(**kwargs)

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        expires = self.expires_at
        if expires.tzinfo is None:  # SQLite round-trips naive datetimes
            expires = expires.replace(tzinfo=UTC)
        return expires <= utcnow()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "guild_id": self.guild_id,
            "case_number": self.case_number,
            "case_ref": self.case_ref,
            "user_id": self.user_id,
            "moderator_id": self.moderator_id,
            "action": self.action,
            "reason": self.reason,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "is_active": self.is_active,
            "revoked_by": self.revoked_by,
            "revoked_reason": self.revoked_reason,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class GuildFilter(Base):
    """One blocked phrase, mirrored into a native AutoMod keyword rule.

    The row is the record of *intent*: which phrases a moderator asked to
    block, and who asked. The rule on Discord is the enforcement; this table is
    what makes ``/filter list`` answerable without a REST round trip and what
    lets the native rule be rebuilt after it is deleted server-side by hand.
    """

    __tablename__ = "guild_filters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    #: Already normalized by ``core.automod.normalize_phrase``, and bounded to
    #: Discord's 60-character keyword limit.
    phrase: Mapped[str] = mapped_column(String(80), nullable=False)
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("guild_id", "phrase", name="uq_filter_guild_phrase"),
    )

    def __repr__(self) -> str:
        return f"<GuildFilter guild_id={self.guild_id} phrase={self.phrase!r}>"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "guild_id": self.guild_id,
            "phrase": self.phrase,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class GuildNote(Base):
    """A private staff note attached to a member.

    Deliberately *not* part of the case ledger. A note is context, not an
    infraction: counting them would silently inflate the warn ladder and hand
    somebody an escalation they never earned.
    """

    __tablename__ = "guild_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    author_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    __table_args__ = (
        Index("ix_guild_notes_guild_user", "guild_id", "user_id", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<GuildNote guild={self.guild_id} user={self.user_id}>"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "guild_id": self.guild_id,
            "user_id": self.user_id,
            "author_id": self.author_id,
            "body": self.body,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class ChannelSnapshot(Base):
    """The exact permission overwrites of one channel, captured before a change.

    ``/panic`` and ``/nuke`` are destructive by nature, so neither is allowed to
    guess at the old state: they record it here first. The payload is a JSON list
    of ``[target_type, target_id, allow_bitfield, deny_bitfield]`` tuples, which
    round-trips through both SQLite and PostgreSQL as text.

    Storing a bitfield rather than booleans matters: ``Permissions`` is an int
    with dozens of independent flags, and storing "view_channel=True" instead of
    the whole mask would silently drop every permission nobody thought to name.
    """

    __tablename__ = "channel_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    #: Operator label, e.g. ``"before-nuke"`` or ``"panic"``.
    label: Mapped[str] = mapped_column(String(64), nullable=False)
    #: ``json.dumps`` of the overwrite tuples, or ``"[]"`` when the channel had none.
    overwrites: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    #: Extra channel state worth restoring: category, position, topic, nsfw.
    channel_meta: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "guild_id", "channel_id", "label", name="uq_snapshot_guild_channel_label"
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<ChannelSnapshot guild={self.guild_id} channel={self.channel_id} "
            f"label={self.label!r}>"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "label": self.label,
            "overwrites": self.overwrites,
            "channel_meta": self.channel_meta,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class GuildIgnore(Base):
    """A channel, role or member whose messages are skipped by AutoMod and spam checks.

    The raid-control escape hatch: during an incident the moderators need the bot
    to keep enforcing, and the fastest way to do that is to stop arguing with a
    specific channel instead of turning the rules off globally.
    """

    __tablename__ = "guild_ignores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    #: ``"channel"``, ``"role"`` or ``"user"``.
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "guild_id", "kind", "target_id", name="uq_ignore_guild_kind_target"
        ),
    )

    def __repr__(self) -> str:
        return f"<GuildIgnore guild={self.guild_id} {self.kind}={self.target_id}>"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "guild_id": self.guild_id,
            "kind": self.kind,
            "target_id": self.target_id,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class BlacklistEntry(Base):
    """A locally-enforced blocked pattern.

    Distinct from :class:`GuildFilter`, which mirrors into Discord's native
    AutoMod. This table is for patterns Discord will not host natively — regular
    expressions, and anything the guild wants enforced by the bot itself so the
    offence is recorded in the case ledger.
    """

    __tablename__ = "blacklist_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    pattern: Mapped[str] = mapped_column(String(200), nullable=False)
    is_regex: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("guild_id", "pattern", name="uq_blacklist_guild_pattern"),
    )

    def __repr__(self) -> str:
        regex = "regex" if self.is_regex else "phrase"
        return f"<BlacklistEntry guild={self.guild_id} {regex}={self.pattern!r}>"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "guild_id": self.guild_id,
            "pattern": self.pattern,
            "is_regex": self.is_regex,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class TempRoleGrant(Base):
    """A role that will be removed at ``expires_at``.

    Persisted rather than held in a background task: a Railway redeploy restarts
    the process, and an in-memory task would leave the role attached forever.
    The sweeper in ``cogs.membertools`` revokes these on boot.
    """

    __tablename__ = "temp_role_grants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    role_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    granted_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "guild_id", "user_id", "role_id", name="uq_temp_role_guild_user_role"
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<TempRoleGrant guild={self.guild_id} user={self.user_id} "
            f"role={self.role_id} expires={self.expires_at}>"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "guild_id": self.guild_id,
            "user_id": self.user_id,
            "role_id": self.role_id,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "granted_by": self.granted_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class StickyMessage(Base):
    """A message re-posted after every other message in the channel."""

    __tablename__ = "sticky_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    #: The live sticky post. Without it, removing a sticky means deleting whichever
    #: of my recent messages happens to be newest, which is how a moderation log
    #: or an audit entry disappears.
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("guild_id", "channel_id", name="uq_sticky_guild_channel"),
    )

    def __repr__(self) -> str:
        return f"<StickyMessage guild={self.guild_id} channel={self.channel_id}>"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "content": self.content,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class ReactionRoleRule(Base):
    """One emoji on one message grants one role when clicked."""

    __tablename__ = "reaction_role_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    #: Raw emoji as typed, or ``"<:name:id>"`` for a custom one.
    emoji: Mapped[str] = mapped_column(String(64), nullable=False)
    role_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "message_id", "emoji", "role_id", name="uq_reaction_role_message_emoji_role"
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<ReactionRoleRule message={self.message_id} "
            f"emoji={self.emoji!r} role={self.role_id}>"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "message_id": self.message_id,
            "emoji": self.emoji,
            "role_id": self.role_id,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Giveaway(Base):
    """A giveaway and its lifecycle.

    ``ends_at`` is authoritative, not the in-memory task: the sweeper finalises
    overdue giveaways on boot, so a redeploy mid-draw still pays out a winner.
    """

    __tablename__ = "giveaways"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    prize: Mapped[str] = mapped_column(String(256), nullable=False)
    winners: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    ends_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    ended: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<Giveaway guild={self.guild_id} prize={self.prize!r} "
            f"ended={self.ended}>"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "message_id": self.message_id,
            "prize": self.prize,
            "winners": self.winners,
            "ends_at": self.ends_at.isoformat() if self.ends_at else None,
            "ended": self.ended,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class GiveawayEntry(Base):
    """One participant in one giveaway."""

    __tablename__ = "giveaway_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    giveaway_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("giveaway_id", "user_id", name="uq_giveaway_entry"),
    )

    def __repr__(self) -> str:
        return f"<GiveawayEntry giveaway={self.giveaway_id} user={self.user_id}>"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "giveaway_id": self.giveaway_id,
            "user_id": self.user_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Poll(Base):
    """A button-driven poll."""

    __tablename__ = "polls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    question: Mapped[str] = mapped_column(String(300), nullable=False)
    #: JSON list of option labels, in display order.
    options: Mapped[str] = mapped_column(Text, nullable=False)
    #: ``True`` for single-choice, ``False`` for "pick as many as you like".
    multiple: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    closed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: When voting stops. Durable so a restart cannot leave a poll open forever;
    #: ``None`` means the poll closes only when a moderator ends it.
    ends_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<Poll guild={self.guild_id} question={self.question!r}>"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "message_id": self.message_id,
            "question": self.question,
            "options": self.options,
            "multiple": self.multiple,
            "closed": self.closed,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class PollVote(Base):
    """One member's choice in one poll. Re-voting replaces the row."""

    __tablename__ = "poll_votes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    poll_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    #: Index into ``Poll.options``.
    option_index: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # Keyed per option, not per user: a multi-choice poll needs one row per
        # selected option, and the per-option key is what stops a double-click
        # on the same button from inflating a tally.
        UniqueConstraint("poll_id", "user_id", "option_index", name="uq_poll_vote"),
    )

    def __repr__(self) -> str:
        return (
            f"<PollVote poll={self.poll_id} user={self.user_id} "
            f"option={self.option_index}>"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "poll_id": self.poll_id,
            "user_id": self.user_id,
            "option_index": self.option_index,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class GuildSetting(Base):
    """One arbitrary key/value setting per guild.

    A general escape hatch for the handful of values that do not earn a column
    on :class:`GuildConfig`: per-rule flags like "delete the message or time the
    author out". Keys are namespaced by the caller (``blacklist.delete:<pattern>``)
    so two features cannot collide.
    """

    __tablename__ = "guild_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(160), nullable=False)
    #: Always stored as text; the caller owns the encoding.
    value: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("guild_id", "key", name="uq_setting_guild_key"),
    )

    def __repr__(self) -> str:
        return f"<GuildSetting guild={self.guild_id} key={self.key!r}>"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "guild_id": self.guild_id,
            "key": self.key,
            "value": self.value,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class GuildBackup(Base):
    """A sealed guild configuration snapshot.

    ``payload`` is the output of :func:`core.backup.encode_payload`: base64 of
    an obfuscated, MAC-protected body. It is stored as text rather than bytes so
    the same column type works on SQLite and PostgreSQL without a migration.
    """

    __tablename__ = "guild_backups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    #: Operator-supplied label, sanitized to a filesystem-safe token.
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    channel_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    role_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # Re-creating a backup under the same name replaces it rather than
        # accumulating near-duplicates that nobody can tell apart later.
        UniqueConstraint("guild_id", "name", name="uq_backup_guild_name"),
    )

    def __repr__(self) -> str:
        return (
            f"<GuildBackup guild_id={self.guild_id} name={self.name!r} "
            f"roles={self.role_count} channels={self.channel_count}>"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "guild_id": self.guild_id,
            "name": self.name,
            "channel_count": self.channel_count,
            "role_count": self.role_count,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
