import asyncio
import os
import time
from datetime import UTC, datetime

os.environ["DISCORD_BOT_TOKEN"] = "MTU1NDYxOTYyODE1ODg0OTA2NQ.GaFRNB.tcO_hmd8h4Rbl-tWRoUAq75t_"

from sqlalchemy import func, insert, select

from config import get_settings
from core.database import Database
from core.models import CaseAction, ModCase
from core.services import get_or_create_guild_config


async def main() -> None:
    get_settings()
    db = Database()
    await db.connect()

    async def timed(label, fn, n):
        start = time.perf_counter()
        for i in range(n):
            await fn(i)
        per = (time.perf_counter() - start) / n * 1000
        print(f"  {label:32} {per:8.2f} ms/op  (n={n})")

    await timed(
        "get_or_create_guild_config",
        lambda i: get_or_create_guild_config(900000 + i),
        50,
    )

    async def count_cases(i):
        async with db.session() as ses:
            await ses.execute(select(func.count()).select_from(ModCase))

    await timed("SELECT count(mod_cases)", count_cases, 50)

    async def write_case(i):
        async with db.session() as ses:
            await ses.execute(
                insert(ModCase),
                [
                    {
                        "guild_id": 1,
                        "user_id": 900000 + i,
                        "action": CaseAction.WARN,
                        "reason": "bench",
                        "moderator_id": 1,
                        "created_at": datetime.now(UTC),
                    }
                ],
            )

    await timed("INSERT mod_case", write_case, 25)
    await db.disconnect()


asyncio.run(main())