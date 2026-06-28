from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import asyncpg


@dataclass
class GuildSettings:
    guild_id: int
    staff_role_id: Optional[int]
    ticket_category_mobile_id: Optional[int]
    ticket_category_pc_id: Optional[int]
    transcript_channel_id: Optional[int]


@dataclass
class Ticket:
    id: int
    guild_id: int
    channel_id: int
    opener_id: int
    target_id: int
    platform: Optional[str]
    challenge_data: dict
    status: str
    claimed_by: Optional[int]


class Database:
    def __init__(self, database_url: str):
        self.database_url = database_url
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self.pool = await asyncpg.create_pool(self.database_url, min_size=1, max_size=5)
        await self._init_schema()

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()
            self.pool = None

    async def _init_schema(self) -> None:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS guild_settings (
                  guild_id BIGINT PRIMARY KEY,
                  staff_role_id BIGINT NULL,
                  ticket_category_mobile_id BIGINT NULL,
                  ticket_category_pc_id BIGINT NULL,
                  transcript_channel_id BIGINT NULL,
                  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS allowed_guilds (
                  guild_id BIGINT PRIMARY KEY,
                  allowed_by BIGINT NOT NULL,
                  allowed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS tickets (
                  id SERIAL PRIMARY KEY,
                  guild_id BIGINT NOT NULL,
                  channel_id BIGINT NOT NULL UNIQUE,
                  opener_id BIGINT NOT NULL,
                  target_id BIGINT NOT NULL,
                  platform TEXT NULL,
                  challenge_data JSONB NOT NULL DEFAULT '{}'::jsonb,
                  status TEXT NOT NULL DEFAULT 'open',
                  claimed_by BIGINT NULL,
                  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                  closed_at TIMESTAMPTZ NULL
                );

                CREATE INDEX IF NOT EXISTS idx_tickets_guild_id ON tickets(guild_id);
                CREATE INDEX IF NOT EXISTS idx_tickets_opener_id ON tickets(opener_id);
                CREATE INDEX IF NOT EXISTS idx_tickets_target_id ON tickets(target_id);
                """
            )
            # Migrations for older installs
            await conn.execute(
                "ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS ticket_category_mobile_id BIGINT NULL;"
            )
            await conn.execute(
                "ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS ticket_category_pc_id BIGINT NULL;"
            )
            await conn.execute(
                "ALTER TABLE guild_settings ADD COLUMN IF NOT EXISTS transcript_channel_id BIGINT NULL;"
            )
            # legacy column (if present) won't be removed; we just stop using it
            # Migrations for older installs
            await conn.execute("ALTER TABLE tickets ADD COLUMN IF NOT EXISTS platform TEXT NULL;")
            await conn.execute(
                "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS challenge_data JSONB NOT NULL DEFAULT '{}'::jsonb;"
            )

    async def is_guild_allowed(self, guild_id: int) -> bool:
        row = await self._fetchrow("SELECT guild_id FROM allowed_guilds WHERE guild_id=$1", guild_id)
        return row is not None

    async def allow_guild(self, guild_id: int, allowed_by: int) -> None:
        await self._execute(
            """
            INSERT INTO allowed_guilds(guild_id, allowed_by)
            VALUES ($1, $2)
            ON CONFLICT (guild_id)
            DO UPDATE SET allowed_by=EXCLUDED.allowed_by, allowed_at=NOW()
            """,
            guild_id,
            allowed_by,
        )

    async def _fetchrow(self, query: str, *args: Any) -> Optional[asyncpg.Record]:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(query, *args)

    async def _execute(self, query: str, *args: Any) -> str:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            return await conn.execute(query, *args)

    async def get_guild_settings(self, guild_id: int) -> GuildSettings:
        row = await self._fetchrow(
            """
            SELECT guild_id, staff_role_id, ticket_category_mobile_id, ticket_category_pc_id, transcript_channel_id
            FROM guild_settings
            WHERE guild_id=$1
            """,
            guild_id,
        )
        if row:
            return GuildSettings(
                guild_id=int(row["guild_id"]),
                staff_role_id=int(row["staff_role_id"]) if row["staff_role_id"] else None,
                ticket_category_mobile_id=int(row["ticket_category_mobile_id"])
                if row["ticket_category_mobile_id"]
                else None,
                ticket_category_pc_id=int(row["ticket_category_pc_id"]) if row["ticket_category_pc_id"] else None,
                transcript_channel_id=int(row["transcript_channel_id"])
                if row["transcript_channel_id"]
                else None,
            )

        await self._execute("INSERT INTO guild_settings(guild_id) VALUES ($1)", guild_id)
        return GuildSettings(
            guild_id=guild_id,
            staff_role_id=None,
            ticket_category_mobile_id=None,
            ticket_category_pc_id=None,
            transcript_channel_id=None,
        )

    async def set_staff_role(self, guild_id: int, staff_role_id: int | None) -> None:
        await self._execute(
            """
            INSERT INTO guild_settings(guild_id, staff_role_id, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (guild_id)
            DO UPDATE SET staff_role_id=EXCLUDED.staff_role_id, updated_at=NOW()
            """,
            guild_id,
            staff_role_id,
        )

    async def set_ticket_category_mobile(self, guild_id: int, ticket_category_id: int | None) -> None:
        await self._execute(
            """
            INSERT INTO guild_settings(guild_id, ticket_category_mobile_id, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (guild_id)
            DO UPDATE SET ticket_category_mobile_id=EXCLUDED.ticket_category_mobile_id, updated_at=NOW()
            """,
            guild_id,
            ticket_category_id,
        )

    async def set_ticket_category_pc(self, guild_id: int, ticket_category_id: int | None) -> None:
        await self._execute(
            """
            INSERT INTO guild_settings(guild_id, ticket_category_pc_id, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (guild_id)
            DO UPDATE SET ticket_category_pc_id=EXCLUDED.ticket_category_pc_id, updated_at=NOW()
            """,
            guild_id,
            ticket_category_id,
        )

    async def set_transcript_channel(self, guild_id: int, channel_id: int | None) -> None:
        await self._execute(
            """
            INSERT INTO guild_settings(guild_id, transcript_channel_id, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (guild_id)
            DO UPDATE SET transcript_channel_id=EXCLUDED.transcript_channel_id, updated_at=NOW()
            """,
            guild_id,
            channel_id,
        )

    async def create_ticket(
        self,
        guild_id: int,
        channel_id: int,
        opener_id: int,
        target_id: int,
        platform: str | None = None,
        challenge_data: dict | None = None,
    ) -> Ticket:
        challenge_data = challenge_data or {}
        row = await self._fetchrow(
            """
            INSERT INTO tickets(guild_id, channel_id, opener_id, target_id, platform, challenge_data, status)
            VALUES ($1, $2, $3, $4, $5, $6, 'open')
            RETURNING id, guild_id, channel_id, opener_id, target_id, platform, challenge_data, status, claimed_by
            """,
            guild_id,
            channel_id,
            opener_id,
            target_id,
            platform,
            challenge_data,
        )
        assert row is not None
        return Ticket(
            id=int(row["id"]),
            guild_id=int(row["guild_id"]),
            channel_id=int(row["channel_id"]),
            opener_id=int(row["opener_id"]),
            target_id=int(row["target_id"]),
            platform=str(row["platform"]) if row["platform"] else None,
            challenge_data=dict(row["challenge_data"]) if row["challenge_data"] else {},
            status=str(row["status"]),
            claimed_by=int(row["claimed_by"]) if row["claimed_by"] else None,
        )

    async def get_ticket_by_channel(self, channel_id: int) -> Ticket | None:
        row = await self._fetchrow(
            """
            SELECT id, guild_id, channel_id, opener_id, target_id, platform, challenge_data, status, claimed_by
            FROM tickets
            WHERE channel_id=$1
            """,
            channel_id,
        )
        if not row:
            return None
        return Ticket(
            id=int(row["id"]),
            guild_id=int(row["guild_id"]),
            channel_id=int(row["channel_id"]),
            opener_id=int(row["opener_id"]),
            target_id=int(row["target_id"]),
            platform=str(row["platform"]) if row["platform"] else None,
            challenge_data=dict(row["challenge_data"]) if row["challenge_data"] else {},
            status=str(row["status"]),
            claimed_by=int(row["claimed_by"]) if row["claimed_by"] else None,
        )

    async def claim_ticket(self, ticket_id: int, staff_user_id: int) -> None:
        await self._execute(
            """
            UPDATE tickets
            SET claimed_by=$2
            WHERE id=$1
            """,
            ticket_id,
            staff_user_id,
        )

    async def close_ticket(self, ticket_id: int) -> None:
        await self._execute(
            """
            UPDATE tickets
            SET status='closed', closed_at=NOW()
            WHERE id=$1
            """,
            ticket_id,
        )

    async def reopen_ticket(self, ticket_id: int) -> None:
        await self._execute(
            """
            UPDATE tickets
            SET status='open', closed_at=NULL
            WHERE id=$1
            """,
            ticket_id,
        )
