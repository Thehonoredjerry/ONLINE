from __future__ import annotations

import logging

import discord
from discord.ext import commands

from . import config
from .db import Database
from .cogs.admin import AdminCog
from .cogs import tickets


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("bot")


class ChallengeTicketBot(commands.Bot):
    def __init__(self, db: Database):
        intents = discord.Intents.default()
        intents.message_content = True  # required for transcript export
        super().__init__(command_prefix="!", intents=intents)
        self.db = db

    async def setup_hook(self) -> None:
        await self.add_cog(AdminCog(self.db))
        await tickets.setup(self, self.db)

        # Sync slash commands
        if config.DEV_GUILD_ID:
            guild = discord.Object(id=int(config.DEV_GUILD_ID))
            await self.tree.sync(guild=guild)
            log.info("Synced commands to DEV_GUILD_ID=%s", config.DEV_GUILD_ID)
        else:
            await self.tree.sync()
            log.info("Synced global commands (can take a while to appear).")

    async def close(self) -> None:
        await self.db.close()
        await super().close()


async def main() -> None:
    config.validate_config()

    db = Database(config.DATABASE_URL)
    await db.connect()

    bot = ChallengeTicketBot(db)
    await bot.start(config.DISCORD_TOKEN)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
