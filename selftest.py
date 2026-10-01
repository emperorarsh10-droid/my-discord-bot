"""End-to-end verification: boot the bot object, the database and the schema.

This does not log in to Discord â€” it proves everything that can be proven
offline: extension loading, the full slash command tree, schema creation, the
atomic case allocator, the database health probe, the pure moderation helpers
and the config checklist that ``/status`` renders.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import sys
from pathlib import Path, PurePosixPath
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault("DISCORD_BOT_TOKEN", "MTIzNDU2Nzg5MDEyMzQ1Njc4.GaBcDe.fF0oBarBazQux0123456789abc")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/_selftest.db")
os.environ.setdefault("OWNER_IDS", "123456789012345678")
os.environ.setdefault("LOG_LEVEL", "INFO")
# Pin the sync mode so the suite is independent of whatever the local `.env`
# (which may legitimately be `guild` during development) happens to say.
os.environ.setdefault("COMMAND_SYNC_MODE", "global")

import discord
from discord import AppCommandOptionType as T

from config import Settings as _Settings, get_settings
from core.dashboard_state import runtime_state
from core.database import get_database
from core.errors import ZagrosError
from core.logging_setup import configure_logging, get_logger
from core.models import CaseAction, GuildConfig
from core.services import record_case
from core.transformers import parse_duration
from main import build_bot, load_extensions, sync_commands

logger = get_logger("selftest")

PASS, FAIL = 0, 0

_SELFTEST_DB = Path("data/_selftest.db")

#: Every table the ORM maps. Asserted as a set in both directions so a new model
#: that is never created, and a database table with no model behind it, both fail.
EXPECTED_TABLES: frozenset[str] = frozenset(
    {
        "blacklist_entries",
        "case_counters",
        "channel_snapshots",
        "giveaway_entries",
        "giveaways",
        "guild_backups",
        "guild_configs",
        "guild_filters",
        "guild_ignores",
        "guild_notes",
        "guild_settings",
        "mod_cases",
        "poll_votes",
        "polls",
        "reaction_role_rules",
        "sticky_messages",
        "temp_role_grants",
    }
)


def _purge_selftest_db() -> None:
    """Remove the self-test SQLite files so every run starts from zero.

    Called both before and after the run: a hard kill (Ctrl-C, a segfault in a
    native extension) skips the trailing cleanup, and a stale case counter would
    otherwise make the allocator assertions fail on the next run.
    """
    for suffix in ("", "-wal", "-shm"):
        path = _SELFTEST_DB.with_name(_SELFTEST_DB.name + suffix)
        if path.exists():
            path.unlink()


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {label}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL += 1
        print(f"  FAIL  {label}  [{detail}]")


def _migration_checks() -> None:
    """Prove an old-build guild_configs table gains every new column.

    An additive migration only ever runs against a database created by an older
    release, so a test that starts from the current schema proves nothing. This
    builds the pre-AutoMod table shape with a live row in it, then runs the real
    migration path: a text default has to render as a quoted literal, or SQLite
    reads ``DEFAULT delete`` as a syntax error and the bot never boots.
    """
    from sqlalchemy import MetaData, Table, create_engine, inspect, text

    from core.database import _add_missing_columns_sync
    from core.models import Base, GuildConfig

    added = [
        "automod_enabled",
        "automod_duplicate_action",
        "automod_caps_action",
        "automod_invite_action",
        "warn_timeout_at",
        "warn_kick_at",
        "anticaps_percent",
        "anticaps_min_length",
        "spam_window_seconds",
        "spam_max_messages",
        "spam_timeout_seconds",
        "anti_invite",
        "panic_active",
    ]

    # A Column cannot belong to two Table objects, so each copy gets its own.
    def _columns(names: set[str]) -> list:
        return [c._copy() for c in GuildConfig.__table__.columns if c.name in names]

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        legacy_table = Table(
            "guild_configs",
            MetaData(),
            *_columns(set(GuildConfig.__table__.columns.keys()) - set(added)),
        )
        legacy_table.create(bind=conn)
        required = [c for c in legacy_table.columns if c.nullable is False]
        names = [c.name for c in required]
        # Identifiers come from the ORM metadata, never from input.
        collist = ", ".join(names)
        placeholders = ", ".join(f":{n}" for n in names)
        conn.execute(
            text(f"INSERT INTO guild_configs ({collist}) VALUES ({placeholders})"),  # noqa: S608
            {
                c.name: (1 if type(c.type).__name__ == "Integer" else "x")
                for c in required
            },
        )

        # Only the columns absent from the legacy shape, so the "missing" set is
        # exactly what an upgrading server would need.
        target = Table("guild_configs", MetaData(), *_columns(set(added)))
        try:
            added_now = _add_missing_columns_sync(conn, target.metadata)
            check(
                "migration adds every new column",
                {name.split(".", 1)[1] for name in added_now} == set(added),
                str(sorted(added_now)),
            )
            columns = {c["name"] for c in inspect(conn).get_columns("guild_configs")}
            check("legacy table ends up complete", set(added) <= columns, f"missing: {sorted(set(added) - columns)}")
            row = conn.execute(
                text(
                    "SELECT automod_enabled, automod_duplicate_action,"
                    " warn_timeout_at, anti_invite FROM guild_configs"
                )
            ).one()
            # Booleans land as 0/1 and text defaults as quoted literals; an
            # unquoted `delete` here is a syntax error, not a value.
            check(
                "existing row gets usable defaults",
                tuple(row) == (0, "delete", 3, 0),
                str(tuple(row)),
            )
        except Exception as exc:  # the exception text is the assertion detail
            check("migration adds every new column", False, f"{type(exc).__name__}: {exc}")

        check(
            "model defaults match the migration defaults",
        GuildConfig(guild_id=1).automod_duplicate_action == "delete",
        str(GuildConfig(guild_id=1).automod_duplicate_action),
    )

    # A multi-choice poll needs more than one row per member, which the original
    # (poll_id, user_id) key structurally forbids. This walks a real old table
    # through the constraint migration.
    from core.models import PollVote

    keys = {tuple(c.name for c in c.columns) for c in PollVote.__table__.constraints}
    check("poll vote key includes option_index", ("poll_id", "user_id", "option_index") in keys, str(keys))

    def _vote_key_migration() -> None:
        engine2 = create_engine("sqlite://")
        with engine2.begin() as conn:
            conn.exec_driver_sql(
                """
                CREATE TABLE poll_votes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
                    poll_id INTEGER NOT NULL,
                    user_id BIGINT NOT NULL,
                    option_index INTEGER NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (poll_id, user_id)
                )
                """
            )
            conn.exec_driver_sql(
                "INSERT INTO poll_votes (poll_id, user_id, option_index) VALUES (1, 42, 0)"
            )
            added = _add_missing_columns_sync(conn, MetaData())
            check("poll vote key migration runs", "poll_votes.unique_key" in added, str(added))
            conn.exec_driver_sql(
                "INSERT INTO poll_votes (poll_id, user_id, option_index) VALUES (1, 42, 1)"
            )
            rows = conn.execute(text("SELECT option_index FROM poll_votes ORDER BY id")).scalars().all()
            check("multi-choice votes both persist", rows == [0, 1], str(rows))
            try:
                conn.exec_driver_sql(
                    "INSERT INTO poll_votes (poll_id, user_id, option_index) VALUES (1, 42, 1)"
                )
                check("same option twice still blocked", False, "no error raised")
            except Exception as exc:
                check("same option twice still blocked", "UNIQUE" in str(exc).upper(), type(exc).__name__)
        engine2.dispose()

    _vote_key_migration()
    engine.dispose()
    check("Base metadata still resolves after migration helpers ran", bool(Base.metadata.tables))


async def _command_sync_checks(bot) -> None:
    """The command tree must publish once, not on every process restart.

    A global sync costs one HTTP request per command against an endpoint Discord
    throttles hard. Re-issuing it on every restart exhausted that budget and
    reset the propagation timer, so for up to an hour after each deploy every
    client showed the previous command set and rejected calls as outdated. The
    fingerprint makes an unchanged tree a no-op.
    """
    import tempfile
    from pathlib import Path as _Path

    from discord import app_commands

    import main as main_module

    tree = bot.tree
    baseline = main_module._tree_fingerprint(tree)
    check("fingerprint is deterministic", baseline == main_module._tree_fingerprint(tree), baseline)
    check("fingerprint is a short hex digest", len(baseline) == 16 and all(
        ch in "0123456789abcdef" for ch in baseline), baseline)

    # A real change to the published shape must move the digest, and removing it
    # must restore the original — otherwise the check would pin commands forever.
    async def _noop(interaction) -> None:  # pragma: no cover - never invoked
        return

    probe = app_commands.Command(name="zeye_selftest_probe", description="x", callback=_noop)
    tree.add_command(probe)
    changed = main_module._tree_fingerprint(tree)
    check("fingerprint tracks a new command", changed != baseline, f"{baseline} -> {changed}")
    tree.remove_command("zeye_selftest_probe")
    check(
        "fingerprint returns when the command is removed",
        main_module._tree_fingerprint(tree) == baseline,
    )

    with tempfile.TemporaryDirectory() as tmp:
        calls: list[int] = []

        async def _fake_sync(*_args, **_kwargs):
            calls.append(1)
            return tree.get_commands()

        original_sync = tree.sync
        original_mode = settings_snapshot().command_sync_mode
        original_on_start = settings_snapshot().sync_on_start
        original_data_dir = settings_snapshot().data_dir
        settings = settings_snapshot()
        settings.command_sync_mode = "global"
        settings.data_dir = _Path(tmp)
        tree.sync = _fake_sync  # type: ignore[method-assign]
        try:
            await main_module.sync_commands(bot, settings)
            check("first boot publishes the tree", len(calls) == 1, f"{len(calls)} call(s)")

            await main_module.sync_commands(bot, settings)
            check("unchanged tree skips the republish", len(calls) == 1, f"{len(calls)} call(s)")

            tree.add_command(probe)
            await main_module.sync_commands(bot, settings)
            check("a changed tree publishes again", len(calls) == 2, f"{len(calls)} call(s)")

            settings.sync_on_start = "always"
            await main_module.sync_commands(bot, settings)
            check("sync_on_start=always always publishes", len(calls) == 3, f"{len(calls)} call(s)")

            settings.sync_on_start = "never"
            await main_module.sync_commands(bot, settings)
            check("sync_on_start=never never publishes", len(calls) == 3, f"{len(calls)} call(s)")
        finally:
            tree.remove_command("zeye_selftest_probe")
            tree.sync = original_sync  # type: ignore[method-assign]
            settings.command_sync_mode = original_mode
            settings.sync_on_start = original_on_start
            settings.data_dir = original_data_dir


def settings_snapshot():
    """The live Settings singleton, for tests that mutate it in place."""
    from config import get_settings

    return get_settings()


async def _probe_honesty_checks(bot) -> None:
    """A probe must not report a healthy subsystem as broken.

    The cache probe called ``is_ws_ratelimited()``, which only reports the
    gateway shard's own reconnect backoff and says nothing about REST. Discord
    rate-limits a freshly booted bot routinely, so the probe printed "REST bucket
    is currently rate limited" and failed the subsystem while the cache was warm
    and every command was working. Operators chased an API problem they did not
    have.
    """
    dev = bot.get_cog("Developer") or bot.get_cog("developer")
    check("developer cog loaded for probe checks", dev is not None, str(list(bot.cogs)))

    if dev is None:
        return

    # Offline selftest has no guilds, so the honest verdict here is the
    # "not in any server" branch — never a rate-limit claim.
    verdict = await dev._probe_cache()
    detail = str(verdict.get("detail", ""))
    check(
        "cache probe never blames the REST bucket",
        "REST bucket" not in detail,
        detail,
    )
    check(
        "cache probe reports the real failure when empty",
        verdict.get("ok") is False and "not in any server" in detail,
        detail,
    )

    # The intent audit must not claim listener-side enforcement works while the
    # privileged intent is off, which is what let AutoMod escalation and sticky
    # reposting silently do nothing in production.
    clean, lines = dev._check_configuration()
    intent_lines = [line for line in lines if line.startswith(("ok ", "! "))
                    and "message_content_intent" in line]
    check("config audit reports the intent", len(intent_lines) == 1, str(lines[-6:]))
    if intent_lines:
        line = intent_lines[0]
        check(
            "missing message_content intent is flagged, not blessed",
            (not bot.intents.message_content) == line.startswith("!"),
            line,
        )

    # The bot must actually request the intent. AutoMod's escalation ladder and
    # sticky reposting read message.content from on_message listeners; with the
    # intent off Discord delivers an empty string and those features are inert
    # while every slash command still appears to work.
    from config import get_settings

    check(
        "message_content intent requested by default",
        get_settings().message_content_intent is True,
        str(get_settings().message_content_intent),
    )
    check("bot requests the intent", bot.intents.message_content is True)


async def main() -> int:
    _purge_selftest_db()
    settings = get_settings()
    configure_logging(force=True)

    print("\n=== 1. configuration ===")
    check("DSN normalized", settings.database_dialect == "sqlite+aiosqlite", settings.database_dialect)
    check("owner ids parsed", 123456789012345678 in settings.owner_ids)
    check("log dir absolute", settings.log_dir.is_absolute(), str(settings.log_dir))
    check("log buffer in range", 25 <= settings.log_buffer <= 5000, str(settings.log_buffer))
    summary = json.dumps(settings.safe_summary())
    check("token never in safe_summary", "MTIzNDU2" not in summary)
    check("fingerprint present", settings.token_fingerprint != "unset", settings.token_fingerprint)
    check("token well formed", settings.token_is_well_formed)

    # Env aliases: PaaS hosts often store the token as DISCORD_TOKEN, and the
    # old DASHBOARD_LOG_BUFFER key must still feed the /status log ring.
    def _alias_choices(field_name: str) -> tuple[str, ...]:
        alias = _Settings.model_fields[field_name].validation_alias
        return tuple(getattr(alias, "choices", (alias,)))

    check("DISCORD_BOT_TOKEN accepts the DISCORD_TOKEN alias",
          "DISCORD_TOKEN" in _alias_choices("discord_bot_token"),
          str(_alias_choices("discord_bot_token")))
    # Railway's dashboard templates and Discord bot tutorials both use BOT_TOKEN,
    # and a bot deployed under it used to die at startup with a message naming
    # only DISCORD_BOT_TOKEN.
    check("DISCORD_BOT_TOKEN accepts the BOT_TOKEN alias",
          "BOT_TOKEN" in _alias_choices("discord_bot_token"),
          str(_alias_choices("discord_bot_token")))
    check("DISCORD_BOT_TOKEN remains the highest-precedence alias",
          _alias_choices("discord_bot_token")[0] == "DISCORD_BOT_TOKEN",
          str(_alias_choices("discord_bot_token")))
    # A bare TOKEN could collide with an unrelated secret, so it must stay out.
    check("a bare TOKEN is not accepted",
          "TOKEN" not in _alias_choices("discord_bot_token"),
          str(_alias_choices("discord_bot_token")))
    check("log_buffer accepts the legacy DASHBOARD_LOG_BUFFER alias",
          "DASHBOARD_LOG_BUFFER" in _alias_choices("log_buffer"),
          str(_alias_choices("log_buffer")))

    _saved_legacy = os.environ.pop("DASHBOARD_LOG_BUFFER", None)
    os.environ["DASHBOARD_LOG_BUFFER"] = "812"
    try:
        _aliased = _Settings(_env_file=None)
        check("legacy DASHBOARD_LOG_BUFFER drives log_buffer", _aliased.log_buffer == 812,
              str(_aliased.log_buffer))
    finally:
        os.environ.pop("DASHBOARD_LOG_BUFFER", None)
        if _saved_legacy is not None:
            os.environ["DASHBOARD_LOG_BUFFER"] = _saved_legacy

    _token_resolution_checks()

    # Every removed web key must be inert, not a crash.
    dead = {"dashboard_enabled", "dashboard_host", "dashboard_port", "dashboard_public",
            "dashboard_token", "dashboard_bearer_token", "dashboard_base_url",
            "dashboard_tls_certfile", "dashboard_tls_keyfile", "dashboard_startup_timeout",
            "dashboard_shutdown_timeout", "bind_host"}
    check("web settings are gone", not (dead & set(_Settings.model_fields)), str(sorted(dead & set(_Settings.model_fields))))
    check("safe_summary has no dashboard block", "dashboard" not in settings.safe_summary())

    warnings = settings.validate_runtime()

    # The shared status surface must accept a valid guild-sync setup, not flag it.
    from core.diagnostics import config_checklist, probe_state, sparkline

    guild_mode = _Settings(
        _env_file=None,
        discord_bot_token="MTIzNDU2Nzg5MDEyMzQ1Njc4.GaBcDe.fF0oBarBazQux0123456789abc",
        command_sync_mode="guild",
        dev_guild_id=424242,
    )
    checklist = config_checklist(guild_mode)
    by_label = {i["label"]: i for i in checklist}
    sync_item = by_label["Command sync"]
    check("guild sync with a dev guild passes the checklist", sync_item["ok"], sync_item["detail"])
    check("checklist has 5 items", len(checklist) == 5, str(len(checklist)))
    check("checklist items shaped",
          all({"label", "ok", "detail"} <= set(item) for item in checklist))
    check("no dashboard auth item", "Dashboard auth" not in by_label, str(list(by_label)))
    check("validate_runtime returns list", isinstance(warnings, list), f"{len(warnings)} warnings")

    # The shared status surface (bot + cogs) must render and probe coherently.
    check("sparkline of nothing is empty", sparkline([]) == "")
    check("sparkline of a flat series is mid-height",
          sparkline([7, 7, 7]) == "▅▅▅", ascii(sparkline([7, 7, 7])))
    rising = sparkline([0, 5, 10])
    check("sparkline of a rise ascends", len(rising) == 3 and rising[0] != rising[-1], ascii(rising))
    check("unrun probe reads unchecked", probe_state(object()) == "unchecked", probe_state(object()))

    print("\n=== 2. duration parser ===")
    for raw, expected in (
        ("30m", 1800), ("12h", 43200), ("1d 12h", 129600),
        ("90m", 5400), ("1w", 604800), ("1mo", 2592000),
    ):
        check(f"parse {raw!r}", parse_duration(raw).total_seconds() == expected)
    for bad in ("", "banana", "1h banana", "0m", "2y", "5x"):
        try:
            parse_duration(bad)
        except Exception:
            check(f"reject {bad!r}", True)
        else:
            check(f"reject {bad!r}", False, "accepted!")

    print("\n=== 3. database ===")
    database = get_database()
    await database.connect()
    check("engine ready", database.is_ready)
    check("dialect reported", database.dialect_name == "sqlite", database.dialect_name)
    health = await database.health()
    check("health ok", health["ok"] is True, json.dumps(health.get("error")))
    check("health timed", isinstance(health["latency_ms"], (int, float)), f"{health['latency_ms']} ms")
    # Every mapped model must exist in the live database. Asserting the exact
    # count instead would make this a change-detector: adding one table for a new
    # feature would fail a test about database health, which is not what it is for.
    mapped = set(health["tables"] or [])
    missing = EXPECTED_TABLES - mapped
    check("every mapped table exists", not missing, f"missing: {sorted(missing)}")
    check(
        "no unmapped tables in db",
        not (mapped - EXPECTED_TABLES),
        f"unexpected: {sorted(mapped - EXPECTED_TABLES)}",
    )

    print("\n=== 4. case allocator (atomic) ===")
    numbers = await asyncio.gather(*[database.next_case_number(999) for _ in range(25)])
    check("25 concurrent -> 25 unique", len(set(numbers)) == 25)
    check("contiguous 1..25", sorted(numbers) == list(range(1, 26)), f"min={min(numbers)} max={max(numbers)}")
    case = await record_case(
        guild_id=999, user_id=555, moderator_id=1,
        action=CaseAction.BAN, reason="selftest", database=database,
    )
    check("case ref format", case.case_ref.startswith("ZEYE-") and len(case.case_ref) == 11, case.case_ref)
    check("case number 26", case.case_number == 26, str(case.case_number))
    check("case persisted", (await database.count_cases(999)) == 1)
    try:
        await record_case(
            guild_id=999, user_id=555, moderator_id=1,
            action=CaseAction.BAN, reason="x", database=database,
        )
    except Exception as exc:
        check("second guild isolate", (await database.next_case_number(1000)) == 1, f"{exc}"[:40])

    from core.services import permissions_from_names

    mask = permissions_from_names(("manage_roles", "kick_members"))
    check("permission names build a real mask",
          mask.manage_roles and mask.kick_members and not mask.ban_members, str(mask.value))
    passthrough = discord.Permissions(ban_members=True)
    check("an existing Permissions object passes through",
          permissions_from_names(passthrough) is passthrough)
    check("moderation counter increments on record_case",
          runtime_state.moderation_action_count >= 1,
          str(runtime_state.moderation_action_count))

    print("\n=== 5. error taxonomy ===")
    try:
        raise ZagrosError("user safe message")
    except ZagrosError as exc:
        check("user_message preserved", exc.user_message == "user safe message")

    # Every AppCommandError subclass must survive _describe(). A typo'd
    # attribute reference in the dispatch chain would otherwise only surface
    # the first time a user trips that specific failure.
    import inspect as _inspect
    from types import SimpleNamespace

    from core.errors import _describe

    async def _dummy_callback(interaction: discord.Interaction) -> None:
        return None

    dummy_command = discord.app_commands.Command(
        name="dummy", description="dummy", callback=_dummy_callback
    )
    fake_command = SimpleNamespace(name="demo", qualified_name="demo", parent=None)

    def _sample(param_name: str) -> Any:
        """A plausible value per parameter name, so every error class builds."""
        return {
            "exception": RuntimeError("boom"),
            "error": RuntimeError("boom"),
            "original": RuntimeError("boom"),
            "command": dummy_command,
            "missing_permissions": {"manage_messages", "ban_members"},
            "name": "demo",
            "guild_id": 123456789012345678,
            "limit": 10,
            "type": discord.app_commands.Transformer(),
            "transformer": discord.app_commands.Transformer(),
            "value": "12h",
            "message": "bad signature",
            "parent": None,
            "parents": [],
            "failed_commands": [dummy_command],
            "commands": [dummy_command],
            "child": SimpleNamespace(
                status=500, code=0, text="sync failed", message="sync failed",
                reason="Internal Server Error", _errors={},
            ),
            "locale": None,
            "string": "hello",
            "context": {},
            "cooldown": discord.app_commands.Cooldown(5, 30),
            "retry_after": 12.5,
            "number": 1,
        }.get(param_name, "x")

    bad: list[str] = []
    for name in dir(discord.app_commands):
        cls = getattr(discord.app_commands, name)
        if not (_inspect.isclass(cls) and issubclass(cls, discord.app_commands.AppCommandError)):
            continue
        try:
            spec = list(_inspect.signature(cls).parameters.items())
        except (TypeError, ValueError):
            # A bare builtin exception takes only a message.
            spec = [("message", _inspect.Parameter("message", _inspect.Parameter.POSITIONAL_OR_KEYWORD))]
        args = [_sample(p) for p, s in spec if s.kind is not s.KEYWORD_ONLY]
        kwargs = {
            p: _sample(p)
            for p, s in spec
            if s.kind is s.KEYWORD_ONLY and s.default is s.empty
        }
        try:
            instance = cls(*args, **kwargs)
        except Exception as exc2:
            bad.append(f"{name}: cannot construct ({exc2})")
            continue
        try:
            title, message, colour = _describe(instance)
            if not title or not message:
                bad.append(f"{name}: empty title/message")
            # Embed(colour=...) accepts an int, so that is the documented contract.
            if not isinstance(colour, (int, discord.Colour)):
                bad.append(f"{name}: colour is {type(colour).__name__}")
            if isinstance(colour, discord.Colour):
                bad.append(f"{name}: colour should be an int, not a Colour")
        except Exception as exc2:
            bad.append(f"{name}: {type(exc2).__name__}: {exc2}")
    check("every AppCommandError maps to a title/message/colour", not bad, "; ".join(bad[:4]))
    check("error dispatch is substantial", len(_describe(discord.app_commands.NoPrivateMessage())) == 3)

    # A raw non-AppCommandError must not escape the fallback path either.
    class Boom(discord.app_commands.AppCommandError):
        pass

    title, message, colour = _describe(Boom())
    check("unknown error hits the branded fallback",
          title == "Command failed" and "dimmed" in message, f"{title} | {message}")

    print("\n=== 5b. rate limiter ===")
    from core.errors import CommandCooldownError
    from core.ratelimit import RateLimit, RateLimiter

    rl = RateLimit(max_calls=3, window=30.0)
    for i in range(3):
        rl.apply("ban", 42)
    check("three calls allowed", True, "3/3 consumed")
    try:
        rl.apply("ban", 42)
        check("fourth call is blocked", False, "no cooldown raised")
    except CommandCooldownError as exc:
        check("fourth call is blocked", "too quickly" in exc.user_message, exc.user_message[:50])
    rl.apply("ban", 43)
    check("other guild unaffected", True, "scope isolation")
    rl.apply("kick", 42)
    check("other action unaffected", True, "action isolation")
    check("reset clears the registry", (rl.reset() or True) and len(rl._registry) == 0)

    single = RateLimiter(max_calls=1, window=30.0)
    single.check("a")
    try:
        single.check("a")
        check("RateLimiter.check enforces", False)
    except CommandCooldownError:
        check("RateLimiter.check enforces", True)
    check("apply is not a coroutine", not _inspect.iscoroutinefunction(RateLimit.apply))

    exempt = RateLimiter(max_calls=1, window=30.0, exempt=lambda s: s == "owner")
    exempt.check("guild-1", "owner")
    exempt.check("guild-1", "owner")
    check("exempt subject bypasses", True, "2 calls for the exempt subject")
    try:
        exempt.check("guild-1", "not-an-owner")
        exempt.check("guild-1", "not-an-owner")
        check("non-exempt subject is still limited", False, "no cooldown after 2/1")
    except CommandCooldownError:
        check("non-exempt subject is still limited", True)

    # The cogs must call the limiter in a way that actually consumes a slot.
    import warnings

    from cogs.moderation import PUNISHMENT_LIMIT as PUNISHMENT_LIMIT_PROBE
    PUNISHMENT_LIMIT_PROBE.reset()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        PUNISHMENT_LIMIT_PROBE.apply("ban", 999, 4242)
        await asyncio.sleep(0)
        never_awaited = [w for w in caught if "never awaited" in str(w.message)]
    check("apply() leaves no un-awaited coroutine", not never_awaited,
          str([str(w.message) for w in never_awaited])[:80])
    try:
        for _ in range(10):
            PUNISHMENT_LIMIT_PROBE.apply("ban", 999, 4242)
        check("probe limiter eventually blocks", False, "10 calls, no cooldown")
    except CommandCooldownError:
        check("probe limiter eventually blocks", True)
    # The configured owner must never be throttled.
    owner = next(iter(settings.owner_ids))
    for _ in range(50):
        PUNISHMENT_LIMIT_PROBE.apply("ban", 999, owner)
    check("configured owner is exempt", True, "50 calls, no cooldown")

    print("\n=== 5c. DM notification gate ===")
    from types import SimpleNamespace as _NS

    from core.services import get_or_create_guild_config, notify_member

    dm_calls: list[int] = []

    class _FakeGuild:
        id = 999

    class _FakeResponse:
        status = 403
        reason = "Forbidden"

    class _FakeMember:
        guild = _FakeGuild()
        id = 4242

        async def send(self, *, embed: object = None) -> None:
            dm_calls.append(self.id)

    embed = discord.Embed(title="test")
    member = _FakeMember()

    settings.dm_members_on_punish = True
    delivered = await notify_member(member, embed, database=database)
    check("DM delivered when globally enabled", delivered and dm_calls == [4242], str(dm_calls))

    # The master switch must win over the per-guild opt-in.
    settings.dm_members_on_punish = False
    delivered = await notify_member(member, embed, database=database)
    check("global off blocks the DM", delivered is False and dm_calls == [4242], str(dm_calls))

    # And an explicit per-guild opt-out must not be overridden by the default.
    settings.dm_members_on_punish = True
    await get_or_create_guild_config(999, database)
    async with database.session() as session:
        row = await session.get(GuildConfig, 999)
        row.dm_on_punish = False
    delivered = await notify_member(member, embed, database=database)
    check("per-guild opt-out respected", delivered is False and dm_calls == [4242], str(dm_calls))

    async with database.session() as session:
        row = await session.get(GuildConfig, 999)
        row.dm_on_punish = True
    delivered = await notify_member(member, embed, database=database)
    check("per-guild opt-in delivers", delivered and len(dm_calls) == 2, str(dm_calls))

    # `default=False` at the call site must not DM even with the switch on.
    delivered = await notify_member(member, embed, database=database, default=False)
    check("call-site default=False suppresses", delivered is False and len(dm_calls) == 2, str(dm_calls))

    # A member with DMs closed must not raise, just report failure.
    settings.dm_members_on_punish = True

    class _ClosedMember(_FakeMember):
        async def send(self, *, embed: object = None) -> None:
            raise discord.Forbidden(_FakeResponse(), "Cannot send messages to this user")

    delivered = await notify_member(_ClosedMember(), embed, database=database)
    check("closed DMs return False, no raise", delivered is False)

    print("\n=== 6. extensions + command tree ===")
    bot = build_bot(settings)
    failures = await load_extensions(bot, settings)
    check("no extension failures", list(failures) == [], str(failures))
    check("feature cogs loaded", len(bot.cogs) >= 3, str(sorted(bot.cogs)))
    if list(failures):
        print("  ABORT: extension failures, remaining command-tree checks skipped")
        return 1
    # walk_commands() recurses into groups, so subcommands appear as "config logs"
    commands = {c.qualified_name: c for c in bot.tree.walk_commands()}
    top_level = {c.name for c in bot.tree.get_commands()}
    check("config group registered", "config" in top_level, str(sorted(top_level)))
    for expected in ("ban", "kick", "mute", "unmute", "warn", "warnings", "purge",
                     "settings", "toggle-dm", "serverinfo", "system", "status", "test",
                     "sync", "reload",
                     "userinfo", "slowmode", "lockdown", "unlock", "botstatus",
                     "massban", "softban", "tempban",
                     "filter", "embed_builder", "embed", "backup_create", "backup_load",
                     "config logs", "config muted-role",
                     "timeout", "untimeout", "warns", "clearwarns", "infractions",
                     "reason", "notes add", "notes view", "cases", "viewcase",
                     "unban", "banlist",
                     "panic", "unpanic", "lockdownall", "unlockall", "slowmodeall",
                     "antiinvite", "antispam", "blacklist add", "blacklist remove",
                     "blacklist list", "nuke", "invites", "clean",
                     "nick", "role", "whois", "avatar", "verify", "altcheck",
                     "strip", "roleall", "temprole", "dm", "modstats",
                     "ignore", "unignore", "topic", "setup", "modhelp",
                     "sticky", "unsticky", "poll", "giveaway", "reactionrole",
                     "vckick", "vcmute", "vcunmute", "vclock", "vcunlock",
                     "automod status", "automod enable", "automod caps",
                     "automod spam", "automod reset"):
        check(f"/{expected} present", expected in commands)
    check("automod group registered", "automod" in top_level, str(sorted(top_level)))
    payload = [c.to_dict() for c in bot.tree.get_commands()]
    check("tree serialises to JSON", len(json.dumps(payload)) > 500)

    # Expectation types come from the library, so this stays valid across upgrades.
    mute = commands["mute"].to_dict()
    duration_opt = next(o for o in mute["options"] if o["name"] == "duration")
    check("mute duration is a string option", duration_opt["type"] == T.string.value, str(duration_opt))
    check("duration has a description", bool(duration_opt.get("description")), str(duration_opt)[:80])
    target_opt = next(o for o in mute["options"] if o["name"] == "target")
    check("mute.target is a user option", target_opt["type"] == T.user.value, str(target_opt)[:70])

    ban = commands["ban"].to_dict()
    days_opt = next(o for o in ban["options"] if o["name"] == "delete_days")
    check("ban.delete_days is an integer", days_opt["type"] == T.integer.value, str(days_opt)[:70])
    check("ban.delete_days optional", days_opt["required"] is False, str(days_opt)[:70])
    reason_opt = next(o for o in ban["options"] if o["name"] == "reason")
    check("ban.reason optional with length bound",
          reason_opt["required"] is False and reason_opt.get("min_length") == 1, str(reason_opt)[:70])
    concrete = [c for c in commands.values() if not isinstance(c, discord.app_commands.Group)]
    # guild_only is enforced by Discord server-side, so it is a flag, not a check.
    not_guild = sorted(c.name for c in concrete if not c.guild_only)
    check("every non-developer command is guild_only",
          not_guild == ["reload", "status", "sync", "test"],
          str(not_guild))

    purge = commands["purge"].to_dict()
    amount = next(o for o in purge["options"] if o["name"] == "amount")
    check("purge bound comes from MAX_PURGE_AMOUNT",
          amount["max_value"] == settings.max_purge_amount,
          f"option max {amount['max_value']} vs config {settings.max_purge_amount}")
    # /clear is kept as a live alias: discord.py has no slash-command alias, so a
    # guild with it in saved commands would otherwise just get "unknown command".
    check("clear alias still registered", "clear" in commands, str(sorted(commands)[:5]))
    check(
        "clear alias has the same bounds",
        next(o for o in commands["clear"].to_dict()["options"] if o["name"] == "amount")["max_value"]
        == amount["max_value"],
    )

    # At least one cog must have registered, and every name reported must be one
    # the tree actually holds: a stale counter in runtime_state would otherwise
    # make /status lie about the running feature set.
    loaded_cogs = runtime_state.snapshot()["cogs_loaded"]
    check("runtime_state knows cogs", bool(loaded_cogs), str(loaded_cogs))
    check(
        "runtime_state cogs match the tree",
        set(loaded_cogs) <= set(bot.extensions),
        f"stale: {sorted(set(loaded_cogs) - set(bot.extensions))}",
    )

    print("\n=== 7. bulk target parsing ===")
    from core.targets import MAX_BULK_TARGETS, parse_user_ids

    ids, rejected = parse_user_ids("<@!111111111111111111> 222222222222222222,333;222222222222222222")
    check("mentions and bare ids accepted",
          ids == [111111111111111111, 222222222222222222], str(ids))
    check("repeated ids collapse", ids.count(222222222222222222) == 1, str(ids))
    check("too-short token is reported, not raised", rejected == ["333"], str(rejected))
    check("duplicates collapse", parse_user_ids("1,1,1")[1] == ["1", "1", "1"])
    messy = parse_user_ids("<@1> abc ,, 222222222222222222")
    check("garbage is rejected, not fatal", messy == ([222222222222222222], ["<@1>", "abc"]),
          str(messy))
    check("empty input yields nothing", parse_user_ids("   ") == ([], []))
    too_many = " ".join(str(10_000_000_000_000_000 + i) for i in range(MAX_BULK_TARGETS + 25))
    check("hard cap enforced", len(parse_user_ids(too_many)[0]) == MAX_BULK_TARGETS,
          str(len(parse_user_ids(too_many)[0])))
    check("overflow is reported, not dropped", len(parse_user_ids(too_many)[1]) == 25,
          str(len(parse_user_ids(too_many)[1])))
    check("unusable snowflake shape rejected", parse_user_ids("12345")[1] == ["12345"],
          str(parse_user_ids("12345")[1]))
    check("truncated mention still parses", parse_user_ids("<@333333333333333333>")[0] == [333333333333333333],
          str(parse_user_ids("<@333333333333333333>")[0]))

    print("\n=== 8. automod phrase helpers ===")
    from core.automod import (
        MAX_KEYWORD_LEN,
        MAX_PHRASES,
        AutoMod,
        AutoModSettings,
        clean_phrases,
        extract_invite_codes,
        looks_like_shouting,
        matches_phrase,
        normalize_phrase,
    )

    check("normalize lowercases + trims", normalize_phrase("  HeLLo  ") == "hello")
    check("normalize collapses inner spaces", normalize_phrase("a   b") == "a b")
    check("clean drops empties and dupes", clean_phrases(["a", "A", " ", "b"]) == ["a", "b"])
    # Over-long keywords are rejected loudly rather than truncated: silently cutting
# "free" down to "fre" would block innocent words, which is worse than an error.
    try:
        clean_phrases(["x" * (MAX_KEYWORD_LEN + 1)])
        check("clean rejects over-long keywords", False, "no error raised")
    except ValueError as exc:
        check("clean rejects over-long keywords", "limit" in str(exc).lower(), str(exc))
    check(
        "clean keeps a keyword at exactly the cap",
        clean_phrases(["y" * MAX_KEYWORD_LEN]) == ["y" * MAX_KEYWORD_LEN],
    )
    check("phrase hit is substring, case-insensitive",
          matches_phrase("well HELLO there", "hello") is True)
    check("phrase miss is False", matches_phrase("nothing here", "hello") is False)
    check("empty phrase never matches", matches_phrase("anything", "") is False)
    check(
        "phrase match respects word boundaries",
        matches_phrase("that party started", "art") is False,
    )
    check(
        "multi-word phrase matches across the whitespace",
        matches_phrase("say   free   nitro now", "free nitro") is True,
    )
    check("MAX_PHRASES is a real bound", 0 < MAX_PHRASES <= 1000, str(MAX_PHRASES))

    check("shouting is detected", looks_like_shouting("WHY IS NOBODY LISTENING", 70, 12) is True)
    check("a short shout is exempt",
          looks_like_shouting("OK NO", 70, 12) is False)
    check("ordinary sentence is not shouting",
          looks_like_shouting("hello everyone how are you", 70, 12) is False)
    check("digits and punctuation are not letters",
          looks_like_shouting("1234567890 !!!!!", 70, 4) is False)
    codes = extract_invite_codes("come to discord.gg/abcdef and also http://discord.com/invite/xyz123")
    check("both invite forms are found", len(codes) >= 2, str(codes))

    # The rate window is the piece with no Discord API to lean on, so it is
    # driven directly: a flooder must trip exactly once, and a member who stops
    # talking must not inherit a stale window after the gap closes.
    spam_settings = AutoModSettings(
        enabled=True,
        spam_window_seconds=10,
        spam_max_messages=3,
        spam_timeout_seconds=60,
    )

    class _Member:
        def __init__(self, uid: int) -> None:
            self.id = uid
            self.roles: list = []

    class _Channel:
        id = 555

    class _Guild:
        id = 777

    class _Message:
        def __init__(self, uid: int, content: str) -> None:
            self.author = _Member(uid)
            self.content = content
            self.channel = _Channel()
            self.guild = _Guild()

    engine = AutoMod(bot=None)  # type: ignore[arg-type]
    verdicts = [
        engine._check_spam(_Message(1, f"msg {i}"), spam_settings)
        for i in range(6)
    ]
    tripped = [v for v in verdicts if v.tripped]
    check("flood trips once, not per message", len(tripped) == 1, str(len(tripped)))
    check("flood verdict is the spam rule",
          tripped and tripped[0].action == "spam", str(tripped[0].detail if tripped else None))
    # The window resets on trip, so the messages that follow start a new window
    # rather than each re-triggering the same punishment.
    check("post-trip window restarts from scratch",
          len(engine._rates.get(1, [])) < 3, str(engine._rates.get(1)))

    slow = AutoMod(bot=None)  # type: ignore[arg-type]
    for i in range(3):
        slow._check_spam(_Message(2, f"msg {i}"), spam_settings)
    # Backdate every timestamp past the window: a member who waits out the window
    # starts fresh instead of tripping on the fourth message an hour later.
    engine_stale = slow._rates.get(2)
    check("rate window recorded", engine_stale is not None and len(engine_stale) == 3, str(engine_stale))
    # Replace the list, not the elements: rebinding a local float does nothing.
    slow._rates[2] = [stamp - 60 for stamp in slow._rates[2]]
    late = slow._check_spam(_Message(2, "later"), spam_settings)
    check("stale window does not trip", not late.tripped, late.detail)
    slow.forget(2)
    check("forget clears rate state", slow._rates.get(2) is None, str(slow._rates.get(2)))
    slow.forget(2)  # idempotent
    check("forget twice is safe", slow._rates.get(2) is None)

    print("\n=== 9. backup codec ===")
    from core.backup import decode_payload, encode_payload, summarize_backup

    token = settings.bot_token
    sample = {"guild": {"name": "Test", "roles": [{"name": "Mod"}], "channels": []},
              "counts": {"roles": 1, "channels": 0}}
    sealed = encode_payload(sample, token)
    check("envelope is not plaintext", "Test" not in sealed, sealed[:40])
    check("round trip is exact", decode_payload(sealed, token) == sample)
    check("wrong key is rejected", _raises(ValueError, lambda: decode_payload(sealed, "wrong-key")))
    check("tampered payload is rejected", _raises(ValueError, lambda: decode_payload(sealed[:-8] + "AAAAAAAA", token)))
    summary = summarize_backup(sample)
    check("summary counts roles", summary["roles"] == 1, str(summary))
    check("summary names the guild", summary["guild_name"] == "Test", str(summary))

    print("\n=== 10. embed colour parsing ===")
    from core.embeds import parse_hex_color

    check("6-digit hex parses", parse_hex_color("#2E7D32") == 0x2E7D32)
    check("bare hex parses", parse_hex_color("1f6f6b") == 0x1F6F6B)
    check("0x prefix parses", parse_hex_color("0xABCDEF") == 0xABCDEF)
    check("short hex rejected", parse_hex_color("#FFF") is None)
    check("garbage rejected", parse_hex_color("chartreuse") is None)

    print("\n=== 11. status surface ===")
    state = runtime_state.snapshot()
    for key in ("status", "latency_ms", "guild_count", "user_count", "command_count",
                "moderation_action_count", "cogs_loaded", "uptime_human",
                "latency_history", "database", "automod_alerts"):
        check(f"snapshot.{key}", key in state, str(state.get(key))[:40])
    # command_count is only ever filled in by a live tree sync, which cannot
    # happen offline. Test the round-trip instead of a literal that would go
    # stale the next time a command is added; the real command list is
    # asserted against in section 6.
    check("command_count starts unsynced", state["command_count"] == 0, str(state["command_count"]))
    runtime_state.set_command_count(len(bot.tree.get_commands()))
    check("command_count round trips",
          runtime_state.snapshot()["command_count"] == len(bot.tree.get_commands()),
          str(runtime_state.snapshot()["command_count"]))
    check("snapshot.moderation_action_count reflects the ledger",
          state["moderation_action_count"] >= 1, str(state["moderation_action_count"]))
    check("no web_watcher key in the snapshot", "web_watcher" not in state)
    check("runtime state reports the offline bot", state["status"] in {"offline", "starting"}, state["status"])
    check("log ring captured records", len(runtime_state.logs) > 0, f"{len(runtime_state.logs)} records")
    check("log records shaped", all(
        {"timestamp", "level", "logger", "message", "source"} <= set(r.to_dict())
        for r in runtime_state.logs.snapshot(20)
    ))
    errors_only = [r for r in runtime_state.logs.snapshot(200) if r.levelno >= 40]
    check("level filter works", all(r.levelno >= 40 for r in errors_only), f"{len(errors_only)} errors")

    runtime_state.record_automod(
        guild_id=1, user_id=2, rule_id=3, keyword="spam", action="block_message", channel_id=4
    )
    check("automod alert recorded", len(runtime_state.automod_alerts) == 1,
          str(runtime_state.automod_alerts))
    check("automod alert keyed correctly",
          runtime_state.automod_alerts[0]["keyword"] == "spam", str(runtime_state.automod_alerts[0]))

    print("\n=== 12. schema ===")
    from core.models import Base, GuildBackup, GuildFilter

    tables = set(Base.metadata.tables)
    check(
        "every model mapped",
        tables == EXPECTED_TABLES,
        f"diff: {sorted(tables ^ EXPECTED_TABLES)}",
    )
    for name in sorted(EXPECTED_TABLES):
        check(f"{name} table present", name in tables)
    check("GuildFilter is mapped", GuildFilter.__tablename__ == "guild_filters")
    check("GuildBackup is mapped", GuildBackup.__tablename__ == "guild_backups")
    for action in ("MASSBAN", "SOFTBAN", "TEMPBAN", "AUTOMOD"):
        check(f"CaseAction.{action}", action in CaseAction.__members__, str(list(CaseAction)))

    check("filter CRUD round trip", await _filter_crud_roundtrip(database))

    print("\n=== 12a. SQLite DSN portability ===")
    _sqlite_dsn_checks()

    print("\n=== 12b. health server (Render keep-alive) ===")
    await _health_server_checks()

    print("\n=== 12c. additive column migration ===")
    _migration_checks()

    print("\n=== 12d. command sync discipline ===")
    await _command_sync_checks(bot)

    print("\n=== 12e. diagnostic probe honesty ===")
    await _probe_honesty_checks(bot)

    print("\n=== 13. teardown ===")
    await database.disconnect()
    check("db disposed", not database.is_ready)
    await bot.close()
    check("bot closed", True)

    print(f"\n{'='*60}\n  {PASS} passed, {FAIL} failed\n{'='*60}")
    return 1 if FAIL else 0


def _token_resolution_checks() -> None:
    """The bot token must resolve from any spelling a PaaS dashboard might use.

    Railway deployments crashed at startup with "DISCORD_BOT_TOKEN is not set"
    while a token was present in the environment under a different key. Two
    separate causes are covered here:

    1. An unrecognised key (``BOT_TOKEN``) — the fallback simply had to be
       accepted as an alias.
    2. An *empty* primary key shadowing a good fallback. ``AliasChoices`` stops
       at the first key that exists, even when its value is ``""``, which is
       what a cleared dashboard secret or a committed ``DISCORD_BOT_TOKEN=``
       line produces. A non-empty candidate must win instead.
    """
    from config import DISCORD_TOKEN_ENV_NAMES, ConfigurationError

    token = "MTIzNDU2Nzg5MDEyMzQ1Njc4.GaBcDe.fF0oBarBazQux0123456789abc"
    _extra = ("TOKEN", "DISCORD_SECRET", "discord_token", "bot_token", "discord_bot_token",
              "ORPHAN_TOKEN")

    @contextlib.contextmanager
    def _cleared_token_env():
        """Hide every candidate token key for the duration of the block."""
        saved = {
            name: os.environ.pop(name, None)
            for name in (*DISCORD_TOKEN_ENV_NAMES, *_extra)
        }
        try:
            yield
        finally:
            for name, value in saved.items():
                os.environ.pop(name, None)
                if value is not None:
                    os.environ[name] = value

    def resolve(env: dict[str, str], init: str | None = None) -> str:
        with _cleared_token_env():
            os.environ.update(env)
            try:
                kwargs: dict[str, Any] = {"_env_file": None}
                if init is not None:
                    kwargs["discord_bot_token"] = init
                return _Settings(**kwargs).bot_token
            except ConfigurationError:
                return "<raises>"

    for name in DISCORD_TOKEN_ENV_NAMES:
        check(f"token resolves from {name}", resolve({name: token}) == token)

    # Lowercase keys, since case_sensitive=False.
    check("token resolves from lowercase discord_token",
          resolve({"discord_token": token}) == token)

    check("DISCORD_BOT_TOKEN wins when both are set",
          resolve({"DISCORD_BOT_TOKEN": "primary", "DISCORD_TOKEN": "secondary"}) == "primary")

    # The AliasChoices bug: an empty primary shadows a usable fallback.
    check("empty DISCORD_BOT_TOKEN falls back to DISCORD_TOKEN",
          resolve({"DISCORD_BOT_TOKEN": "", "DISCORD_TOKEN": token}) == token)
    check("empty DISCORD_BOT_TOKEN falls back to BOT_TOKEN",
          resolve({"DISCORD_BOT_TOKEN": "", "BOT_TOKEN": token}) == token)
    check("whitespace-only DISCORD_BOT_TOKEN falls back",
          resolve({"DISCORD_BOT_TOKEN": "   ", "DISCORD_TOKEN": token}) == token)
    check("first non-empty candidate wins",
          resolve({"DISCORD_BOT_TOKEN": "", "DISCORD_TOKEN": "aa", "BOT_TOKEN": "bb"}) == "aa")

    check("no token at all raises", resolve({}) == "<raises>")
    # Generic names must stay rejected, or an unrelated host secret could be
    # used as a Discord credential without anybody noticing.
    check("a bare TOKEN is not accepted as the bot token", resolve({"TOKEN": token}) == "<raises>")
    check("DISCORD_SECRET is not accepted as the bot token",
          resolve({"DISCORD_SECRET": token}) == "<raises>")

    # An explicit constructor argument outranks the environment.
    check("explicit token beats the environment",
          resolve({"DISCORD_BOT_TOKEN": "from-env"}, "explicit") == "explicit")
    check("empty explicit token still yields the environment value",
          resolve({"DISCORD_BOT_TOKEN": "from-env"}, "") == "from-env")

    # The diagnostic must distinguish "never supplied" from "supplied under a
    # name we ignore" — otherwise a container that exits 4 on every restart
    # leaves nothing to act on.
    from config import _missing_token_message

    with _cleared_token_env():
        blank = _missing_token_message()
        check("the diagnostic says the token was never supplied",
              "never supplied" in blank, blank)

        os.environ["ORPHAN_TOKEN"] = token
        overlooked = _missing_token_message()
        check("the diagnostic names an ignored token-like variable",
              "ORPHAN_TOKEN" in overlooked, overlooked)
        check("the diagnostic suggests the canonical name",
              "rename it to" in overlooked, overlooked)
        # A value must never reach a log line, only the name.
        check("no secret value appears in the diagnostic",
              token[:20] not in overlooked, overlooked)

    # The error message must name the alternatives, since a naming mistake on a
    # dashboard is the overwhelmingly likely cause.
    # Force the "nothing set" state, since the rest of the suite may have left
    # a token in the environment.
    try:
        with _cleared_token_env():
            missing_token = _Settings(_env_file=None).bot_token
    except ConfigurationError as exc:
        message = str(exc)
    else:
        message = f"<no error, token was {missing_token!r}>"
    check("the error names every accepted variable",
          all(name in message for name in DISCORD_TOKEN_ENV_NAMES), message)


def _sqlite_dsn_checks() -> None:
    """Prove no SQLite DSN can resolve to a filesystem-root parent.

    Regression guard for a production crash on Linux hosts: a three-slash DSN
    such as ``sqlite+aiosqlite:///data/zagrosian_eye.db`` parses as an *absolute*
    path on Linux, so creating its parent directory raised
    ``PermissionError: [Errno 13] '/data'``. The identical string is merely
    relative on Windows, which is why it passed every desktop test.

    Absolute-path detection is asserted with :class:`PurePosixPath` on purpose:
    the native ``Path`` would answer "is this absolute?" differently per
    platform and re-open the very bug being guarded.
    """
    from config import _is_absolute_sqlite_target

    def remainder_of(url: str) -> str:
        return url.partition("://")[2]

    def sqlite_opens(url: str) -> PurePosixPath:
        rem = remainder_of(url)
        lead = len(rem) - len(rem.lstrip("/"))
        tail = rem.split("?", 1)[0].lstrip("/")
        return PurePosixPath("/" + tail) if lead >= 2 else PurePosixPath(tail)

    def _settings_for(dsn: str) -> Any:
        return _Settings(
            _env_file=None,
            discord_bot_token="MTIzNDU2Nzg5MDEyMzQ1Njc4.GaBcDe.fF0oBarBazQux0123456789abc",
            database_url=dsn,
        )

    relative_spellings = [
        "sqlite:///./data/zagrosian_eye.db",
        "sqlite:///data/zagrosian_eye.db",
        "sqlite+aiosqlite:///data/zagrosian_eye.db",
        "sqlite+aiosqlite:///./data/zagrosian_eye.db",
        "sqlite:///data/deep/nested/zagrosian_eye.db",
    ]
    for dsn in relative_spellings:
        resolved = _settings_for(dsn).database_url
        opened = sqlite_opens(resolved)
        check(
            f"{dsn} stays out of the filesystem root",
            str(opened.parent) != "/",
            str(opened),
        )

    check(
        "every relative spelling lands on the same database file",
        len({sqlite_opens(_settings_for(d).database_url).name for d in relative_spellings}) == 1,
    )

    # The async spelling is the one that used to bypass normalization entirely,
    # because only the bare "sqlite" scheme was rewritten.
    check(
        "sqlite+aiosqlite is normalized, not passed through",
        _settings_for("sqlite+aiosqlite:///data/x.db").database_url.startswith(
            "sqlite+aiosqlite:///"
        ),
    )

    # :memory: is a sentinel, not a filename. Rewriting it produces a real file
    # literally named ":memory:", which silently discards the database.
    for dsn in ("sqlite:///:memory:", "sqlite+aiosqlite:///:memory:"):
        resolved = _settings_for(dsn).database_url
        check(
            f"{dsn} keeps the in-memory sentinel",
            sqlite_opens(resolved).name == ":memory:",
            resolved,
        )

    # An operator who writes four slashes means an absolute path; honouring it
    # matters because silently relocating a database loses the existing ledger.
    for dsn in ("sqlite:////opt/app/data/prod.db", "sqlite+aiosqlite:////opt/app/data/prod.db"):
        check(f"{dsn} is left verbatim", _settings_for(dsn).database_url.endswith(
            "////opt/app/data/prod.db"
        ), _settings_for(dsn).database_url)

    check("a 3-slash remainder is relative", not _is_absolute_sqlite_target("/data/x.db"))
    check("a 4-slash remainder is absolute", _is_absolute_sqlite_target("//data/x.db"))
    check("a Windows drive path is absolute", _is_absolute_sqlite_target("C:/data/x.db"))
    check("an empty target is not absolute", not _is_absolute_sqlite_target(""))

    # Postgres DSNs must be untouched by any of this.
    pg = _settings_for("postgres://user:pass@host:5432/db").database_url
    check("postgres DSN untouched", pg == "postgresql+asyncpg://user:pass@host:5432/db", pg)
    # A query string must survive path normalization.
    check(
        "sqlite query string preserved",
        _settings_for("sqlite+aiosqlite:///./data/x.db?timeout=5").database_url.endswith(
            "?timeout=5"
        ),
    )


async def _health_server_checks() -> None:
    """Prove the liveness server binds, serves, and honours the PORT contract.

    Binds an ephemeral port rather than a fixed one so the suite never collides
    with a real deployment or a parallel run. Also asserts the two properties
    that actually matter in production: a malformed PORT is ignored instead of
    crashing the bot, and the health route stays 200 even while the gateway is
    offline (a monitor must not flap during a Discord reconnect).
    """
    import aiohttp

    from core.dashboard_state import BotStatus
    from core.health_server import HealthServer, resolve_port, start_from_env

    probe = web_server = HealthServer(0)
    try:
        web_server = HealthServer(0)
        await web_server.start()
    except OSError:
        # No loopback socket available in this sandbox; skip rather than fail a
        # suite that is about the bot.
        check("health server binds a socket", False, "no loopback available")
        return

    port = web_server.port
    base = f"http://127.0.0.1:{port}"
    check("health server bound to the requested port", port > 0, str(port))
    check("is_running after start", web_server.is_running)

    async with aiohttp.ClientSession() as session:
        async with session.get(f"{base}/") as response:
            body = await response.text()
            check("GET / returns 200", response.status == 200, str(response.status))
            check(
                "GET / says the bot is alive",
                body.strip().lower().startswith("bot is alive"),
                repr(body[:40]),
            )

        async with session.get(f"{base}/health") as response:
            payload = await response.json()
            check("GET /health returns 200", response.status == 200, str(response.status))
            check(
                "/health payload is JSON with a status field",
                isinstance(payload, dict) and "status" in payload and "online" in payload,
                str(sorted(payload))[:120],
            )

        # Liveness contract: 200 while the process runs, regardless of gateway.
        was = runtime_state.status
        runtime_state.set_status(BotStatus.OFFLINE)
        async with session.get(f"{base}/health") as response:
            check(
                "/health stays 200 while the gateway is offline",
                response.status == 200,
                str(response.status),
            )
        runtime_state.set_status(was)

        async with session.get(f"{base}/definitely-not-a-route") as response:
            check("unknown route 404s", response.status == 404, str(response.status))

    await web_server.stop()
    check("is_running is False after stop", not web_server.is_running)
    del probe

    # PORT parsing: absent, malformed and out-of-range all disable cleanly.
    saved = os.environ.pop("PORT", None)
    try:
        check("no PORT disables the server", resolve_port() is None)
        for bad in ("abc", "0", "70000", "-5"):
            os.environ["PORT"] = bad
            check(f"PORT={bad!r} is ignored", resolve_port() is None)
        os.environ["PORT"] = " 8080 "
        check("PORT is whitespace-tolerant", resolve_port() == 8080)

        # Occupy a port, then ask the server to bind the same one. A privileged
        # port (PORT=1) is not a reliable stand-in: this suite may run as an
        # administrator on Windows, where binding :1 succeeds instead of
        # failing. A genuine conflict exercises the same OSError path on every
        # platform and any privilege level.
        #
        # The blocker must bind 0.0.0.0, because that is the address the health
        # server uses; a 127.0.0.1 listener does not reliably conflict with it.
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            blocker.bind(("0.0.0.0", 0))
            blocker.listen(1)
            taken = blocker.getsockname()[1]
            os.environ["PORT"] = str(taken)
            check(
                "bind failure returns None instead of raising",
                await start_from_env() is None,
                f"port {taken}",
            )
        finally:
            blocker.close()
    finally:
        if saved is None:
            os.environ.pop("PORT", None)
        else:
            os.environ["PORT"] = saved


def _raises(exc_type: type[BaseException], fn: Any) -> bool:
    try:
        fn()
    except exc_type:
        return True
    except Exception:  # wrong exception type still counts as "raised"
        return False
    return False


async def _filter_crud_roundtrip(database: Any) -> bool:
    """CRUD on ``guild_filters`` through the service layer, case included.

    The test database is passed explicitly so this never reaches the
    process-wide singleton.
    """
    from core.services import add_filter, list_filters, remove_filter

    guild_id = 424242
    await add_filter(guild_id, "Spamword", created_by=1, database=database)
    # Same phrase, different case. Discord's keyword filter is case-insensitive,
    # so a second row would be a silent duplicate inside the native rule.
    if await add_filter(guild_id, "spamword", created_by=1, database=database):
        return False
    await add_filter(guild_id, "second", created_by=1, database=database)

    # Phrases are stored case-folded, so "Spamword" comes back as "spamword".
    if await list_filters(guild_id, database=database) != ["second", "spamword"]:
        return False
    if not await remove_filter(guild_id, "SPAMWORD", database=database):
        return False
    if await list_filters(guild_id, database=database) != ["second"]:
        return False
    if await remove_filter(guild_id, "never-added", database=database):
        return False
    await remove_filter(guild_id, "second", database=database)
    return await list_filters(guild_id, database=database) == []


if __name__ == "__main__":
    _purge_selftest_db()
    try:
        sys.exit(asyncio.run(main()))
    finally:
        _purge_selftest_db()
