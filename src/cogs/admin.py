import discord
from discord import app_commands
from discord.ext import commands

from ..config import OWNER_ID
from ..db import Database


class AdminCog(commands.Cog):
    def __init__(self, db: Database):
        self.db = db

    @app_commands.command(
        name="allow-bot",
        description="Enable this bot in the current server (owner only)",
    )
    async def allow_bot(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)
        if not interaction.user or interaction.user.id != OWNER_ID:
            return await interaction.response.send_message("Owner only.", ephemeral=True)

        await self.db.allow_guild(interaction.guild.id, interaction.user.id)
        await interaction.response.send_message("Bot enabled for this server ✅", ephemeral=True)

