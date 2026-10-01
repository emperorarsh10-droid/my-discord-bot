"""Error taxonomy and the global failure handler.

Contract
--------
**The bot never dies because a command failed.** Every failure path in this file
follows the same three steps:

1.  Write the *full* traceback to the log (stderr + ``errors.log`` + the
    in-memory ring) at the correct severity.
2.  Record a one-line summary on :data:`runtime_state` so ``/status`` can
    raise an alarm.
3.  Tell the user *something*, in private, without ever raising from the
    handler — including when the interaction has already expired.

``register_global_handlers`` installs this for both slash commands
(``CommandTree``) and the low-level client events (``bot.on_error``).
"""

from __future__ import annotations

import asyncio
import traceback
from typing import Any, Final

import discord
from discord import app_commands

from core.dashboard_state import runtime_state
from core.embeds import COLOR_ERROR, COLOR_NEUTRAL, error_embed, truncate
from core.logging_setup import get_logger

__all__ = [
    "CommandCooldownError",
    "ConfigurationError",
    "DatabaseUnavailableError",
    "HierarchyError",
    "MissingTargetError",
    "PermissionDeniedError",
    "ZagrosError",
    "handle_app_command_error",
    "handle_client_error",
    "register_global_handlers",
    "send_error_response",
]

logger = get_logger("zagrosian.errors")

#: Flavour text shown when a command dies in a way the user cannot influence.
APOLOGY: Final[str] = "The eye dimmed for a moment. The fault has been logged."

#: How long we are willing to wait on a response before giving up on the user.
_RESPONSE_TIMEOUT: Final[float] = 8.0


# --------------------------------------------------------------------------- #
# Exception taxonomy
# --------------------------------------------------------------------------- #
class ZagrosError(Exception):
    """Base class for every error this bot raises deliberately.

    Carries the message safe to show a moderator, so handlers never have to
    translate internal failures into user-facing text themselves.
    """

    #: Short heading rendered above the body of the error embed.
    title: str = "Something went wrong"

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


class ConfigurationError(ZagrosError):
    title = "Configuration problem"


class CommandCooldownError(ZagrosError):
    title = "Slow down"


class MissingTargetError(ZagrosError):
    title = "Target not found"


class PermissionDeniedError(ZagrosError):
    title = "Permission denied"


class HierarchyError(ZagrosError):
    title = "Role hierarchy violation"


class DatabaseUnavailableError(ZagrosError):
    title = "Database unavailable"


# --------------------------------------------------------------------------- #
# discord.py error -> user message
# --------------------------------------------------------------------------- #
def _describe(error: app_commands.AppCommandError) -> tuple[str, str, int]:
    """Map an ``AppCommandError`` onto ``(title, message, colour)``."""
    if isinstance(error, ZagrosError):
        return error.title, error.user_message, COLOR_ERROR

    if isinstance(error, app_commands.CommandOnCooldown):
        retry = getattr(error, "retry_after", None)
        wait = f"Try again in **{retry:.1f}s**." if retry else "Try again shortly."
        return (
            CommandCooldownError.title,
            f"This command is on cooldown. {wait}",
            COLOR_NEUTRAL,
        )

    if isinstance(error, app_commands.MissingPermissions):
        missing = _format_missing_permissions(getattr(error, "missing_permissions", []))
        return (
            PermissionDeniedError.title,
            f"You need the following permission(s): {missing}.",
            COLOR_ERROR,
        )

    if isinstance(error, app_commands.BotMissingPermissions):
        missing = _format_missing_permissions(getattr(error, "missing_permissions", []))
        return (
            PermissionDeniedError.title,
            f"I am missing the permission(s): {missing}. "
            "Grant them to me and invite me again if that is not enough.",
            COLOR_ERROR,
        )

    if isinstance(error, app_commands.MissingAnyRole):
        return (
            PermissionDeniedError.title,
            "You do not hold any of the roles required to use this command.",
            COLOR_ERROR,
        )

    if isinstance(error, app_commands.NoPrivateMessage):
        return (
            "Guild only",
            "This command cannot be used in DMs.",
            COLOR_ERROR,
        )

    if isinstance(error, app_commands.TransformerError):
        value = getattr(error, "value", "that value")
        return (
            "Invalid input",
            f"I could not read `{value}`. "
            f"{truncate(str(error), 500)}",
            COLOR_ERROR,
        )

    if isinstance(error, app_commands.CheckFailure):
        return (
            PermissionDeniedError.title,
            "You are not allowed to use this command here.",
            COLOR_ERROR,
        )

    if isinstance(error, app_commands.CommandSignatureMismatch):
        return (
            "Command out of date",
            "This command was re-registered while it was running. Please run it "
            "again — the new version is the one that counts.",
            COLOR_ERROR,
        )

    if isinstance(error, app_commands.CommandSyncFailure):
        failures = getattr(error, "failed_commands", []) or []
        detail = "; ".join(
            f"`{name}` ({len(children)} child command(s) failed)"
            for name, children in failures
        )
        return (
            "Sync failure",
            f"The command tree could not be published: {detail or 'unknown cause'}.",
            COLOR_ERROR,
        )

    if isinstance(error, app_commands.CommandInvokeError):
        # The inner exception is logged with full traceback; the user gets a
        # generic apology so internal details never leak.
        return ("Command failed", APOLOGY, COLOR_ERROR)

    return ("Command failed", APOLOGY, COLOR_ERROR)


