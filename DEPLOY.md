# Deploying The Zagrosian Eye

Everything here assumes you already created the Discord application and copied
its token. Two ways to run it: on a plain box (or Docker-ish host) with
`python main.py`, or on a PaaS such as Render.

---

## 0. Discord application setup

1. https://discord.com/developers/applications → **New Application**.
2. **Bot → Add Bot**, then **Reset Token** and copy it. This is
   `DISCORD_BOT_TOKEN`.
3. **Bot → Privileged Gateway Intents**: nothing here is required. The bot asks
   only for the `guilds` intent (for guild counts and cache) — it does **not**
   request `message_content` or `members`. Leave the privileged toggles **off**.
4. **OAuth2 → General**: copy the **Client ID** and **Public Key**.
5. Invite it with:

   ```
   https://discord.com/oauth2/authorize?client_id=<CLIENT_ID>&permissions=1099780156422&scope=bot%20applications.commands
   ```

   `1099780156422` is the sum of the permissions the bot actually uses
   (Manage Roles, Kick, Ban, Manage Messages, Moderate Members, Manage Channels,
   View Channels, Send Messages, Embed Links, Read Message History, Add Reactions).
   Raise it manually in the invite dialog if you want more.

---

## 1. Run locally

```bash
git clone <your-repo> zagrosian-eye
cd zagrosian-eye

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

python -m pip install -r requirements.txt
copy .env.example .env          # Windows   (cp on macOS/Linux)
# edit .env: at minimum set DISCORD_BOT_TOKEN
python main.py
```

On first boot the bot creates `data/zagrosian_eye.db` (SQLite) and the `logs/`
directory, then syncs its slash commands.

Verify before celebrating:

```bash
python -m ruff check .
python selftest.py          # offline; no Discord login required
```

`selftest.py` boots the bot object and the database offline, and asserts the whole
command tree, the schema, the backup codec and the error taxonomy. A green run is
the strongest signal you have short of a live login.

> **Python version.** Python 3.13 removed the stdlib `audioop` module that
> `discord.py 2.3.2` imports at package load. `requirements.txt` pulls
> `audioop-lts` on 3.13+ to fill the gap. Python 3.11 or 3.12 need nothing
> special.

---

## 2. Environment variables

Only `DISCORD_BOT_TOKEN` is strictly required; every other key has a sane
default. Run `/status` in Discord for the fully resolved picture with secrets
redacted, or `/settings` for the per-server view.

| Variable | Default | Notes |
|---|---|---|
| `DISCORD_BOT_TOKEN` | — | **Required.** Alias: `DISCORD_TOKEN` |
| `APPLICATION_ID` / `CLIENT_ID` | — | Optional; used for invite links |
| `PUBLIC_KEY` | — | Optional |
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/zagrosian_eye.db` | Use `postgresql+asyncpg://…` in production |
| `DATABASE_TIMEOUT` | `5.0` | Seconds before a round-trip is declared unhealthy |
| `OWNER_IDS` | *(empty)* | Comma-separated user IDs for `/status`, `/test`, `/sync`, `/reload`. Empty ⇒ the Discord app owner only |
| `COMMAND_SYNC_MODE` | `global` | `guild` for instant sync while developing (needs `DEV_GUILD_ID`) |
| `DEV_GUILD_ID` | *(empty)* | Required when `COMMAND_SYNC_MODE=guild` |
| `LOG_LEVEL` | `INFO` | |
| `LOG_DIR` | `./logs` | |
| `LOG_BUFFER` | `400` | Records kept in the ring `/status` reads. `DASHBOARD_LOG_BUFFER` is still accepted as an alias |
| `DM_MEMBERS_ON_PUNISH` | `true` | Master switch; per-guild `/toggle-dm` overrides |
| `MAX_PURGE_AMOUNT` | `100` | Hard cap for `/clear` |
| `CASE_PREFIX` | `ZEYE` | Prefix on case references |
| `DEFAULT_MUTE_ROLE` | `Muted` | Name used by `/setup muted-role` |

