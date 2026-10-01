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
    "CaseAction",
    "CaseCounter",
    "GuildBackup",
    "GuildConfig",
    "GuildFilter",
    "ModCase",
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
        super().__init__(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "guild_id": self.guild_id,
            "log_channel_id": self.log_channel_id,
            "muted_role_id": self.muted_role_id,
            "dm_on_punish": self.dm_on_punish,
            "action_count": self.action_count,
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
