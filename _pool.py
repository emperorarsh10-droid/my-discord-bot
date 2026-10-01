"""Quantify the NullPool cost: SQLite reconnects on every single query."""

import asyncio
import os
import time
from datetime import UTC, datetime

os.environ["DISCORD_BOT_TOKEN"] = "MTU1NDYxOTYyODE1ODg0OTA2NQ.GaFRNB.tcO_hmd8h4Rbl-tWRoUAq75t_"

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from config import PROJECT_ROOT
from core.models import ModCase


async def run(poolclass, label, n=60):
    db_path = PROJECT_ROOT / "data" / "_bench.db"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    kwargs = {"echo": False, "future": True}
    if poolclass is not None:
        kwargs["poolclass"] = poolclass
        kwargs["connect_args"] = {"timeout": 30}
    engine = create_async_engine(url, **kwargs)
    async with engine.begin() as conn:
        await conn.run_sync(ModCase.__table__.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def read():
        async with factory() as ses:
            await ses.execute(select(func.count()).select_from(ModCase))

    start = time.perf_counter()
    for _ in range(n):
        await read()
    per = (time.perf_counter() - start) / n * 1000
    print(f"  {label:34} {per:7.2f} ms/op")
    await engine.dispose()
    return per


async def main():
    print("Same read query, two pool strategies:")
    null = await run(NullPool, "NullPool (current)")
    print()
    from sqlalchemy.pool import AsyncAdaptedQueuePool

    pooled = await run(AsyncAdaptedQueuePool, "AsyncAdaptedQueuePool")
    print()
    print(f"  saving per query: {null - pooled:6.2f} ms")
    print(f"  current cost at 10 q/s: {null * 10:.0f} ms/s of overhead")


asyncio.run(main())