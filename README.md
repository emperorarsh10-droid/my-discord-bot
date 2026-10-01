# The Zagrosian Eye

A production-grade Discord moderation and server-management bot. Every capability
is a **slash command** — there is no web tier, no HTTP listener, and no port to
expose.

The bot requests the privileged `message_content` intent, because the AutoMod
**escalation ladder** and **sticky-message reposting** are enforced from
`on_message` listeners and read `message.content`. Handing phrase blocking to
Discord's own AutoMod covers keyword hits, but the warn/kick ladder and stickies
are bot-side by design, and without the intent they would be silently inert
while every slash command still appeared to work. Phrase blocking itself is
still evaluated by Discord, so it keeps working even if the intent is ever
revoked.

---

## Table of contents

- [Requirements](#requirements)
- [Install](#install)
- [Configure](#configure)
- [Invite the bot](#invite-the-bot)
- [Run](#run)
- [First-run setup](#first-run-setup)
- [Commands](#commands)
- [Architecture](#architecture)
- [Data model](#data-model)
- [Safety properties](#safety-properties)
- [Testing](#testing)
- [Deployment notes](#deployment-notes)
- [Troubleshooting](#troubleshooting)

---

## Requirements

| Component | Version | Why |
|---|---|---|
| Python | 3.11+ (developed on 3.14) | `StrEnum`, `asyncio.timeout`, modern typing |
| `discord.py` | 2.3.2+ | Slash-command `app_commands` API, native AutoMod |
| `SQLAlchemy` | 2.1+ | async ORM with the 2.0 style |
| `aiosqlite` | any | default SQLite driver |
| `asyncpg` | any | only for PostgreSQL |
| `pydantic-settings` | 2.x | validated, secret-aware config |

> **Python 3.13+:** stdlib `audioop` was removed. `audioop-lts` is listed in
> `requirements.txt` as a drop-in replacement so `voice` extras keep importing.

---

## Install

```powershell
git clone <your-fork-url> "discord ai maker"
cd "discord ai maker"

python -m venv .venv
.\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
pip install -r requirements.txt
```

<details>
<summary>PostgreSQL instead of SQLite</summary>

```powershell
pip install asyncpg
# then set DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/dbname
```
</details>

---

## Configure

```powershell
Copy-Item .env.example .env
```

`.env` is git-ignored. Every value is optional except `DISCORD_BOT_TOKEN`.

### Required

| Variable | Description |
|---|---|
| `DISCORD_BOT_TOKEN` | Bot token from the Developer Portal → Bot → Token |

### Full reference

| Variable | Default | Notes |
|---|---|---|
| **Database** | | |
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/zagrosian_eye.db` | Short forms are normalized: `postgres://` → `postgresql+asyncpg://`, `sqlite:///` → `sqlite+aiosqlite:///` |
| `DATABASE_TIMEOUT` | `5.0` | Seconds before a DB round-trip is declared unhealthy |
| **Logging** | | |
| `LOG_LEVEL` | `INFO` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` \| `CRITICAL` |
| `LOG_DIR` | `./logs` | |
| `LOG_MAX_BYTES` | `10485760` | Rotate at 10 MiB |
| `LOG_BACKUP_COUNT` | `5` | |
| `LOG_BUFFER` | `400` | Records kept in the live ring that `/status` reads. The old `DASHBOARD_LOG_BUFFER` name is still accepted |
| **Command sync** | | |
| `COMMAND_SYNC_MODE` | `global` | `global` = production (up to ~1h propagation). `guild` = instant, dev only |
| `DEV_GUILD_ID` | *(unset)* | Required when `COMMAND_SYNC_MODE=guild` |
| **Authorization** | | |
| `OWNER_IDS` | *(unset)* | Comma-separated user IDs for `/test`, `/sync`, `/reload`. Also grants rate-limit exemption. Accepts `123`, `123,456`, or a JSON list |
| **Moderation defaults** | | |
| `MAX_PURGE_AMOUNT` | `100` | Hard ceiling for `/purge`, 2–5000 |
| `CASE_PREFIX` | `ZEYE` | Case-ID prefix, 2–8 chars |
| `DEFAULT_MUTE_ROLE` | `Muted` | Role name auto-created by `/mute` when none is configured |
| `DM_MEMBERS_ON_PUNISH` | `true` | **Global master switch** for punishment DMs. A per-guild override can only narrow this, never re-enable it |

---

## Invite the bot

Build a URL from your Application ID:

```
https://discord.com/oauth2/authorize?client_id=YOUR_APP_ID&permissions=1099780156422&scope=bot%20applications.commands
```

That permission integer grants exactly: **Kick Members**, **Ban Members**,
**View Channel**, **Send Messages**, **Manage Messages**, **Embed Links**,
**Read Message History**, **Manage Roles**, **Moderate Members**. Nothing else —
no Administrator, no Manage Server. The bot checks for these at runtime and names
any that are missing.

---

## Run

```powershell
python main.py
```

Boot order is deliberate: logging → config validation → database connect → schema
create → client build → extensions load → gateway login → (on ready) command sync and
the expiry sweeper. A failure at any step aborts with a specific message instead of a
half-live bot.

First global sync can take up to an hour to propagate. Use
`COMMAND_SYNC_MODE=guild` with `DEV_GUILD_ID` while developing.

---

## First-run setup

```
/setup logs         # pick the moderation-log channel (auto-detected if omitted)
/setup muted-role   # create or pick the mute role
/settings           # review the resolved config
```

---

## Commands

### Moderation — `cogs/moderation.py`

| Command | Notes |
|---|---|
| `/ban <user> [reason] [delete_days]` | Bans, optionally purging 0–7 days of the user's messages |
| `/kick <user> [reason]` | |
| `/mute <user> [duration] [reason]` | Optional duration; timed mutes lift themselves via a background sweeper |
| `/unmute <user> [reason]` | |
| `/warn <user> <reason>` | |
| `/warnings <user> [action]` | Case history, optionally filtered by action type |
| `/purge <amount>` | Hard-capped by `MAX_PURGE_AMOUNT`, re-validated against the live setting |
| `/massban <targets> [reason] [delete_days]` | Up to 100 IDs at once. Accepts raw IDs, `<@id>` pings, or a mixed paste. Bounded to 5 concurrent requests, one ledger case per account |
| `/softban <user> [reason] [delete_days]` | Bans to purge history, then unbans so they can rejoin. Reports honestly if the lift step failed |
| `/tempban <user> <duration> [reason] [delete_days]` | Lifts itself. Accepts users who already left; the sweeper runs every 5 minutes |

Durations accept `30m`, `2h`, `7d`, `1w`, or combinations (`1d12h`).

Every punish command refuses a target the moderator cannot act on — Discord only
enforces role order for the *bot*, not for the moderator issuing the command, so
without that check a moderator with **Moderate Members** could ban the whole admin
team and every ban would succeed. Bot owners and administrators bypass it.

### Management — `cogs/management.py`

| Command | Notes |
|---|---|
| `/setup logs [channel]` | |
| `/setup muted-role [name]` | |
| `/settings` | Secrets are redacted; tokens shown as a fingerprint only |
| `/toggle-dm [enabled]` | Per-guild override of `DM_MEMBERS_ON_PUNISH` |
| `/userinfo [member]` | Account age, join date, roles, boosting and key permissions |
| `/slowmode <seconds> [channel]` | 0 disables; hard-capped at 21600 (6h) |
| `/lockdown [channel] [reason]` | Denies **@everyone** the ability to send messages |
| `/unlock [channel] [reason]` | Clears only that `send_messages` overwrite |
| `/filter action:<add\|remove\|list> [phrase]` | Blocks phrases through Discord's native AutoMod |
| `/embed_builder <title> [description] [color] [image] [footer] [channel]` | Composes an embed through a form and posts it |
| `/embed_cancel` | Throws away the draft you have open |
| `/backup_create <name>` | Sealed snapshot of roles, channels and settings to `data/backups/` |
| `/botstatus` | Connection card: gateway ping, uptime, subsystem probes, session errors |
| `/serverinfo` | Member/role/channel counts and the bot's permissions in this guild |
| `/system` | Gateway latency, uptime, server/command counts, database probe, session errors |

### Phrase filtering

`/filter action:add phrase:...` writes to `guild_filters` and then reconciles a
single native AutoMod rule named `Zagrosian Eye · blocked phrases`. The table is
the record of intent; the Discord rule is the enforcement.

Phrase blocking specifically works without the `message_content` intent, because
Discord evaluates the keyword and blocks the message itself. The bot only hears
about it afterwards, through `on_automod_action`, where it logs the block and
keeps a 50-entry ring for `/status`. The *escalation ladder* layered on top —
`/automod` warn/kick/timeout rungs, and the per-guild spam-rate window — is
bot-side and does read message text, which is why the intent is requested.

`/filter action:list` reports whether the rule is **live**, **drifted** (phrases
saved but missing from the rule — usually because someone deleted it by hand), or
**inactive**. Re-running any `/filter` action re-syncs and repairs the drift.

If the bot lacks **Manage Server**, the phrases are still saved and the command
says plainly that nothing is being *blocked* yet — it does not claim success it
does not have.

### Backups

`/backup_create` serialises the guild, seals it with the bot token as the key
(HMAC-SHA256 counter-mode keystream plus an integrity MAC), and writes it to
`data/backups/<guild_id>_<name>.zeye`. This is tamper-evident and obfuscated, **not
encryption** — anyone holding the bot token can read a backup.

`/backup_load` asks for confirmation on a button scoped to the moderator who ran
it, then restores conservatively: guild name and description, plus role names,
colours, hoist and mentionable, and the positions of roles that already exist.

It deliberately never recreates or deletes channels, and never touches permissions.
A snapshot is a record of how a server looked, not a grant of authority, and a
restore must never widen anyone's access.

### Developer — `cogs/developer.py`

| Command | Notes |
|---|---|
| `/test` | Live DB round-trip, gateway/REST latency, and gateway cache probes with per-subsystem PASS/FAIL labels. Owners **and** server admins (Manage Server) |
| `/status` | The operational panel: connection, health probes, AutoMod alerts, recent logs, and a config go/no-go checklist |
| `/sync [global\|guild]` | Force a command sync — owner only |
| `/reload [cog]` | Hot-reload one cog or all of them — owner only |

---

## Architecture

```
config.py              Pydantic settings, SecretStr handling, runtime validation
main.py                Bot subclass, boot sequence, error handlers, lifecycle
selftest.py            Offline end-to-end regression suite (172 assertions)

core/
  database.py          AsyncEngine, session factory, schema, health, atomic allocator
  models.py            SQLAlchemy ORM models
  services.py          Guild config, case ledger, audit reasons, DM gate, filters
  errors.py            Error taxonomy + global handlers
  embeds.py            Branded embed construction, hex colour parsing
  transformers.py      Duration transformer and parser
  ratelimit.py         Sliding-window + named-action limiters
  logging_setup.py     Console/file/log-ring fan-out
  dashboard_state.py   Runtime state singleton, bounded log and AutoMod rings
  targets.py           Bulk user-ID parsing for the batch commands (pure)
  automod.py           Phrase normalization and native rule reconciliation
  backup.py            Sealed codec, guild serialisation, conservative restore

cogs/
  moderation.py        /ban /kick /mute /unmute /warn /warnings /purge /massban
                       /softban /tempban + expiry sweeper
  management.py        /setup /settings /toggle-dm /userinfo /slowmode /lockdown
                       /unlock /filter /embed_builder /backup_* /botstatus
                       /serverinfo /system + AutoMod listener
  developer.py         /status /test /sync /reload
```

Design rules the code holds to:

- Errors are values. Every expected failure is a `ZagrosError` subclass carrying a
  safe `user_message`; exceptions are the last resort.
- Immutability by default, explicit mutable state, acyclic dependencies.
- No comment describes *what* code does. Comments explain *why*.
- Nothing is truncated with "rest of code omitted".

---

## Data model

Five tables, created automatically on first boot.

| Table | Purpose |
|---|---|
| `guild_configs` | Per-guild settings: log channel, muted role, DM override, action counter |
| `mod_cases` | Append-only ledger. `case_ref` like `ZEYE-000142` |
| `case_counters` | Atomic per-guild sequence |
| `guild_filters` | Blocked phrases, normalized, unique per guild, with the moderator who added each |
| `guild_backups` | Sealed backup payloads, indexed by guild and label |

**Case numbers are gap-free and race-free.** Allocation uses
`INSERT ... ON CONFLICT DO UPDATE ... RETURNING` inside the caller's transaction —
the only portable construction that works on both SQLite and PostgreSQL. The
`(guild_id, case_number)` unique constraint is the backstop.

Timed mutes and tempbans are lifted by a sweeper that starts in `on_ready`, not at
extension load, because loading extensions happens before the gateway connects.
It runs every 5 minutes and is idempotent: a ban whose window passed while the
process was down is lifted on the next pass rather than left hanging.

There is no migration tool. New tables are created automatically; changing an
existing table's columns is not supported.

---

## Safety properties

- **Rate limiting.** 5 punitive actions per 30s per guild, 3 `/purge` per 30s. Bot
  owners are exempt. `discord.py` 2.3.2 has no built-in `app_commands.cooldown`, so
  this is a hand-rolled sliding window. `/massban` additionally caps itself at 5
  concurrent requests, because 100 parallel REST calls would stall unrelated
  commands through the shared global rate-limit bucket.
- **No accidental mass-pings.** `allowed_mentions=discord.AllowedMentions.none()`
  means the bot can never ping `@everyone`, roles, or users through its own output.
- **Hierarchy checks, in both directions.** Before every action the bot checks its
  own role against the target, *and* the moderator's role against the target. Bot
  owners and administrators bypass the second check.
- **Bounded bulk input.** `/massban` caps at 100 IDs, de-duplicates them, and
  reports unparsable tokens instead of failing the whole run.
- **Honest failure.** A partial state is reported as partial. If AutoMod cannot be
  synced, or a softban's lift step fails, the command says so rather than implying
  success.
- **DM privacy.** `DM_MEMBERS_ON_PUNISH=false` is a global master switch. A per-guild
  `true` cannot override it.
- **Secret hygiene.** Tokens are `SecretStr`. `/settings` shows an 8-character
  fingerprint (`token_fingerprint`), never the value. Logs redact by pattern.
- **Bounded everything.** Log ring, AutoMod alert ring, purge amount, bulk-target
  count, log file rotation. No unbounded growth from user input.
- **Graceful degradation.** A dead database never takes down moderation. It degrades
  the affected feature and says so in the log.

---

## Testing

The suite is fully offline — no Discord token, no network, no listening socket, a
temporary SQLite file, and it cleans up after itself.

```powershell
python selftest.py
```

172 assertions across configuration parsing, duration parsing, the atomic case
allocator, the error taxonomy, the rate limiter, the DM gate, the full command
tree, extension loading, bulk-ID parsing, phrase normalization, the backup codec
(tamper detection included), hex colour parsing, the runtime snapshot, and the
five-table schema.

Lint (config in `ruff.toml`, covers `pyflakes`, `bugbear`, `bandit`, `async`, and
`logging` rulesets):

```powershell
pip install ruff
python -m ruff check .
```

The suite also passes under `python -O`, which strips `assert` statements — no test
or runtime path depends on one.

### What cannot be tested offline

Gateway login, slash-command publication, live permission and role-hierarchy
behaviour, native AutoMod rule creation, modal interactions, real DM delivery, and
reconnect handling all need a real token. Run `/test` and `/status` in a live
server after inviting the bot to smoke-test those paths.

---

## Deployment notes

**systemd** (`/etc/systemd/system/zagrosian.service`):

```ini
[Unit]
Description=The Zagrosian Eye
After=network-online.target

[Service]
Type=simple
User=zagrosian
WorkingDirectory=/opt/zagrosian
ExecStart=/opt/zagrosian/.venv/bin/python main.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

**Behind a reverse proxy:** not needed. The bot holds no listening socket, so there
is nothing to terminate or forward. TLS is Discord's, end to end.

**PostgreSQL:** use `postgresql+asyncpg://`, run `/settings` to confirm the dialect,
and make sure the DB user can `CREATE TABLE` on first boot.

**Backups:** `data/backups/` holds the sealed `.zeye` files. Back up that directory
if the files matter — the `guild_backups` table is only an index, and re-sealing
after a token change is not possible.

**Graceful shutdown:** `SIGTERM` triggers a real shutdown — command sync is
skipped, in-flight sweeps are allowed to finish, then the engine is disposed.

---

## Troubleshooting

**Commands do not appear.** Global sync takes up to an hour. Set
`COMMAND_SYNC_MODE=guild` and `DEV_GUILD_ID` to your test server, then run `/sync`.

**`/filter` says "saved, but not enforced yet".** The phrases are in the database
but Discord's AutoMod rule could not be touched. The bot needs **Manage Server** in
that server. Re-run the command after granting it.

**`/filter action:list` says "drifted".** Someone deleted or edited the native rule
by hand in Discord's own UI. Re-run any `/filter` action to rebuild it from the
table.

**`/massban` skips accounts and says why.** It refuses to touch the server owner,
you, the bot, and anyone outranking either the bot or you. It also reports tokens
that were not valid snowflakes instead of silently dropping them.

**`/backup_load` returns "not a zeye-backup-1 envelope".** The file was created by a
different bot token, or edited. Backups are sealed with the token, so changing
`DISCORD_BOT_TOKEN` orphans every existing backup.

**`/purge` says the amount is too high.** `MAX_PURGE_AMOUNT` is a hard ceiling
re-validated server-side regardless of client input. Raise it in `.env` if intended.

**Mutes or tempbans did not lift.** The sweeper runs every 5 minutes and starts in
`on_ready`. Check the log for `expiry` lines, and confirm the muted role still exists
and the bot can still manage it.

**Database unhealthy.** Run `/test` for the exact error and the resolved DSN. For
SQLite, confirm the `data/` directory is writable.

**Rate limited by accident.** Bot owners are exempt via `OWNER_IDS`. If
`OWNER_IDS` is unset, only the Discord application owner qualifies.