There is no `PORT` to bind for the bot itself and no `HOST` to expose. The
gateway socket is outbound. A small optional health server does bind `PORT` when
the host sets it — see §3.3 for the Render keep-alive setup.

A **non-empty** `OWNER_IDS` is recommended even though the app owner falls back
automatically — it lets more than one operator run the developer commands.

---

## 3. Where to run it

The process opens **no listening socket** — it dials out to Discord's gateway.
That single fact rules out most free tiers: they are built around inbound HTTP
traffic, and a bot generates none.

| Host | Cost | Card needed | Stays up? | Disk |
|---|---|---|---|---|
| **Your own hardware** (Pi, mini PC, spare desktop) | $0 | No | Yes, indefinitely | Persistent |
| Oracle Cloud Always Free | $0 | **Yes** | Yes | Persistent |
| Render free web service | $0 | No | **No** — sleeps after 15 min idle | Ephemeral |
| Render Background Worker | $7/mo | Yes | Yes | Ephemeral |
| Railway / Fly.io | $5 trial, then billed | Yes | Until credit runs out | Ephemeral |

**Oracle's Always Free tier is the only reputable no-cost always-on option, and it
requires a credit card** for a temporary verification hold. There is currently no
free, no-card, genuinely always-on host that is safe to put a real bot on. Hosts
advertising one (Waifly, bot-hosting.net, FadeHost, "Kerit Cloud", and similar)
gate their free tiers behind clauses like *offline for 3 days → suspended*,
*manual renewal every 4 days*, or *free addresses sleep when idle* — which is not
24/7 by any honest reading.

If the no-card constraint is hard, the realistic answer is hardware you own. A
Raspberry Pi 4, an old desktop, or a used mini PC runs this bot indefinitely for
zero recurring cost and no vendor who can revoke it. Section 3.1 covers that.

### 3.1 On your own machine (Linux, systemd)

The durable free option. Requires a machine that stays powered on; your desktop
only qualifies if you never shut it down.

```bash
# 1. Place the code. /opt/zagrosian-eye must be owned by the service user,
#    because the unit hardens ReadWritePaths to data/ and logs/ only.
sudo mkdir -p /opt/zagrosian-eye
sudo cp -r . /opt/zagrosian-eye && cd /opt/zagrosian-eye

# 2. Dedicated unprivileged user, no login shell.
sudo useradd --system --create-home --home-dir /opt/zagrosian-eye \
    --shell /usr/sbin/nologin zeye

# 3. Virtualenv + dependencies, as zeye.
sudo -u zeye python3.12 -m venv .venv
sudo -u zeye .venv/bin/pip install -r requirements.txt

# 4. Secrets, readable only by zeye (unit sets ProtectSystem=strict).
sudo -u zeye cp .env.example .env
sudo -u zeye $EDITOR .env            # set DISCORD_BOT_TOKEN
sudo chown zeye:zeye .env && sudo chmod 600 .env

# 5. Unit + start.
sudo cp deploy/zagrosian-eye.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now zagrosian-eye
journalctl -u zagrosian-eye -f       # live logs
```

The unit runs as `zeye` with `ProtectSystem=strict` and grants write access to
`data/` and `logs/` only, so the token file and the rest of the tree are not
writable by a compromised process. `Restart=always` with `RestartSec=15` means a
crash or a gateway timeout recovers without you touching it; `StartLimitBurst`
stops an unrecoverable crash loop after 10 attempts in 5 minutes instead of
spinning forever.

```bash
sudo systemctl restart zagrosian-eye
sudo systemctl status zagrosian-eye
```

**Upgrades:** pull the new code, then

```bash
sudo systemctl stop zagrosian-eye
sudo -u zeye .venv/bin/pip install -r requirements.txt
sudo systemctl start zagrosian-eye
```

`data/` and `logs/` survive every step of that, which is the whole point.

### 3.2 Docker

