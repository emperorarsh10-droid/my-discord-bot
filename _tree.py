"""Load every cog offline and dump the real command tree."""

import asyncio
import os

os.environ.setdefault(
    "DISCORD_BOT_TOKEN",
    "MTIzNDU2Nzg5MDEyMzQ1Njc4.GaBcDe.fF0oBarBazQux0123456789abc",
)
os.environ.setdefault("OWNER_IDS", "123456789012345678")
os.environ.setdefault("COMMAND_SYNC_MODE", "global")

import discord

from config import get_settings
from core.dashboard_state import runtime_state
from main import build_bot, load_extensions

REQUESTED = [
    "modhelp", "warn", "warns", "clearwarns", "timeout", "untimeout",
    "infractions", "reason", "notes:add", "notes:view", "cases", "viewcase",
    "unban", "banlist", "purge", "lockdown", "unlock", "massban", "filter",
    "nuke", "panic", "unpanic", "slowmode", "slowmodeall", "antiinvite",
    "antispam", "blacklist:add", "blacklist:remove", "blacklist:list",
    "lockdownall", "unlockall", "setup", "softban", "nick", "role", "whois",
    "avatar", "verify", "altcheck", "strip", "modstats", "automod", "ignore",
    "unignore", "embed", "dm", "temprole", "roleall", "reactionrole",
    "invites", "clean", "topic", "sticky", "unsticky", "giveaway", "poll",
    "vckick", "vcmute", "vcunmute", "vclock", "vcunlock",
]

#: Command groups are registered by name but hold no options of their own, so
#: the walker reports their children with a `group:` prefix. Both spellings count
#: as present; otherwise every group looks missing.
GROUPS = {"automod", "filter", "notes", "blacklist", "config"}


async def main() -> None:
    settings = get_settings()
    bot = build_bot(settings)
    failures = await load_extensions(bot, settings)

    if failures:
        print("LOAD FAILURES:")
        for name in failures:
            print("  ", name, "->", runtime_state.cog_errors.get(name, "?"))
        return

    def walk(node, prefix=""):
        # get_commands(), not walk_commands(): the tree's walker already yields
        # nested group children, so recursing over it would count each subcommand
        # twice and report phantom duplicates.
        out = []
        children = node.get_commands() if hasattr(node, "get_commands") else node.walk_commands()
        for cmd in children:
            if isinstance(cmd, discord.app_commands.Group):
                out += walk(cmd, f"{prefix}{cmd.name}:")
                continue
            if isinstance(cmd, discord.app_commands.Command):
                out.append((f"{prefix}{cmd.name}", getattr(cmd, "cog_name", "?")))
        return out

    found = walk(bot.tree)
    have = {name for name, _ in found}
    for group in GROUPS:
        if any(name.startswith(f"{group}:") for name in have):
            have.add(group)
    missing = [r for r in REQUESTED if r not in have]
    dupes = sorted({n for n in have if sum(1 for m, _ in found if m == n) > 1})

    print(f"total commands: {len(found)}")
    print("DUPLICATE PATHS:", dupes if dupes else "(none)")
    print()
    print("MISSING:", " ".join(missing) if missing else "(none)")
    print()
    for name, cog in sorted(found):
        mark = " " if name in REQUESTED else "."
        print(f" {mark} /{name:22} [{cog}]")
    await bot.close()


asyncio.run(main())
