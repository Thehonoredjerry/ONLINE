import os
import asyncio
import time
from threading import Thread

import discord
from discord import app_commands
from flask import Flask

# ── Environment variables ────────────────────────────────────────────────────
TOKEN            = os.environ["DISCORD_TOKEN"]
GUILD_ID         = int(os.environ["DISCORD_GUILD_ID"])
VOICE_CHANNEL_ID = int(os.environ["DISCORD_VOICE_CHANNEL_ID"])
PORT             = int(os.environ.get("PORT", 3000))

# ── Keep-alive HTTP server (required by Railway) ─────────────────────────────
http = Flask(__name__)

@http.route("/")
def health():
    return {"status": "ok", "uptime": int(time.time() - START_TIME)}

def run_http():
    http.run(host="0.0.0.0", port=PORT)

# ── Discord bot ───────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.voice_states = True

bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

START_TIME       = time.time()
manual_leave     = False
default_channel  = VOICE_CHANNEL_ID
retry_count      = 0

def next_retry_delay() -> float:
    """Exponential backoff: 10s → 20s → 40s … capped at 5 minutes."""
    global retry_count
    delay = min(10 * (2 ** retry_count), 300)
    retry_count += 1
    return delay

async def join_channel(channel_id: int, guild_id: int):
    global retry_count

    guild = bot.get_guild(guild_id)
    if not guild:
        print(f"[bot] Guild {guild_id} not found")
        return

    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.VoiceChannel):
        print(f"[bot] Channel {channel_id} is not a voice channel")
        return

    # Disconnect from any existing voice connection first
    if guild.voice_client:
        await guild.voice_client.disconnect(force=True)

    print(f"[bot] Joining: {channel.name}")
    try:
        await channel.connect()
        retry_count = 0
        print(f"[bot] Connected to: {channel.name}")
    except Exception as e:
        print(f"[bot] Failed to connect: {e}")
        if not manual_leave:
            delay = next_retry_delay()
            print(f"[bot] Retrying in {delay}s...")
            await asyncio.sleep(delay)
            await join_channel(default_channel, GUILD_ID)


@bot.event
async def on_ready():
    global manual_leave
    print(f"[bot] Logged in as {bot.user}")

    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.listening,
            name="in the voice chat 🎧"
        ),
        status=discord.Status.online,
    )

    # Sync slash commands to the guild
    guild_obj = discord.Object(id=GUILD_ID)
    tree.copy_global_to(guild=guild_obj)
    await tree.sync(guild=guild_obj)
    print("[bot] Slash commands synced")

    manual_leave = False
    await join_channel(VOICE_CHANNEL_ID, GUILD_ID)


@bot.event
async def on_voice_state_update(member, before, after):
    """Auto-rejoin if the bot itself gets disconnected."""
    if member != bot.user:
        return
    if before.channel and not after.channel and not manual_leave:
        print("[bot] Disconnected from voice — reconnecting...")
        delay = next_retry_delay()
        await asyncio.sleep(delay)
        await join_channel(default_channel, GUILD_ID)


# ── Slash commands ────────────────────────────────────────────────────────────

@tree.command(name="join", description="Bot joins the voice channel you are currently in")
async def join_cmd(interaction: discord.Interaction):
    global manual_leave, default_channel

    member = interaction.user
    if not isinstance(member, discord.Member) or not member.voice or not member.voice.channel:
        await interaction.response.send_message(
            "You need to be in a voice channel first!", ephemeral=True
        )
        return

    channel = member.voice.channel
    await interaction.response.defer()
    manual_leave = False
    default_channel = channel.id
    await join_channel(channel.id, interaction.guild_id)
    await interaction.followup.send(f"Joined **{channel.name}**! 🎧")


@tree.command(name="leave", description="Bot leaves the current voice channel")
async def leave_cmd(interaction: discord.Interaction):
    global manual_leave

    vc = interaction.guild.voice_client
    if not vc:
        await interaction.response.send_message(
            "I'm not in a voice channel right now.", ephemeral=True
        )
        return

    manual_leave = True
    await vc.disconnect(force=True)
    await interaction.response.send_message(
        "Left the voice channel. Use `/join` to bring me back."
    )


@tree.command(name="ping", description="Check bot latency")
async def ping_cmd(interaction: discord.Interaction):
    ms = round(bot.latency * 1000)
    await interaction.response.send_message(f"Pong! 🏓 Latency: **{ms}ms**")


@tree.command(name="uptime", description="Show how long the bot has been running")
async def uptime_cmd(interaction: discord.Interaction):
    total = int(time.time() - START_TIME)
    h, rem = divmod(total, 3600)
    m, s   = divmod(rem, 60)
    await interaction.response.send_message(f"⏱ Uptime: **{h}h {m}m {s}s**")


@tree.command(name="help", description="List all available commands")
async def help_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(
        "**Commands:**\n"
        "`/join` — Join your current voice channel\n"
        "`/leave` — Leave the voice channel\n"
        "`/ping` — Check latency\n"
        "`/uptime` — How long the bot has been running\n"
        "`/help` — Show this message"
    )


# ── Entry point ───────────────────────────────────────────────────────────────
Thread(target=run_http, daemon=True).start()
bot.run(TOKEN)
