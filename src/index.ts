import {
  Client,
  GatewayIntentBits,
  Events,
  ActivityType,
  GuildMember,
  REST,
  Routes,
  SlashCommandBuilder,
  ChatInputCommandInteraction,
} from "discord.js";
import {
  joinVoiceChannel,
  VoiceConnectionStatus,
  entersState,
  createAudioPlayer,
  NoSubscriberBehavior,
  getVoiceConnection,
} from "@discordjs/voice";
import express from "express";

const TOKEN = process.env.DISCORD_TOKEN!;
const GUILD_ID = process.env.DISCORD_GUILD_ID!;
const VOICE_CHANNEL_ID = process.env.DISCORD_VOICE_CHANNEL_ID!;
const PORT = process.env.PORT || 3000;

if (!TOKEN) throw new Error("DISCORD_TOKEN is not set");
if (!GUILD_ID) throw new Error("DISCORD_GUILD_ID is not set");
if (!VOICE_CHANNEL_ID) throw new Error("DISCORD_VOICE_CHANNEL_ID is not set");

// Keep-alive HTTP server required by Railway
const app = express();
app.get("/", (_req, res) => {
  res.json({ status: "ok", uptime: Math.floor(process.uptime()) });
});
app.listen(PORT, () => {
  console.log(`[keep-alive] Listening on port ${PORT}`);
});

// Slash command definitions
const commands = [
  new SlashCommandBuilder()
    .setName("join")
    .setDescription("Bot joins the voice channel you are currently in"),
  new SlashCommandBuilder()
    .setName("leave")
    .setDescription("Bot leaves the current voice channel"),
  new SlashCommandBuilder()
    .setName("ping")
    .setDescription("Check bot latency"),
  new SlashCommandBuilder()
    .setName("uptime")
    .setDescription("Show how long the bot has been running"),
  new SlashCommandBuilder()
    .setName("help")
    .setDescription("List all available commands"),
].map((cmd) => cmd.toJSON());

const client = new Client({
  intents: [
    GatewayIntentBits.Guilds,
    GatewayIntentBits.GuildVoiceStates,
  ],
});

const player = createAudioPlayer({
  behaviors: { noSubscriber: NoSubscriberBehavior.Pause },
});

let manualLeave = false;
let defaultChannelId = VOICE_CHANNEL_ID;
let retryCount = 0;

function getRetryDelay() {
  // Exponential backoff: 10s → 20s → 40s → ... capped at 5 minutes
  const delay = Math.min(10_000 * Math.pow(2, retryCount), 5 * 60 * 1000);
  retryCount++;
  return delay;
}

async function joinChannel(channelId: string, guildId: string) {
  const guild = client.guilds.cache.get(guildId);
  if (!guild) {
    console.error(`[bot] Guild ${guildId} not found`);
    return null;
  }

  const channel = guild.channels.cache.get(channelId);
  if (!channel || !channel.isVoiceBased()) {
    console.error(`[bot] Channel ${channelId} is not a voice channel`);
    return null;
  }

  // Destroy existing connection before joining
  const existing = getVoiceConnection(guildId);
  if (existing) existing.destroy();

  console.log(`[bot] Joining: ${channel.name}`);

  const connection = joinVoiceChannel({
    channelId,
    guildId,
    adapterCreator: guild.voiceAdapterCreator,
    selfDeaf: true,
    selfMute: true,
  });

  connection.subscribe(player);

  connection.on(VoiceConnectionStatus.Disconnected, async () => {
    if (manualLeave) return;
    console.warn("[bot] Disconnected — attempting to reconnect...");
    try {
      await Promise.race([
        entersState(connection, VoiceConnectionStatus.Signalling, 5_000),
        entersState(connection, VoiceConnectionStatus.Connecting, 5_000),
      ]);
    } catch {
      connection.destroy();
      const delay = getRetryDelay();
      console.log(`[bot] Reconnecting in ${delay / 1000}s...`);
      setTimeout(() => joinChannel(defaultChannelId, GUILD_ID), delay);
    }
  });

  connection.on(VoiceConnectionStatus.Ready, () => {
    retryCount = 0;
    console.log(`[bot] Connected to: ${channel.name}`);
  });

  try {
    await entersState(connection, VoiceConnectionStatus.Ready, 30_000);
    return connection;
  } catch {
    console.error("[bot] Could not connect within 30s");
    connection.destroy();
    if (!manualLeave) {
      const delay = getRetryDelay();
      console.log(`[bot] Retrying in ${delay / 1000}s...`);
      setTimeout(() => joinChannel(defaultChannelId, GUILD_ID), delay);
    }
    return null;
  }
}

async function registerCommands(clientId: string) {
  const rest = new REST().setToken(TOKEN);
  console.log("[bot] Registering slash commands...");
  await rest.put(Routes.applicationGuildCommands(clientId, GUILD_ID), {
    body: commands,
  });
  console.log("[bot] Slash commands registered");
}

client.once(Events.ClientReady, async (c) => {
  console.log(`[bot] Logged in as ${c.user.tag}`);

  c.user.setPresence({
    activities: [{ name: "in the voice chat 🎧", type: ActivityType.Listening }],
    status: "online",
  });

  try {
    await client.guilds.fetch(GUILD_ID);
  } catch {
    console.error(`[bot] Guild ${GUILD_ID} not found — make sure the bot is invited to your server`);
    return;
  }

  await registerCommands(c.user.id);
  await joinChannel(VOICE_CHANNEL_ID, GUILD_ID);
});

client.on(Events.InteractionCreate, async (interaction) => {
  if (!interaction.isChatInputCommand()) return;
  const cmd = interaction as ChatInputCommandInteraction;

  if (cmd.commandName === "join") {
    const member = cmd.member as GuildMember;
    const voiceChannel = member.voice.channel;
    if (!voiceChannel) {
      await cmd.reply({ content: "You need to be in a voice channel first!", ephemeral: true });
      return;
    }
    await cmd.deferReply();
    manualLeave = false;
    defaultChannelId = voiceChannel.id;
    await joinChannel(voiceChannel.id, cmd.guildId!);
    await cmd.editReply(`Joined **${voiceChannel.name}**! 🎧`);
    return;
  }

  if (cmd.commandName === "leave") {
    const connection = getVoiceConnection(cmd.guildId!);
    if (!connection) {
      await cmd.reply({ content: "I'm not in a voice channel right now.", ephemeral: true });
      return;
    }
    manualLeave = true;
    connection.destroy();
    await cmd.reply("Left the voice channel. Use `/join` to bring me back.");
    return;
  }

  if (cmd.commandName === "ping") {
    await cmd.reply(`Pong! 🏓 Latency: **${client.ws.ping}ms**`);
    return;
  }

  if (cmd.commandName === "uptime") {
    const secs = Math.floor(process.uptime());
    const h = Math.floor(secs / 3600);
    const m = Math.floor((secs % 3600) / 60);
    const s = secs % 60;
    await cmd.reply(`⏱ Uptime: **${h}h ${m}m ${s}s**`);
    return;
  }

  if (cmd.commandName === "help") {
    await cmd.reply(
      "**Commands:**\n" +
      "`/join` — Join your current voice channel\n" +
      "`/leave` — Leave the voice channel\n" +
      "`/ping` — Check latency\n" +
      "`/uptime` — How long the bot has been running\n" +
      "`/help` — Show this message"
    );
    return;
  }
});

client.login(TOKEN);
