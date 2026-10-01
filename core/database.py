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

from sqlalchemy import (
    BigInteger,
    Column,
    Connection,
    Dialect,
    Enum,
    Inspector,
    Integer,
    MetaData,
    String,
    Text,
    event,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import AsyncAdaptedQueuePool

from config import PROJECT_ROOT, ConfigurationError, get_settings
from core.dashboard_state import HealthProbe, runtime_state
from core.logging_setup import get_logger
from core.models import Base, CaseCounter

__all__ = ["Database", "get_database", "reset_database"]

logger = get_logger("zagrosian.database")

_PROBE_STATEMENT: Final[str] = "SELECT 1"

#: Seconds a checkout waits for a free connection before raising. Matches the
#: default slash-command interaction budget so a saturated pool surfaces as a
#: clean error well inside Discord's 3s initial-response window being deferred.
SQLITE_POOL_TIMEOUT: Final[float] = 30.0


def render_literal(value: Any, dialect: Dialect) -> str:
    """Render a Python default as SQL text safe for this dialect.

    A bare ``server_default="delete"`` reaches SQLite as the token ``delete``,
    which is a syntax error, not a string. Embedding the value through the
    dialect's own escaping is what makes the migration portable; hand-rolling
    quotes here is how a Windows path or an apostrophe breaks the boot.
    """
    # Escape single quotes by doubling them, which both SQLite and PostgreSQL
    # accept for a text default. Numeric defaults need no quoting.
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    text_value = str(value).replace("'", "''")
    return f"'{text_value}'"


def _column_ddl(column: Column[Any], dialect: Dialect) -> str:
    """Render one column as portable ``ADD COLUMN`` DDL.

    Only types this bot actually uses are handled. An unknown type raises rather
    than emitting SQL that would fail on one dialect and silently succeed on
    another — a loud boot failure beats a column that exists with the wrong
    shape.
    """
    resolved = column.type.compile(dialect=dialect)
    # Enum renders as its name, which the live table already has; VARCHAR is the
    # portable stand-in for anything textual.
    if isinstance(column.type, (String, Text, Enum)):
        resolved = "VARCHAR"
    # BigInteger on SQLite is a 64-bit INTEGER under the hood, and the dialect
    # renders it as BIGINT which SQLite accepts but normalises to INTEGER.
    if isinstance(column.type, (BigInteger, Integer)):
        resolved = "INTEGER"

    parts = [column.name, resolved]
    if not column.nullable:
        # Only ever reachable for a column that also carries a server default;
        # a NOT NULL addition without one would fail against existing rows.
        server_default = column.server_default
        if server_default is None:
            raise RuntimeError(
                f"column {column.name!r} is NOT NULL with no server default; "
                "add a default so the migration can populate existing rows"
            )
        parts.append(f"DEFAULT {render_literal(server_default.arg, dialect)}")
    return " ".join(parts)


def _add_missing_columns_sync(connection: Connection, metadata: MetaData) -> list[str]:
    """Add absent columns in place. Returns ``table.column`` names added.

    Runs on the sync ``Connection`` that ``AsyncConnection.run_sync`` provides.
    The dialect is read from that connection rather than guessed, so the rendered
    DDL matches the server that will execute it.
    """
    # The Dialect object itself, not its name: TypeEngine.compile() resolves
    # type names and default renderers through it.
    dialect = connection.dialect
    inspector = inspect(connection)
    existing_tables = set(inspector.get_table_names())
    added: list[str] = []

    for table in metadata.sorted_tables:
        # A table absent here was just created by create_all, so it already has
        # every column in the metadata and there is nothing to reconcile.
        if table.name not in existing_tables:
            continue

        present = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue
            ddl = _column_ddl(column, dialect)
            connection.exec_driver_sql(
                f'ALTER TABLE {table.name} ADD COLUMN {ddl}'
            )
            added.append(f"{table.name}.{column.name}")

    added.extend(_migrate_poll_vote_key(connection, inspector))
    return added


def _migrate_poll_vote_key(connection: Connection, inspector: Inspector) -> list[str]:
    """Widen ``poll_votes``'s unique key to include ``option_index``.

    The original key was ``(poll_id, user_id)``, which structurally forbids a
    multi-choice poll from recording more than one selection per member. Adding a
    column cannot fix that, so the constraint has to be dropped and recreated;
    SQLite and PostgreSQL spell that differently, which is why it is one branch
    per dialect rather than a generic attempt.
    """
    if "poll_votes" not in set(inspector.get_table_names()):
        return []

    wanted = {"poll_id", "user_id", "option_index"}
    for constraint in inspector.get_unique_constraints("poll_votes"):
        columns = set(constraint.get("column_names") or ())
        if columns == wanted:
            return []

    logger.info("Widening poll_votes unique key to (poll_id, user_id, option_index)")
    if connection.dialect.name == "postgresql":
        connection.exec_driver_sql("ALTER TABLE poll_votes DROP CONSTRAINT uq_poll_vote")
    else:
        # SQLite cannot drop a constraint; recreating the table is the only way.
        connection.exec_driver_sql("DROP TABLE IF EXISTS poll_votes__old")
        connection.exec_driver_sql(
            "CREATE TABLE poll_votes__old AS SELECT * FROM poll_votes"
        )
        connection.exec_driver_sql("DROP TABLE poll_votes")
        connection.exec_driver_sql(
            """
            CREATE TABLE poll_votes (
                id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
                poll_id INTEGER NOT NULL,
                user_id BIGINT NOT NULL,
                option_index INTEGER NOT NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (poll_id, user_id, option_index)
            )
            """
        )
        connection.exec_driver_sql(
            "INSERT INTO poll_votes SELECT * FROM poll_votes__old"
        )
        connection.exec_driver_sql("DROP TABLE poll_votes__old")
        connection.exec_driver_sql(
            "CREATE INDEX ix_poll_votes_poll_id ON poll_votes (poll_id)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX ix_poll_votes_user_id ON poll_votes (user_id)"
        )
    return ["poll_votes.unique_key"]


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
            # aiosqlite runs every statement on a worker thread, so a pooled
            # connection is worth real time: measured 1.9ms/query against 6.1ms
            # for NullPool, because NullPool re-opens the file and re-runs
            # busy_timeout+PRAGMA setup on every checkout.
            #
            # SQLite still serialises *writers*, so the pool is deliberately
            # small: five connections is enough to keep read traffic off the
            # write lock, and max_overflow=0 stops a burst of commands from
            # queueing on ``database is locked`` instead of waiting on the pool.
            kwargs["poolclass"] = AsyncAdaptedQueuePool
            kwargs["pool_size"] = self._settings.sqlite_pool_size
            kwargs["max_overflow"] = 0
            kwargs["pool_timeout"] = SQLITE_POOL_TIMEOUT
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
        """Create missing tables, then add missing columns to existing ones.

        ``create_all`` creates tables but never alters them, so a deployment that
        predates a new column keeps the old shape and every ORM query naming that
        column fails. This bot ships additive columns often enough that a real
        migration framework is overkill, so the gaps are filled with
        ``ADD COLUMN`` guarded by an inspection of the live table.

        Rules this deliberately obeys:

        *   **Additive only.** Nothing is dropped, renamed or retyped. There is no
            ``down`` path because there is no destructive step to undo.
        *   **Idempotent.** The column list comes from the live table on every
            boot, so re-running is free and a partially applied migration heals.
        *   **Nullable or defaulted.** A new column must never be ``NOT NULL``
            without a server default, because adding it to a populated table
            would otherwise fail on the existing rows.
        """
        engine = self._require_engine()
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            added = await connection.run_sync(
                _add_missing_columns_sync, Base.metadata
            )

        if added:
            logger.info("Schema migration | added columns: %s", ", ".join(added))
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