`Dockerfile` targets any container host. It runs as an unprivileged user, sets
`PYTHONUNBUFFERED=1` so `journalctl`/`docker logs` stay live, and pins Python
3.12 (audioop is still stdlib there, so `discord.py` imports without the
`audioop-lts` shim).

```bash
docker build -t zagrosian-eye .
docker run -d --name zagrosian-eye --restart unless-stopped \
  --env-file .env -v zagrosian-data:/app/data -v zagrosian-logs:/app/logs \
  zagrosian-eye
docker logs -f zagrosian-eye
```

The `-v` mounts are what make the ledger survive an image rebuild. Skipping them
means every `docker build` starts you from an empty database.

### 3.3 Render free tier + UptimeRobot (keep-alive)

This is the only way to run on Render's free tier. The mechanism:

Render suspends a free web service after 15 minutes without inbound traffic. A
Discord bot holds an **outbound** gateway socket and never receives an HTTP
request, so it would be suspended roughly 15 minutes after boot while being
completely healthy. The workaround is to bind a trivial HTTP server and have an
external monitor ping it, so the process never looks idle.

`core/health_server.py` implements this with `aiohttp.web`, which ships inside
`discord.py`'s own dependency — **nothing is added to `requirements.txt`**.

| | |
|---|---|
| `GET /` | `200` + plain text `Bot is alive` |
| `GET /health` | `200` + JSON with bot status, uptime, latency, guild count |
| `GET /anything-else` | `404` |

The server starts **before** the database and gateway, so the port is accepting
connections while the process is still booting, and it shuts down **last**, so a
monitor sees `200` right through a redeploy instead of a connection refused.

Both routes return `200` whenever the process is running — even mid-reconnect.
Returning `503` on a transient Discord blip would make the monitor declare a
false outage; the real gateway state is reported inside the JSON body instead.

**1. Create the service.** Render → **New → Web Service** (not Background
Worker, which is paid) → connect the repo → `plan: free`. `render.yaml` in this
repo declares all of it, so you can use **New → Blueprint** and point at the repo
instead. Build `pip install --no-cache-dir -r requirements.txt`, start
`python main.py`.

**2. No port to configure.** Render injects `PORT` at runtime and expects your
service to bind it. The server reads `os.environ["PORT"]` itself and binds
`0.0.0.0` (not `127.0.0.1`, which a proxy outside the container cannot reach —
the most common cause of a health check that fails while everything looks fine
from inside). If `PORT` is unset the server does not start at all, which is what
keeps local runs and systemd units clean.

**3. Environment variables** (Render dashboard → Environment, or
`render.yaml` with `sync: false`):

```
DISCORD_BOT_TOKEN=<token>     # required, never commit
OWNER_IDS=<your-user-id>
DATABASE_URL=<see persistence note>
COMMAND_SYNC_MODE=global
PYTHONUNBUFFERED=1            # otherwise logs buffer and appear to hang
```

**4. Add the monitor.** UptimeRobot (free) → *Add New Monitor*:

- Monitor Type: **HTTP(s)**
- URL: `https://zagrosian-eye.onrender.com/health`
- Monitoring Interval: **5 minutes** (their free minimum)

Use the public `onrender.com` URL. Pinging `127.0.0.1` or `0.0.0.0` never
leaves the instance and does not count as traffic. Five minutes gives three
pings inside Render's 15-minute window.

**5. Confirm it worked.** Watch the Render logs for the line emitted at boot:

```
Health server listening on 0.0.0.0:10000 (/ and /health)
```

Then open `https://<your-service>.onrender.com/health` in a browser — you should
get JSON. Leave it for 20+ minutes and confirm UptimeRobot still reports **Up**;
if the service had slept, the first ping would have taken ~1 minute to wake it.

**Costs to watch.** Free web services get 750 instance hours per workspace per
month (shared across services) and are suspended for the rest of the month once
exhausted. Outbound bandwidth is capped, and a service exceeding it is suspended
if no payment method is on file. One always-on instance is comfortably inside
750 hours (a month is ~720), but a second service in the same workspace is not.

