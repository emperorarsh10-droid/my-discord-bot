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
        # ``DISCORD_TOKEN`` is the shorter name many hosting dashboards default
        # to; both resolve to the same field.
        validation_alias=AliasChoices("DISCORD_BOT_TOKEN", "DISCORD_TOKEN"),
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
        elif scheme in {"sqlite", "sqlite3"}:
            # A relative sqlite path is resolved against the project root so the
            # database lands in ./data no matter the launch directory.
            if remainder.startswith("/") and not remainder.startswith("//"):
                tail = remainder.lstrip("/")
                if not tail or tail.startswith("./"):
                    tail = tail.lstrip("./") or "data/zagrosian_eye.db"
                remainder = f"./{tail}"
            scheme = "sqlite+aiosqlite"
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
        """The raw token. Raises rather than handing back an empty string."""
        token = self.discord_bot_token.get_secret_value()
        if not token:
            raise ConfigurationError(
                "DISCORD_BOT_TOKEN is not set. Copy .env.example to .env and fill it in."
            )
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
