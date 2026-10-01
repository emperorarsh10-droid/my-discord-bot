"""Async database access layer.

Holds the single :class:`Database` instance that owns the SQLAlchemy async
engine and session factory, and exposes the three things the rest of the app
needs:

*   :meth:`Database.session` — a transactional session context manager.
*   :meth:`Database.health` — a timed, self-healing ``SELECT 1`` probe used by
    ``/test`` and the Web Watcher.
*   :meth:`next_case_number` — an atomic, dialect-aware case allocator.

All public coroutines are safe to call from the gateway task, from the HTTP
server task and from ``asyncio.to_thread``; the engine itself is designed for
concurrent use.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Final

from sqlalchemy import event, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from config import PROJECT_ROOT, ConfigurationError, get_settings
from core.dashboard_state import HealthProbe, runtime_state
from core.logging_setup import get_logger
from core.models import Base, CaseCounter

__all__ = ["Database", "get_database", "reset_database"]

logger = get_logger("zagrosian.database")

_PROBE_STATEMENT: Final[str] = "SELECT 1"


class Database:
    """Owns engine lifecycle and hands out sessions."""

    __slots__ = ("_engine", "_initialized", "_lock", "_session_factory", "_settings")

    def __init__(self) -> None:
        self._settings = get_settings()
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None
        self._initialized: bool = False
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def _sqlite_path(self) -> Path | None:
        """Absolute path of the SQLite file, or ``None`` for in-memory.

        ``config._normalize_sqlite_target`` has already resolved a relative
        target against the project root, so by the time a DSN reaches here the
        path is absolute and this returns it unchanged. The ``Path`` fallback
        below is a safety net rather than the normal path.
        """
        if not self._settings.uses_sqlite:
            return None

        _, separator, remainder = self._settings.database_url.partition("://")
        if not separator:
            return None

        # Strip any query string (?timeout=5 and friends) before touching it.
        target = remainder.split("?", 1)[0]
        if not target or target.lstrip("/") == ":memory:":
            return None

        # config._normalize_sqlite_target() re-emits relative paths as an
        # absolute path in this host's dialect, which means the text here is not
        # necessarily what the native Path() constructor expects:
        #
        #   POSIX    "sqlite:////srv/app.db"  -> database "/srv/app.db"
        #   Windows  "sqlite:///C:/app.db"    -> database "C:/app.db"
        #                                              (SQLAlchemy reads "C:" as host)
        #
        # Both arrive here with a leading slash that Path() would turn into a
        # UNC root ("\\srv") or a doubled drive ("C:\C:"). Decoding the DSN form
        # by hand, rather than trusting the platform, keeps this identical on
        # every host — which is the whole point of the exercise.
        candidate = Path(target.lstrip("/") if os.name == "nt" else target)
        if not candidate.is_absolute():
            candidate = PROJECT_ROOT / candidate
        return candidate

    def _ensure_sqlite_directory(self) -> None:
        """Create the directory holding the SQLite file.

        Failures are reported, not raised as ``PermissionError``: an unwritable
        data directory is a deployment problem, and the connect attempt that
        follows produces a far clearer error than a bare ``mkdir`` traceback.
        """
        db_path = self._sqlite_path()
        if db_path is None:
            return

        directory = db_path.parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConfigurationError(
                f"Cannot create the SQLite data directory {directory}: {exc}. "
                "Point DATABASE_URL at a writable location, e.g. "
                "sqlite+aiosqlite:///./data/zagrosian_eye.db (relative paths are "
                f"resolved against {PROJECT_ROOT})."
            ) from exc

        if not os.access(directory, os.W_OK):
            raise ConfigurationError(
                f"SQLite data directory {directory} is not writable. A PaaS free "
                "tier usually mounts the app read-only; use PostgreSQL instead "
                "(DATABASE_URL=postgresql+asyncpg://user:pass@host/db)."
            )

    def _build_engine(self) -> AsyncEngine:
        self._ensure_sqlite_directory()

        kwargs: dict[str, Any] = {
            "echo": self._settings.log_level == "DEBUG",
            "future": True,
            # Recycle well before typical proxy/DB idle timeouts so a long-lived
            # bot never hands out a dead connection.
            "pool_recycle": 1800,
            "pool_pre_ping": True,
        }

        if self._settings.uses_sqlite:
            # SQLite serialises writers; a single shared connection keeps
            # "database is locked" out of the hot path.
            kwargs["poolclass"] = NullPool
            kwargs["connect_args"] = {"timeout": int(self._settings.database_timeout)}

        engine = create_async_engine(self._settings.database_url, **kwargs)

        if self._settings.uses_sqlite:
            self._apply_sqlite_pragmas(engine)

        return engine

    @staticmethod
    def _apply_sqlite_pragmas(engine: AsyncEngine) -> None:
        """Enforce the pragmas that make SQLite safe for a concurrent bot.

        ``WAL`` removes reader/writer blocking, ``foreign_keys`` is off by
        default in SQLite (silently!), and ``busy_timeout`` converts lock
        contention into a wait instead of an immediate ``database is locked``.
        """

        @event.listens_for(Engine, "connect")
        def _set_pragmas(dbapi_connection: Any, _record: Any) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.execute("PRAGMA busy_timeout=5000")
            finally:
                cursor.close()

    async def connect(self) -> None:
        """Create the engine, verify connectivity and ensure the schema exists.

        Idempotent and safe to call concurrently.
        """
        async with self._lock:
            if self._initialized and self._engine is not None:
                return

            self._engine = self._build_engine()
            self._session_factory = async_sessionmaker(
                bind=self._engine,
                class_=AsyncSession,
                expire_on_commit=False,
                autoflush=False,
            )

            try:
                async with self._engine.connect() as connection:
                    await connection.execute(text(_PROBE_STATEMENT))
                logger.info("Database engine ready | dialect=%s", self.dialect_name)
            except SQLAlchemyError:
                await self._teardown_engine()
                raise

            await self.create_schema()
            self._initialized = True

    async def create_schema(self) -> None:
        """Create any missing tables.

        Deliberately not a migration system: a fresh install gets a working
        schema immediately, while existing deployments are expected to use
        Alembic (see README).
        """
        engine = self._require_engine()
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        logger.info(
            "Schema verified | tables=%s",
            ", ".join(sorted(Base.metadata.tables)),
        )

    async def _teardown_engine(self) -> None:
        engine, self._engine = self._engine, None
        self._session_factory = None
        self._initialized = False
        if engine is not None:
            with contextlib.suppress(Exception):
                await engine.dispose()

    async def disconnect(self) -> None:
        """Dispose of the engine. Safe to call when never connected."""
        async with self._lock:
            logger.info("Disposing database engine pool")
            await self._teardown_engine()

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #
    def _require_engine(self) -> AsyncEngine:
        if self._engine is None:
            raise RuntimeError(
                "Database.connect() has not completed; call it during bot setup"
            )
        return self._engine

    async def _ensure_session_factory(self) -> async_sessionmaker[AsyncSession]:
        """Return the session factory, connecting on demand.

        Uses an explicit ``RuntimeError`` rather than ``assert`` so the failure
        against a backed-out connection survives ``python -O``.
        """
        if self._session_factory is None:
            await self.connect()
        if self._session_factory is None:
            raise RuntimeError("Database session factory unavailable after connect()")
        return self._session_factory

    @property
    def is_ready(self) -> bool:
        return self._initialized and self._engine is not None

    @property
    def dialect_name(self) -> str:
        """Engine name for display, e.g. ``sqlite`` or ``postgresql``."""
        if self._engine is None:
            return self._settings.database_dialect
        return self._engine.dialect.name

    @property
    def server_version(self) -> str | None:
        """Backend version string, or ``None`` before the first probe."""
        if self._engine is None:
            return None
        return getattr(self._engine.dialect, "_server_version_info", None) and str(
            self._engine.dialect.server_version_info
        )

    # ------------------------------------------------------------------ #
    # Sessions
    # ------------------------------------------------------------------ #
    @contextlib.asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Transactional session scope.

        Commits on clean exit, rolls back on any exception, and always returns
        the connection to the pool.
        """
        factory = await self._ensure_session_factory()
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    # ------------------------------------------------------------------ #
    # Health probe
    # ------------------------------------------------------------------ #
    async def health(self) -> dict[str, Any]:
        """Time a ``SELECT 1`` and record the outcome on the runtime state.

        Never raises: a dead database must be *reported*, not propagated into a
        slash command that is trying to display the failure.

        Returns a JSON-ready dict; ``ok`` is the only field callers must check.
        """
        probe = HealthProbe(url=self._settings.database_dialect)
        result: dict[str, Any] = {
            "ok": False,
            "dialect": self.dialect_name,
            "latency_ms": None,
            "server_version": None,
            "error": None,
            "tables": None,
        }

        if self._engine is None:
            result["error"] = "engine not initialised"
            probe.record(ok=False, latency_ms=0.0, error=result["error"])
            runtime_state.database = probe
            return result

        started = time.perf_counter()
        try:
            async with asyncio.timeout(self._settings.database_timeout):
                async with self._engine.connect() as connection:
                    await connection.execute(text(_PROBE_STATEMENT))
                    result["server_version"] = self._safe_server_version(connection)
                    result["tables"] = sorted(Base.metadata.tables)
        except TimeoutError:
            result["error"] = (
                f"no response within {self._settings.database_timeout:.1f}s"
            )
            logger.error("Database health check timed out")
        except DBAPIError as exc:
            result["error"] = _describe_db_error(exc)
            logger.exception("Database health check failed")
            # A broken pooled connection poisons every future checkout; drop the
            # pool so the next call reconnects instead of failing forever.
            await self._dispose_after_failure()
        except SQLAlchemyError as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            logger.exception("Database health check raised a SQLAlchemy error")

        elapsed_ms = (time.perf_counter() - started) * 1000
        result["latency_ms"] = round(elapsed_ms, 2)
        result["ok"] = result["error"] is None
        probe.record(
            ok=result["ok"],
            latency_ms=elapsed_ms,
            error=result["error"],
        )
        runtime_state.database = probe
        return result

    def _safe_server_version(self, connection: Any) -> str | None:
        try:
            return str(connection.dialect.server_version_info)
        except Exception:  # noqa: BLE001 - version reporting is best effort
            return None

    async def _dispose_after_failure(self) -> None:
        with contextlib.suppress(Exception):
            await self._engine.dispose()  # type: ignore[union-attr]
        logger.warning("Database engine pool disposed after connection failure")

    # ------------------------------------------------------------------ #
    # Case allocation
    # ------------------------------------------------------------------ #
    async def next_case_number(self, guild_id: int) -> int:
        """Atomically increment and return the guild's case counter.

        Uses ``INSERT ... ON CONFLICT DO UPDATE ... RETURNING``, which is the
        only construct that is both atomic and portable between SQLite (3.35+)
        and PostgreSQL. A plain ``SELECT max()`` would race between two
        moderators banning people at the same moment.
        """
        factory = await self._ensure_session_factory()

        dialect = self.dialect_name
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            statement = pg_insert(CaseCounter).values(
                guild_id=guild_id, last_number=1
            )
        elif dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert

            statement = sqlite_insert(CaseCounter).values(
                guild_id=guild_id, last_number=1
            )
        else:  # pragma: no cover - guarded by config validation
            raise RuntimeError(f"No atomic case allocator for dialect {dialect!r}")

        statement = statement.on_conflict_do_update(
            index_elements=[CaseCounter.guild_id],
            set_={"last_number": CaseCounter.last_number + 1},
        ).returning(CaseCounter.last_number)

        async with factory() as session:
            result = await session.execute(statement)
            number = result.scalar_one()
            await session.commit()
        return int(number)

    async def count_cases(self, guild_id: int | None = None) -> int:
        """Total case rows, optionally scoped to one guild."""
        from core.models import ModCase

        factory = await self._ensure_session_factory()
        async with factory() as session:
            statement = select(ModCase.id)
            if guild_id is not None:
                statement = statement.where(ModCase.guild_id == guild_id)
            rows = await session.execute(statement)
        return len(rows.all())


def _describe_db_error(exc: DBAPIError) -> str:
    """Turn a driver exception into one line safe for an embed field."""
    original = exc.orig
    message = str(getattr(original, "args", [original])[0] if original else exc)
    # Strip the credentials-bearing DSN if the driver echoed it back.
    for marker in ("password", "://"):
        if marker == "://" and "://" in message:
            scheme, _, rest = message.partition("://")
            if "@" in rest:
                _, _, location = rest.rpartition("@")
                message = f"{scheme}://***@{location}"
            break
    return f"{type(original).__name__}: {message}"


_database: Database | None = None


def get_database() -> Database:
    """Return the process-wide :class:`Database`, creating it on first use."""
    global _database
    if _database is None:
        _database = Database()
    return _database


def reset_database() -> None:
    """Drop the singleton (test helper). The caller must await ``disconnect``."""
    global _database
    _database = None