### Persistence: keep the ledger off the host disk

On every ephemeral host (Render, Railway, Fly.io, most free bot hosts) the local
filesystem is wiped on redeploy or restart. That deletes
`data/zagrosian_eye.db` — the case ledger — and `data/backups/`.

The fix is free and needs no credit card: **[Neon](https://console.neon.tech)**
offers a free PostgreSQL plan (0.5 GB per project). Your code already speaks
Postgres — `asyncpg` is in `requirements.txt`, `config.py` normalises the DSN,
and `core/database.py` takes a dialect-specific atomic path for case numbering.
So this is a `.env` change with **no code change**:

```
DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@ep-xxx.region.aws.neon.tech/neondb?sslmode=require
```

`main.py` applies the schema on boot, so tables are created automatically on
first connect. This also means that if you land on a free host that vanishes
overnight, your moderation history is still intact.

`data/backups/` stays local regardless — sealed snapshots are files. Copy them
somewhere durable if they matter.

---

## 4. Post-deploy checklist

In the server you invited the bot to:

```
/setup logs          # pick (or create) the moderation-log channel
/setup muted-role    # create (or pick) the mute role
/settings            # review the resolved per-server config
/status              # connection, health probes, AutoMod alerts, recent logs
/test                # per-subsystem PASS/FAIL probes
```

`/status` and `/test` are available to owners **and** to anyone with **Manage
Server**, so a trusted admin can confirm the deployment without editing
`OWNER_IDS`. `/status` renders the same readiness as a go/no-go checklist (token
well-formed, async driver, owners set, sync mode, log directory) alongside the
live log ring.

Then confirm the two Discord-side permissions that a slash command cannot grant
itself:

- **Manage Server** — required for `/filter` to touch the native AutoMod rule.
- **Manage Roles** — required for `/backup_load` to restore role names and colours.

---

## 5. Operations

- **Logs** — written to `logs/`, and mirrored into a bounded in-memory ring that
  `/status` reads. Rotation is handled by `LOG_MAX_BYTES` and `LOG_BACKUP_COUNT`.
- **Sync** — `/sync` republishes the command tree; `/sync guild` does it
  instantly for the dev guild. Global sync can take up to an hour to propagate.
- **Reload** — `/reload` hot-reloads extensions without a restart.
- **Backups** — `/backup_create` writes sealed files to `data/backups/`. They are
  sealed with `DISCORD_BOT_TOKEN`, so rotating the token orphans them: they can no
  longer be decoded.
- **Upgrades** — re-run `pip install -r requirements.txt` after pulling.
  `main.py` applies the schema on boot; there are no migration files. New tables
  are created automatically, but changing existing columns is not supported.

---

## 6. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `ModuleNotFoundError: No module named 'audioop'` | Python ≥ 3.13 without `audioop-lts`. Reinstall from `requirements.txt` |
| Bot goes offline ~15 min after deploy on Render | Free web services suspend on idle. Add the health server + UptimeRobot (§3.3) |
| Render reports unhealthy, no bot errors in logs | Usually bound to `127.0.0.1`; must be `0.0.0.0` |
| Logs are empty and the service looks hung | Set `PYTHONUNBUFFERED=1` so stdout is unbuffered |
| `Health server listening` never appears | `PORT` is unset, or the bind failed — check the logs for `Could not bind` |
| Slash commands absent after deploy | Wait out global propagation, or set `COMMAND_SYNC_MODE=guild` + `DEV_GUILD_ID` and run `/sync guild` |
| `/filter` reports "saved, but not enforced yet" | Missing **Manage Server** in that server. Grant it and re-run the command |
| `/backup_load` says "not a zeye-backup-1 envelope" | The file was created under a different bot token, or was edited |
| `OWNER_IDS` ignored | It is user IDs, comma-separated: `111,222`. An empty value means "app owner only" |
| Data gone after restart on a PaaS | Ephemeral disk — attach a disk or move to PostgreSQL (see §3) |