def _format_missing_permissions(permissions: Any) -> str:
    if not permissions:
        return "the required permissions"
    if isinstance(permissions, (str, discord.Permissions)):
        value = str(permissions).replace("|", ", ").replace("Permissions.", "")
        return truncate(value, 400)
    try:
        names = [getattr(p, "name", str(p)) for p in permissions]
    except TypeError:
        names = [str(permissions)]
    return truncate(", ".join(f"`{n}`" for n in names) or "the required permissions", 400)


# --------------------------------------------------------------------------- #
# Response plumbing
# --------------------------------------------------------------------------- #
async def send_error_response(
    interaction: discord.Interaction,
    *,
    title: str,
    message: str,
    colour: int = COLOR_ERROR,
) -> None:
    """Deliver an error to the user, degrading gracefully at every step.

    Order of preference:
      1. ``interaction.response.send_message(ephemeral=True)``
      2. ``interaction.followup.send(...)`` if already deferred
      3. a plain DM if the channel rejects the bot (for example, missing
         ``View Channel`` or ``Send Messages``)

    Any failure along the way is logged at DEBUG and swallowed: reporting a
    failure must never itself become a failure.
    """
    embed = error_embed(message, title=title)
    embed.colour = colour

    try:
        if interaction.response.is_done():
            await asyncio.wait_for(
                interaction.followup.send(embed=embed, ephemeral=True),
                timeout=_RESPONSE_TIMEOUT,
            )
            return

        await asyncio.wait_for(
            interaction.response.send_message(embed=embed, ephemeral=True),
            timeout=_RESPONSE_TIMEOUT,
        )
        return
    except discord.HTTPException as exc:
        logger.debug("Direct error response failed (%s); falling back to DM", exc)
    except TimeoutError:
        logger.debug("Error response timed out; falling back to DM")
    except Exception as exc:  # noqa: BLE001 - last-resort guard
        logger.debug("Unexpected failure while responding to an error: %s", exc)

    # Fallback: direct message. Works even when the channel is unavailable.
    try:
        await asyncio.wait_for(
            interaction.user.send(embed=embed), timeout=_RESPONSE_TIMEOUT
        )
    except (TimeoutError, discord.Forbidden, discord.HTTPException) as exc:
        logger.warning(
            "Could not deliver error notice to user %s: %s", interaction.user.id, exc
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Unexpected DM failure to user %s: %s", interaction.user.id, exc)


# --------------------------------------------------------------------------- #
# Global handlers
# --------------------------------------------------------------------------- #
async def handle_app_command_error(
    bot: discord.Client,
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
    /,
) -> None:
    """``CommandTree.on_error`` implementation."""
    command_name = _command_path(interaction)

    # Deliberate errors already carry a user-safe message and do not warrant a
    # traceback in the error log — that would bury real bugs under noise.
    cause = error.__cause__
    if isinstance(error, ZagrosError):
        logger.info("Command %s rejected: %s", command_name, error.user_message)
    elif isinstance(error, app_commands.CommandInvokeError) and isinstance(
        cause, ZagrosError
    ):
        logger.info("Command %s rejected: %s", command_name, cause.user_message)
        error = cause
    else:
        logger.error(
            "Unhandled error in command %s\n%s",
            command_name,
            "".join(traceback.format_exception(error)),
            exc_info=(type(error), error, error.__traceback__),
        )

    runtime_state.record_error(f"{command_name}: {_short(error)}", error)

    title, message, colour = _describe(error)
    await send_error_response(
        interaction, title=title, message=message, colour=colour
    )

    if isinstance(error, (app_commands.CommandOnCooldown,)):
        logger.debug("Cooldown triggered for %s", command_name)


async def handle_client_error(
    bot: discord.Client,
    event: str,
    *args: Any,
    **kwargs: Any,
) -> None:
    """``bot.on_error`` implementation for non-command events."""
    if args and isinstance(args[0], BaseException):
        exc = args[0]
        logger.error(
            "Unhandled error in event %s\n%s",
            event,
            "".join(traceback.format_exception(exc)),
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        runtime_state.record_error(f"event:{event}", _short(exc))
        return
    logger.error("Unhandled error in event %s (no exception supplied)", event)


def register_global_handlers(bot: discord.Client) -> None:
    """Attach the handlers above to a client instance."""
    bot.on_error = handle_client_error  # type: ignore[method-assign]
    bot.tree.on_error = handle_app_command_error  # type: ignore[assignment]

    # A crashed background task must be visible, not silent.
    @bot.listen()
    async def _on_task_exception(event: Any) -> None:  # pragma: no cover - runtime path
        logger.error(
            "Background task %s crashed: %r", getattr(event, "__class__", event), event
        )
        runtime_state.record_error("background_task", repr(event))


def _command_path(interaction: discord.Interaction) -> str:
    """``/ban user:@x`` — full command path including subcommands."""
    data = interaction.data
    if not isinstance(data, dict):
        return "unknown"
    name = data.get("name", "unknown")
    options = data.get("options") or []
    parts = [str(name)]
    for option in options:
        if isinstance(option, dict) and option.get("type") in (1, 2):  # subcommand group
            parts.append(str(option.get("name", "?")))
    return "/" + " ".join(parts)


def _short(error: BaseException) -> str:
    message = str(error).strip()
    return f"{type(error).__name__}: {message}" if message else type(error).__name__
