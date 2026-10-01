"""Configuration layer for The Zagrosian Eye.

Responsibilities
----------------
1.  Load and validate every runtime knob from the environment (``.env`` file or
    real process env) exactly once, through a cached factory.
2.  Never let a secret reach a log line, a traceback or a slash-command reply.
    The token is stored as ``SecretStr`` and every human-facing projection goes
    through :meth:`Settings.safe_summary`.
3.  Normalize database DSNs so operators can paste the short forms they see in
    dashboards (``postgres://``, ``sqlite:///``) without reading driver docs.
4.  Fail loudly and early: a misconfigured deployment must die during import of
    the settings object, not three commands deep inside a command handler.

Usage
-----
    from config import get_settings

    settings = get_settings()          # cached, safe to call anywhere
    token     = settings.bot_token     # raises ConfigurationError if unset
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "PROJECT_ROOT",
    "ConfigurationError",
    "Settings",
    "get_settings",
    "reset_settings_cache",
]

# ``config.py`` sits at the repository root; anchor every relative path to it so
# the bot behaves identically no matter what the shell's CWD happens to be.
PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent

# Discord bot tokens are three dot-separated base64url segments.
_TOKEN_SHAPE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_\-.]+$")

CommandSyncMode = Literal["global", "guild"]


class ConfigurationError(RuntimeError):
    """Raised when the environment cannot produce a usable configuration."""


#: SQLite spells an absolute path with four slashes — a POSIX convention. On
#: Windows the same prefix would make the DSN a UNC network reference
#: (``\\\\host\\share``), which resolves to nothing, so it is only applied on
#: POSIX hosts. Windows' native ``C:\\...`` form is already unambiguous.
_PREFIX_WITH_DOUBLE_SLASH: Final[bool] = os.name == "posix"

#: Environment variable names accepted for the Discord bot token, in precedence
#: order. Shared with :attr:`Settings.bot_token`'s error message so the two can
#: never drift apart, and referenced by the selftest suite as the contract.
DISCORD_TOKEN_ENV_NAMES: Final[tuple[str, ...]] = (
    "DISCORD_BOT_TOKEN",
    "DISCORD_TOKEN",
    "BOT_TOKEN",
)


#: Substrings that suggest a variable is meant to hold the bot token, used only
#: to build the "token is not set" diagnostic. Matching is on the *name* alone,
#: and no value is ever read, printed or logged.
_TOKEN_NAME_HINTS: Final[tuple[str, ...]] = ("TOKEN", "SECRET", "DISCORD", "BOT")


def _suspicious_token_var_names() -> list[str]:
    """Environment variable names that look like a token holder but were ignored.

    When the bot refuses to start, the single most useful fact is whether the
    token arrived at all. Without this, "no token found" is indistinguishable
    from "token present under a name nobody guessed" — and a container that
    exits 4 on every restart gives no room to experiment.

    Only names are returned. Values are never inspected, so this cannot leak a
    credential into a log line, and a host full of unrelated secrets produces a
    short list rather than a dump.
    """
    accepted = {name.upper() for name in DISCORD_TOKEN_ENV_NAMES}
    return sorted(
        name
        for name in os.environ
        if name.upper() not in accepted
        and any(hint in name.upper() for hint in _TOKEN_NAME_HINTS)
    )


def _missing_token_message() -> str:
    """Explain precisely which token variables were looked for, and what was seen."""
    accepted = ", ".join(DISCORD_TOKEN_ENV_NAMES)
    message = (
        f"No Discord bot token found. Set one of: {accepted}. "
        f"Checked {len(DISCORD_TOKEN_ENV_NAMES)} name(s) in the process environment "
        "and .env."
    )

    overlooked = _suspicious_token_var_names()
    if overlooked:
        shown = ", ".join(overlooked[:6])
        more = f" (+{len(overlooked) - 6} more)" if len(overlooked) > 6 else ""
        message += (
            f" These look token-related but were NOT read: {shown}{more}. "
            "If one of them holds the bot token, rename it to "
            f"{DISCORD_TOKEN_ENV_NAMES[0]}."
        )
    else:
        message += (
            " No token-like variable is present in the environment at all, so the "
            "value was never supplied — add it on your host's dashboard. Note that "
            "variables added to a different service, environment or redeploy group "
            "are not inherited by this one."
        )
    return message


def _is_absolute_sqlite_target(path_part: str) -> bool:
    """True when a SQLite DSN target names an absolute filesystem location.

    Judged purely by DSN grammar, never by the host OS. The SQLite dialect spells
    an absolute path with **two or more** leading slashes in the remainder — the
    first belongs to the ``://`` separator:

    ====================  ==================  ==========
    DSN                    remainder            meaning
    ====================  ==================  ==========
    ``sqlite:///x.db``     ``/x.db``           relative
    ``sqlite:////x.db``    ``//x.db``          absolute
    ====================  ==================  ==========

    Using the native :class:`~pathlib.Path` here would make the answer depend on
    the platform: ``Path("/data")`` is absolute on Linux but relative on Windows,
    so a check written that way passes on a desktop and raises
    ``PermissionError: [Errno 13]`` in production. Counting slashes is a property
    of the DSN string, so it gives the same answer everywhere.

    A Windows-style ``C:/...`` target is absolute on every platform and is
    detected explicitly.
    """

    if not path_part:
        return False
    if re.match(r"^[A-Za-z]:[\\/]", path_part):
        return True
    return len(path_part) - len(path_part.lstrip("/")) >= 2


def _encode_sqlite_absolute(path: Path) -> str:
    """Spell an absolute filesystem path the way *this* host's driver expects.

    SQLAlchemy reads the text after ``://`` as ``[host][/database]``, so the
    encoding of an absolute path is genuinely platform-specific. Verified
    against the installed SQLAlchemy 2.x + aiosqlite:

    ==========  ==============================  ==========================
    Host        Absolute POSIX path             Windows drive path
    ==========  ==============================  ==========================
    POSIX       ``sqlite:////srv/app.db``       n/a
    Windows     n/a                             ``sqlite:///C:/app.db``
    ==========  ==============================  ==========================

    The Windows row is the non-obvious one. The documented four-slash form
    (``////C:/app.db``) fails with ``unable to open database file``, while the
    three-slash form works because SQLAlchemy parses ``C:`` as the *host* and
    keeps ``/C:/app.db`` as the database, which the driver opens natively.
    Emitting ``////C:/...`` on Windows instead yields a UNC path
    (``\\\\C:\\app.db``), which is why this is not simply POSIX everywhere.
    """
    text = path.as_posix()
    if _PREFIX_WITH_DOUBLE_SLASH:
        return f"//{text}"
    # Windows still needs exactly one leading slash, so the finished DSN reads
    # "sqlite:///C:/app.db". Dropping it yields "sqlite://C:/app.db", where
    # SQLAlchemy parses "C:" as the host and raises
    # ValueError: invalid literal for int() with base 10: ''
    return f"/{text}"


def _normalize_sqlite_target(remainder: str) -> str:
    """Return the path portion of a SQLite DSN, anchored to the project root.

    SQLite DSNs are ambiguous by design, and getting this wrong fails in a way
    that looks unrelated to configuration:

    ``sqlite:///rel.db``      relative path
    ``sqlite:////abs.db``     absolute path (four slashes)

    A *three*-slash DSN whose remainder begins with ``/`` — e.g.
    ``sqlite+aiosqlite:///data/zagrosian_eye.db`` — parses as an **absolute**
    path on Linux, so ``Path(...).parent`` is ``/data`` and creating it raises
    ``PermissionError: [Errno 13]``. The same string is merely "relative" on
    Windows, which is why this reproduces on a Linux host and not on a desktop.

    So every relative target is resolved against ``PROJECT_ROOT`` and re-emitted
    as an unambiguous absolute path for the running platform. A target the
    operator clearly meant as absolute is left verbatim: relocating somebody's
    database because their DSN looked odd is worse than honouring it.

    ``:memory:`` is a sentinel rather than a filename, and the slash-stripping
    below would otherwise turn it into a file literally called ``:memory:``.
    """

    path_part, separator, query = remainder.partition("?")

    # A remainder carrying two or more leading slashes is the four-slash
    # absolute form the operator typed deliberately, so it is honoured exactly.
    # Silently relocating it would point the bot at an empty database.
    if _is_absolute_sqlite_target(path_part):
        return remainder

    # Compare after stripping slashes: SQLite spells the in-memory sentinel as
    # "sqlite:///:memory:", so the value seen here is "/:memory:".
    stripped = path_part.lstrip("/")
    if not stripped or stripped == ":memory:":
        return remainder

    relative = stripped.lstrip("./").lstrip("/")
    if not relative:
        return remainder

    resolved = (PROJECT_ROOT / relative).resolve()

    absolute = _encode_sqlite_absolute(resolved)
    return f"{absolute}{separator}{query}" if separator else absolute


class Settings(BaseSettings):
    """Validated, immutable view over the process environment."""

    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env",),
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore",
        validate_assignment=True,
        # Complex fields (``owner_ids``) are parsed by their validators, not by
        # an upfront ``json.loads``. Without this, an empty ``OWNER_IDS=`` in
        # ``.env`` — a perfectly valid "no owners" — crashes at startup with a
        # JSONDecodeError before any validator runs.
        enable_decoding=False,
    )

    # -- Discord ------------------------------------------------------------
    discord_bot_token: SecretStr = Field(
        default=SecretStr(""),
        # PaaS dashboards each invent their own name for a secret, and a bot
        # deployed under one of the wrong ones fails at the gateway handshake
        # with a message that points nowhere near the real mistake. Rather than
        # guessing one name, accept every spelling seen in the wild — in this
        # order, first non-empty wins:
        #
        #   DISCORD_BOT_TOKEN  the documented name, and the one .env.example uses
        #   DISCORD_TOKEN      Railway's "bot token" template default
        #   BOT_TOKEN          common in Discord bot tutorials
        #
        # See DISCORD_TOKEN_ENV_NAMES for the authoritative list.
        #
        # Deliberately omitted: a bare TOKEN or DISCORD_SECRET. Those names are
        # generic enough to collide with an unrelated secret on a shared host,
        # and silently connecting with the wrong credential is worse than
        # failing loudly. Case-insensitivity comes from ``case_sensitive=False``,
        # so lowercase ``discord_token`` resolves as well.
        validation_alias=AliasChoices(*DISCORD_TOKEN_ENV_NAMES),
        description="Bot token issued by the Discord developer portal.",
    )
    owner_ids: set[int] = Field(
        default_factory=set,
        description="User IDs permitted to run developer-only commands.",
    )
    command_sync_mode: CommandSyncMode = Field(default="global")
    dev_guild_id: int | None = Field(
        default=None,
        description="Target guild for instant command propagation in dev mode.",
    )

    # -- Database -----------------------------------------------------------
    database_url: str = Field(
        default="sqlite+aiosqlite:///./data/zagrosian_eye.db",
        description="Async SQLAlchemy DSN. Short forms are auto-normalized.",
    )
    database_timeout: float = Field(default=5.0, ge=0.1, le=120.0)
    sqlite_pool_size: int = Field(
        default=5,
        ge=1,
        le=50,
        description="Persistent aiosqlite connections. Higher buys read "
        "concurrency; lower reduces write-lock contention.",
    )

    # -- Operations ---------------------------------------------------------
    log_level: str = Field(default="INFO")
    log_dir: Path = Field(default=Path("./logs"))
    log_max_bytes: int = Field(default=10 * 1024 * 1024, ge=4096)
    log_backup_count: int = Field(default=5, ge=1, le=100)
    cog_directory: str = Field(default="cogs")
    #: Capacity of the live log ring buffer that ``/status`` tails. The legacy
    #: ``DASHBOARD_LOG_BUFFER`` name stays accepted so old deployments keep
    #: working after the web tier was removed.
    log_buffer: int = Field(
        default=400,
        ge=25,
        le=5000,
        validation_alias=AliasChoices("LOG_BUFFER", "DASHBOARD_LOG_BUFFER"),
    )

    # -- Moderation defaults ------------------------------------------------
    max_purge_amount: int = Field(default=100, ge=2, le=5000)
    case_prefix: str = Field(default="ZEYE", min_length=2, max_length=8)
    default_mute_role: str = Field(default="Muted", min_length=1, max_length=64)
    dm_members_on_punish: bool = Field(default=True)

    # ------------------------------------------------------------------ #
    # Validation
    # ------------------------------------------------------------------ #
    @model_validator(mode="before")
    @classmethod
    def _prefer_non_empty_token(cls, data: Any) -> Any:
        """Let the first *non-empty* candidate win, not merely the first match.

        ``AliasChoices`` stops at the first key that exists, even when its value
        is an empty string — and an empty value is exactly what a PaaS dashboard
        leaves behind after a secret is cleared, or what ``DISCORD_BOT_TOKEN=``
        in a committed ``.env`` produces. In that situation the empty primary
        silently shadows a perfectly good ``DISCORD_TOKEN`` and the bot dies at
        startup claiming no token was found, which is a confusing way to learn
        that a fallback was needed at all.

        Two details of ``mode="before"`` on a ``BaseSettings`` model drive the
        implementation. The mapping is keyed by *alias*, not field name — the
        alias shadows the field name entirely because ``populate_by_name`` is
        off, so writing ``discord_bot_token`` here is silently ignored. And
        ``AliasChoices`` has already stopped at the first key that existed, so
        the fallback values are absent from the mapping altogether. Hence
        ``os.environ`` is scanned directly, and the winner is written back under
        the primary alias.
        """
        if not isinstance(data, dict):
            return data

        def _text(value: Any) -> str:
            if isinstance(value, SecretStr):
                return value.get_secret_value().strip()
            return value.strip() if isinstance(value, str) else ""

        # An explicit non-empty constructor value outranks everything below.
        for key in ("discord_bot_token", *DISCORD_TOKEN_ENV_NAMES):
            if key in data and _text(data[key]):
                return data

        chosen = next(
            (
                os.environ[name].strip()
                for name in DISCORD_TOKEN_ENV_NAMES
                if (os.environ.get(name) or "").strip()
            ),
            "",
        )
        if not chosen:
            return data

        data = dict(data)
        # Keyed by alias on purpose; see the docstring above.
        data[DISCORD_TOKEN_ENV_NAMES[0]] = SecretStr(chosen)
        return data

    @field_validator("discord_bot_token", mode="after")
    @classmethod
    def _strip_token(cls, value: SecretStr) -> SecretStr:
        """Whitespace is never valid in a token and usually means a bad copy/paste."""
        return SecretStr(value.get_secret_value().strip())

    @field_validator("owner_ids", mode="before")
    @classmethod
    def _parse_owner_ids(cls, value: Any) -> Any:
        """Accept ``1,2``, ``[1, 2]``, ``1 2`` or a real collection.

        pydantic-settings attempts a JSON decode before validators run, so a
        bare ``OWNER_IDS=123`` arrives here as an ``int`` and a comma list
        arrives as a string. Normalising all of those to a set keeps the
        documented ``OWNER_IDS=1,2,3`` form working.
        """
        if value is None or value == "":
            return set()
        if isinstance(value, (set, frozenset, list, tuple)):
            return {int(item) for item in value}
        if isinstance(value, bool):
            raise ValueError("OWNER_IDS must be user IDs, not a boolean")
        if isinstance(value, int):
            return {value}
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return set()
            if text.startswith("[") and text.endswith("]"):
                try:
                    return {int(item) for item in json.loads(text)}
                except (ValueError, TypeError):
                    pass
            parts = [p.strip().strip("\"'") for p in re.split(r"[,\s]+", text)]
            ids = {int(p) for p in parts if p and p.lstrip("-").isdigit()}
            if not ids:
                raise ValueError(
                    f"OWNER_IDS must be a comma-separated list of Discord user IDs, got {value!r}"
                )
            return ids
        return value

    @field_validator("dev_guild_id", mode="before")
    @classmethod
    def _blank_dev_guild(cls, value: Any) -> Any:
        """Treat an empty ``DEV_GUILD_ID=`` as "no dev guild", not an error."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("log_dir", mode="after")
    @classmethod
    def _anchor_log_dir(cls, value: Path) -> Path:
        return value if value.is_absolute() else (PROJECT_ROOT / value).resolve()

    @field_validator("log_level", mode="after")
    @classmethod
    def _normalize_log_level(cls, value: str) -> str:
        level = value.strip().upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(
                f"LOG_LEVEL must be one of DEBUG/INFO/WARNING/ERROR/CRITICAL, got {value!r}"
            )
        return level

    @field_validator("cog_directory", mode="after")
    @classmethod
    def _validate_cog_directory(cls, value: str) -> str:
        cleaned = value.strip().strip("./\\")
        if not cleaned or Path(cleaned).is_absolute() or ".." in Path(cleaned).parts:
            raise ValueError("COG_DIRECTORY must be a relative path inside the project")
        return cleaned

    @field_validator("case_prefix", mode="after")
    @classmethod
    def _normalize_prefix(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("database_url", mode="after")
    @classmethod
    def _normalize_dsn(cls, value: str) -> str:
        """Rewrite driverless/legacy DSNs to their async equivalents.

        Discord bots are I/O bound, so a blocking driver would stall the entire
        gateway. Every supported backend is forced onto its asyncio variant.
        """
        dsn = value.strip()
        if not dsn:
            raise ValueError("DATABASE_URL must not be empty")

        scheme, separator, remainder = dsn.partition("://")
        if not separator:
            raise ValueError(
                "DATABASE_URL is malformed: expected 'dialect+driver://...' form"
            )
        scheme = scheme.lower()
        already_async = "+" in scheme

        if scheme in {"postgres", "postgresql"}:
            scheme = "postgresql+asyncpg"
        elif scheme in {"sqlite", "sqlite3", "sqlite+aiosqlite"}:
            # Every SQLite spelling goes through the same path fixup, including
            # the already-async form. Skipping "sqlite+aiosqlite" here is what
            # let a three-slash DSN reach the engine as an absolute /data path
            # and crash with PermissionError on a read-only Linux host.
            scheme = "sqlite+aiosqlite"
            remainder = _normalize_sqlite_target(remainder)
        elif scheme in {"mysql", "mariadb"}:
            scheme = "mysql+asyncmy"
        elif scheme in {"postgresql+psycopg2", "psycopg2"}:
            scheme = "postgresql+asyncpg"
        elif scheme == "mysql+pymysql":
            scheme = "mysql+asyncmy"

        if not already_async and "+" not in scheme and scheme not in {
            "sqlite+aiosqlite",
        }:
            raise ValueError(
                f"DATABASE_URL dialect {scheme!r} has no configured async driver; "
                "use postgresql+asyncpg, mysql+asyncmy or sqlite+aiosqlite"
            )

        return f"{scheme}://{remainder}"

    @model_validator(mode="after")
    def _cross_field_rules(self) -> Settings:
        if self.command_sync_mode == "guild" and self.dev_guild_id is None:
            raise ValueError(
                "COMMAND_SYNC_MODE=guild requires DEV_GUILD_ID to be set so global "
                "commands are never wiped by a dev-only sync"
            )
        return self

    # ------------------------------------------------------------------ #
    # Derived values
    # ------------------------------------------------------------------ #
    @property
    def bot_token(self) -> str:
        """The raw token. Raises rather than handing back an empty string.

        The message lists every accepted variable name, because "token is not
        set" on a PaaS dashboard is nearly always a *naming* problem: the secret
        exists under a key the bot does not read. Naming the alternatives turns
        a startup crash into a one-glance fix.
        """
        token = self.discord_bot_token.get_secret_value()
        if not token:
            raise ConfigurationError(_missing_token_message())
        return token

    @property
    def token_is_well_formed(self) -> bool:
        """Shape check only — a malformed token is worth a warning, not a crash."""
        token = self.discord_bot_token.get_secret_value()
        return bool(token) and _TOKEN_SHAPE.match(token) is not None

    @property
    def token_fingerprint(self) -> str:
        """Stable 8-char digest, so two deployments can be compared safely."""
        token = self.discord_bot_token.get_secret_value()
        if not token:
            return "unset"
        return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]

    @property
    def database_dialect(self) -> str:
        return self.database_url.split("://", 1)[0]

    @property
    def uses_sqlite(self) -> bool:
        return self.database_dialect.startswith("sqlite")

    @property
    def syncs_globally(self) -> bool:
        return self.command_sync_mode == "global"

    @property
    def cog_package(self) -> str:
        return self.cog_directory.replace("\\", "/").replace("/", ".")

    @property
    def cog_path(self) -> Path:
        return PROJECT_ROOT / self.cog_directory

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #
    def safe_summary(self) -> dict[str, Any]:
        """Redacted projection consumed by ``/test`` and ``/status``.

        No method on this class may leak a secret into a string that a user can
        read. Lengths and digests are safe; the values themselves are not.
        """
        token = self.discord_bot_token.get_secret_value()
        return {
            "discord_bot_token": {
                "status": "set" if token else "missing",
                "length": len(token),
                "fingerprint": self.token_fingerprint,
                "well_formed": self.token_is_well_formed,
            },
            "database_url": {
                "dialect": self.database_dialect,
                # Strip credentials and path: host + engine class is all a UI needs.
                "endpoint": self._redacted_dsn(),
                "timeout_seconds": self.database_timeout,
            },
            "command_sync_mode": self.command_sync_mode,
            "dev_guild_id": self.dev_guild_id,
            "owner_ids": sorted(self.owner_ids),
            "log_level": self.log_level,
            "log_dir": str(self.log_dir),
            "max_purge_amount": self.max_purge_amount,
            "case_prefix": self.case_prefix,
            "default_mute_role": self.default_mute_role,
        }

    def _redacted_dsn(self) -> str:
        scheme, _, remainder = self.database_url.partition("://")
        if self.uses_sqlite:
            return f"{scheme}://{remainder}"
        # postgresql+asyncpg://user:secret@host:5432/db -> ...@host:5432/db
        if "@" in remainder:
            _, _, location = remainder.rpartition("@")
            return f"{scheme}://***@{location}"
        return f"{scheme}://***"

    def validate_runtime(self) -> list[str]:
        """Prepare filesystem prerequisites. Returns human-readable warnings.

        Called once during boot. Raises :class:`ConfigurationError` for
        conditions that make the process unable to run at all; returns a list
        for conditions worth a loud warning but survivable (e.g. a weak token).
        """
        warnings: list[str] = []

        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConfigurationError(
                f"LOG_DIR {self.log_dir} is not creatable: {exc}"
            ) from exc

        if not os.access(self.log_dir, os.W_OK):
            raise ConfigurationError(f"LOG_DIR {self.log_dir} is not writable")

        if self.command_sync_mode == "guild":
            warnings.append(
                "COMMAND_SYNC_MODE=guild propagates instantly and is development-only; "
                "global slash commands can take up to an hour to appear"
            )
        if self.uses_sqlite:
            warnings.append(
                "SQLite serializes writers; move to PostgreSQL for multi-guild production"
            )
        # Read the raw value: ``bot_token`` deliberately raises when unset, and
        # "no token configured" is a warning here, not a crash.
        if self.discord_bot_token.get_secret_value() and not self.token_is_well_formed:
            warnings.append(
                "DISCORD_BOT_TOKEN contains characters Discord tokens never use "
                "(spaces, quotes); verify the copy/paste"
            )
        if not self.owner_ids:
            warnings.append(
                "OWNER_IDS is empty: /test, /reload and /sync will be unusable"
            )
        if not self.discord_bot_token.get_secret_value():
            warnings.append(
                "DISCORD_BOT_TOKEN is not set: the bot cannot connect to Discord yet"
            )
        return warnings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so the ``.env`` file is parsed once. Use
    :func:`reset_settings_cache` in tests that mutate the environment.
    """
    return Settings()


def reset_settings_cache() -> None:
    """Drop the cached settings instance (test helper)."""
    get_settings.cache_clear()
