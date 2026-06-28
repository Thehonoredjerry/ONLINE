from __future__ import annotations

import asyncio
import io
import re
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from ..db import Database


DEFAULT_CATEGORY_NAME = "Challenge Tickets"
MOBILE_CATEGORY_NAME = "Challenge Tickets - Mobile"
PC_CATEGORY_NAME = "Challenge Tickets - PC"
PANEL_EMBED_TEMPLATE = {
    "title": "เลือก mobile / pc",
    "description": "พร้อมใส่ user id ให้ครบ",
    "author": {"name": "เปิด challenge"},
}

PANEL_VIEW_CUSTOM_ID_PREFIX = "challenge_ticket_panel:"
PANEL_BTN_MOBILE_ID = f"{PANEL_VIEW_CUSTOM_ID_PREFIX}mobile"
PANEL_BTN_PC_ID = f"{PANEL_VIEW_CUSTOM_ID_PREFIX}pc"


def _is_admin(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    perms = interaction.user.guild_permissions
    return perms.administrator or perms.manage_guild


async def _require_staff_role_id(db: Database, guild_id: int) -> int | None:
    settings = await db.get_guild_settings(guild_id)
    return settings.staff_role_id


def _has_staff_role(member: discord.Member, staff_role_id: int) -> bool:
    return any(r.id == staff_role_id for r in member.roles)


def _sanitize_channel_name(name: str) -> str:
    name = name.lower()
    name = re.sub(r"[^a-z0-9\-]+", "-", name)
    name = re.sub(r"-+", "-", name).strip("-")
    return name[:90] or "challenge"


class ChallengeOpenModal(discord.ui.Modal):
    def __init__(self, parent: "TicketGroup", platform: str):
        super().__init__(title=f"Open challenge ({platform})")
        self.parent = parent
        self.platform = platform

        self.target_user_id = discord.ui.TextInput(
            label="Target user ID",
            placeholder="Paste Discord user ID (numbers)",
            required=True,
            max_length=25,
        )

        self.add_item(self.target_user_id)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.parent._handle_open(
            interaction,
            target_user_id=str(self.target_user_id.value),
            platform=self.platform,
            challenge_data={},
        )


class TicketPanelView(discord.ui.View):
    def __init__(self, parent: "TicketGroup"):
        super().__init__(timeout=None)
        self.parent = parent

    @discord.ui.button(label="Mobile", style=discord.ButtonStyle.primary, custom_id=PANEL_BTN_MOBILE_ID)
    async def mobile(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore[override]
        if not await self.parent._ensure_allowed(interaction):
            return
        await interaction.response.send_modal(ChallengeOpenModal(self.parent, "mobile"))

    @discord.ui.button(label="PC", style=discord.ButtonStyle.success, custom_id=PANEL_BTN_PC_ID)
    async def pc(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore[override]
        if not await self.parent._ensure_allowed(interaction):
            return
        await interaction.response.send_modal(ChallengeOpenModal(self.parent, "pc"))


class TicketGroup(app_commands.Group):
    def __init__(self, bot: commands.Bot, db: Database):
        super().__init__(name="ticket", description="Challenge ticket system")
        self.bot = bot
        self.db = db

    async def _ensure_category(self, guild: discord.Guild, platform: str | None) -> discord.CategoryChannel:
        settings = await self.db.get_guild_settings(guild.id)

        category: Optional[discord.CategoryChannel] = None
        wanted_name = DEFAULT_CATEGORY_NAME
        wanted_id: int | None = None
        if platform == "mobile":
            wanted_name = MOBILE_CATEGORY_NAME
            wanted_id = settings.ticket_category_mobile_id
        elif platform == "pc":
            wanted_name = PC_CATEGORY_NAME
            wanted_id = settings.ticket_category_pc_id
        else:
            # fallback: if platform isn't specified, prefer PC category if set, else Mobile, else default
            wanted_id = settings.ticket_category_pc_id or settings.ticket_category_mobile_id
            if settings.ticket_category_pc_id:
                wanted_name = PC_CATEGORY_NAME
            elif settings.ticket_category_mobile_id:
                wanted_name = MOBILE_CATEGORY_NAME

        if wanted_id:
            category = guild.get_channel(wanted_id)  # type: ignore[assignment]

        if category and isinstance(category, discord.CategoryChannel):
            return category

        # Try to find by name
        for c in guild.categories:
            if c.name == wanted_name:
                if platform == "mobile":
                    await self.db.set_ticket_category_mobile(guild.id, c.id)
                elif platform == "pc":
                    await self.db.set_ticket_category_pc(guild.id, c.id)
                else:
                    # store as both if none specified
                    await self.db.set_ticket_category_mobile(guild.id, c.id)
                    await self.db.set_ticket_category_pc(guild.id, c.id)
                return c

        # Create
        category = await guild.create_category(wanted_name, reason="Ticket category setup")
        if platform == "mobile":
            await self.db.set_ticket_category_mobile(guild.id, category.id)
        elif platform == "pc":
            await self.db.set_ticket_category_pc(guild.id, category.id)
        else:
            await self.db.set_ticket_category_mobile(guild.id, category.id)
            await self.db.set_ticket_category_pc(guild.id, category.id)
        return category

    async def _get_ticket_channel(self, interaction: discord.Interaction) -> discord.TextChannel | None:
        if not interaction.channel or not isinstance(interaction.channel, discord.TextChannel):
            return None
        return interaction.channel

    async def add_persistent_views(self) -> None:
        # Persistent view so buttons keep working after bot restarts
        self.bot.add_view(TicketPanelView(self))

    async def _ensure_allowed(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            if not interaction.response.is_done():
                await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return False
        if not await self.db.is_guild_allowed(interaction.guild.id):
            msg = "Bot is not enabled in this server. Ask the owner to run `/allow-bot`."
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
            return False
        return True

    async def _can_manage_ticket(self, interaction: discord.Interaction, ticket) -> bool:
        # opener OR staff OR admin
        if not interaction.guild:
            return False
        if interaction.user and getattr(interaction.user, "id", None) == ticket.opener_id:
            return True
        if isinstance(interaction.user, discord.Member):
            settings = await self.db.get_guild_settings(interaction.guild.id)
            if settings.staff_role_id and _has_staff_role(interaction.user, settings.staff_role_id):
                return True
            if _is_admin(interaction):
                return True
        return False

    async def _is_staff(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        settings = await self.db.get_guild_settings(interaction.guild.id)
        if settings.staff_role_id and _has_staff_role(interaction.user, settings.staff_role_id):
            return True
        return _is_admin(interaction)

    @app_commands.command(
        name="set_staff_role",
        description="Set the staff role that can manage/claim/close tickets",
    )
    @app_commands.describe(role="Role that can manage tickets")
    async def set_staff_role(self, interaction: discord.Interaction, role: discord.Role):
        if not await self._ensure_allowed(interaction):
            return
        if not interaction.guild:
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)
        if not _is_admin(interaction):
            return await interaction.response.send_message(
                "You need Manage Server (or Admin) to do this.", ephemeral=True
            )

        await self.db.set_staff_role(interaction.guild.id, role.id)
        await interaction.response.send_message(
            f"Staff role set to {role.mention}.", ephemeral=True
        )

    @app_commands.command(
        name="set_category",
        description="Set the category where tickets are created (mobile/pc)",
    )
    @app_commands.describe(platform="Which platform category to use", category="Category for ticket channels")
    @app_commands.choices(
        platform=[
            app_commands.Choice(name="mobile", value="mobile"),
            app_commands.Choice(name="pc", value="pc"),
        ]
    )
    async def set_category(
        self,
        interaction: discord.Interaction,
        platform: app_commands.Choice[str],
        category: discord.CategoryChannel,
    ):
        if not await self._ensure_allowed(interaction):
            return
        if not interaction.guild:
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)
        if not _is_admin(interaction):
            return await interaction.response.send_message(
                "You need Manage Server (or Admin) to do this.", ephemeral=True
            )

        if platform.value == "mobile":
            await self.db.set_ticket_category_mobile(interaction.guild.id, category.id)
        else:
            await self.db.set_ticket_category_pc(interaction.guild.id, category.id)
        await interaction.response.send_message(
            f"Ticket category for **{platform.value}** set to **{category.name}**.", ephemeral=True
        )

    @app_commands.command(
        name="set_transcript_channel",
        description="Set the channel where transcripts will be saved (admin)",
    )
    @app_commands.describe(channel="Channel to receive transcripts")
    async def set_transcript_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not await self._ensure_allowed(interaction):
            return
        if not interaction.guild:
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)
        if not _is_admin(interaction):
            return await interaction.response.send_message(
                "You need Manage Server (or Admin) to do this.", ephemeral=True
            )

        await self.db.set_transcript_channel(interaction.guild.id, channel.id)
        await interaction.response.send_message(
            f"Transcript channel set to {channel.mention}.", ephemeral=True
        )

    @app_commands.command(
        name="panel",
        description="Send the challenge ticket panel (buttons) to the current channel (admin)",
    )
    async def panel(self, interaction: discord.Interaction):
        if not await self._ensure_allowed(interaction):
            return
        if not interaction.guild:
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)
        if not _is_admin(interaction):
            return await interaction.response.send_message(
                "You need Manage Server (or Admin) to do this.", ephemeral=True
            )

        embed = discord.Embed(
            title=PANEL_EMBED_TEMPLATE["title"],
            description=PANEL_EMBED_TEMPLATE["description"],
            color=discord.Color.blurple(),
        )
        embed.set_author(name=PANEL_EMBED_TEMPLATE["author"]["name"])

        await interaction.response.send_message("Panel sent.", ephemeral=True)
        await interaction.channel.send(embed=embed, view=TicketPanelView(self))  # type: ignore[union-attr]

    @app_commands.command(
        name="open",
        description="(Staff) Open a challenge ticket manually (backup)",
    )
    @app_commands.describe(
        target_user_id="The user ID of the person you want to challenge (copy ID from Discord)"
    )
    async def open_ticket(self, interaction: discord.Interaction, target_user_id: str):
        if not await self._ensure_allowed(interaction):
            return
        if not await self._is_staff(interaction):
            return await interaction.response.send_message(
                "Staff only. Users should open tickets using the panel button.", ephemeral=True
            )
        await self._handle_open(
            interaction,
            target_user_id=target_user_id,
            platform=None,
            challenge_data={},
        )

    async def _handle_open(
        self,
        interaction: discord.Interaction,
        target_user_id: str,
        platform: str | None,
        challenge_data: dict,
    ):
        if not await self._ensure_allowed(interaction):
            return
        if not interaction.guild:
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)
        if not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Could not read your member info.", ephemeral=True)

        target_user_id = target_user_id.strip()
        if not target_user_id.isdigit():
            return await interaction.response.send_message(
                "That doesn't look like a user ID (numbers only).", ephemeral=True
            )
        target_id = int(target_user_id)
        if interaction.guild.get_member(target_id) is None:
            return await interaction.response.send_message(
                "That user is not in this server (or I can't see them).", ephemeral=True
            )

        category = await self._ensure_category(interaction.guild, platform)

        staff_role_id = await _require_staff_role_id(self.db, interaction.guild.id)
        staff_role = interaction.guild.get_role(staff_role_id) if staff_role_id else None

        opener_id = interaction.user.id
        opener_display = interaction.user.display_name

        channel_name = _sanitize_channel_name(f"challenge-{opener_display}-{target_id}")

        overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_channels=True,
                manage_messages=True,
                read_message_history=True,
            ),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            ),
            discord.Object(id=target_id): discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            ),
        }
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_messages=True,
                read_message_history=True,
            )

        # Defer only if this came from a slash command; for modal submits the response may already be used.
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)

        channel = await interaction.guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            reason=f"Challenge ticket opened by {interaction.user} against {target_id}",
        )

        await self.db.create_ticket(
            guild_id=interaction.guild.id,
            channel_id=channel.id,
            opener_id=opener_id,
            target_id=target_id,
            platform=platform,
            challenge_data=challenge_data,
        )

        # Initial messages inside the ticket (ping staff + both users)
        staff_ping = staff_role.mention if staff_role else ""
        await channel.send(f"{staff_ping} <@{opener_id}> <@{target_id}>")

        header = f"Challenge ticket opened: <@{opener_id}> vs <@{target_id}>"
        if platform:
            header += f" (**{platform}**)"
        await channel.send(header)
        embed = discord.Embed(
            title="Challenge Ticket",
            description=(
                "Use this channel to discuss the challenge.\n\n"
                "**Staff commands:** `/ticket claim`, `/ticket add`, `/ticket close`"
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Opener", value=f"<@{opener_id}>", inline=True)
        embed.add_field(name="Target", value=f"<@{target_id}>", inline=True)
        if platform:
            embed.add_field(name="Platform", value=platform, inline=True)
        await channel.send(embed=embed)

        if interaction.response.is_done():
            await interaction.followup.send(f"Ticket created: {channel.mention}", ephemeral=True)
        else:
            await interaction.response.send_message(f"Ticket created: {channel.mention}", ephemeral=True)

    @app_commands.command(
        name="add",
        description="Add a user to the current ticket (staff only)",
    )
    @app_commands.describe(user="User to add")
    async def add_user(self, interaction: discord.Interaction, user: discord.User):
        if not await self._ensure_allowed(interaction):
            return
        if not interaction.guild:
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)
        if not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Could not read your member info.", ephemeral=True)

        staff_role_id = await _require_staff_role_id(self.db, interaction.guild.id)
        if not staff_role_id:
            return await interaction.response.send_message(
                "Staff role is not set. Use `/ticket set_staff_role` first.", ephemeral=True
            )
        if not _has_staff_role(interaction.user, staff_role_id) and not _is_admin(interaction):
            return await interaction.response.send_message("Staff only.", ephemeral=True)

        channel = await self._get_ticket_channel(interaction)
        if not channel:
            return await interaction.response.send_message("Use this inside a ticket channel.", ephemeral=True)

        ticket = await self.db.get_ticket_by_channel(channel.id)
        if not ticket:
            return await interaction.response.send_message(
                "This channel is not a tracked ticket.", ephemeral=True
            )

        await channel.set_permissions(
            discord.Object(id=user.id),
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            reason=f"Added to ticket by {interaction.user}",
        )
        await interaction.response.send_message(f"Added <@{user.id}> to this ticket.")

    @app_commands.command(
        name="claim",
        description="Claim the current ticket (staff only)",
    )
    async def claim(self, interaction: discord.Interaction):
        if not await self._ensure_allowed(interaction):
            return
        if not interaction.guild:
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)
        if not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Could not read your member info.", ephemeral=True)

        staff_role_id = await _require_staff_role_id(self.db, interaction.guild.id)
        if not staff_role_id:
            return await interaction.response.send_message(
                "Staff role is not set. Use `/ticket set_staff_role` first.", ephemeral=True
            )
        if not _has_staff_role(interaction.user, staff_role_id) and not _is_admin(interaction):
            return await interaction.response.send_message("Staff only.", ephemeral=True)

        channel = await self._get_ticket_channel(interaction)
        if not channel:
            return await interaction.response.send_message("Use this inside a ticket channel.", ephemeral=True)

        ticket = await self.db.get_ticket_by_channel(channel.id)
        if not ticket:
            return await interaction.response.send_message(
                "This channel is not a tracked ticket.", ephemeral=True
            )
        if ticket.status == "closed":
            return await interaction.response.send_message("This ticket is already closed.", ephemeral=True)

        await self.db.claim_ticket(ticket.id, interaction.user.id)
        await interaction.response.send_message(f"Claimed by {interaction.user.mention}.")

    @app_commands.command(
        name="close",
        description="Close the current ticket (opener or staff)",
    )
    async def close(self, interaction: discord.Interaction):
        if not await self._ensure_allowed(interaction):
            return
        if not interaction.guild:
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)

        channel = await self._get_ticket_channel(interaction)
        if not channel:
            return await interaction.response.send_message("Use this inside a ticket channel.", ephemeral=True)

        ticket = await self.db.get_ticket_by_channel(channel.id)
        if not ticket:
            return await interaction.response.send_message(
                "This channel is not a tracked ticket.", ephemeral=True
            )
        if ticket.status == "closed":
            return await interaction.response.send_message("This ticket is already closed.", ephemeral=True)

        if not await self._can_manage_ticket(interaction, ticket):
            return await interaction.response.send_message(
                "Only the opener or staff can close this ticket.", ephemeral=True
            )

        # Confirmation before closing
        await interaction.response.send_message(
            "Are you sure you want to close this ticket?",
            ephemeral=True,
            view=ConfirmCloseView(self, ticket_id=ticket.id),
        )

    async def _perform_close(self, interaction: discord.Interaction, ticket_id: int) -> None:
        # Called from the confirmation view
        if not interaction.guild or not interaction.channel or not isinstance(interaction.channel, discord.TextChannel):
            return

        ticket = await self.db.get_ticket_by_channel(interaction.channel.id)
        if not ticket or ticket.id != ticket_id:
            return
        if ticket.status == "closed":
            return

        if not await self._can_manage_ticket(interaction, ticket):
            return

        await self.db.close_ticket(ticket.id)

        # Hide from both users; staff stays
        try:
            await interaction.channel.set_permissions(discord.Object(id=ticket.opener_id), view_channel=False)
            await interaction.channel.set_permissions(discord.Object(id=ticket.target_id), view_channel=False)
        except discord.HTTPException:
            pass

        await interaction.channel.send(
            f"Ticket closed by <@{interaction.user.id}>.\nStaff options:",
            view=CloseOptionsView(self, ticket_id=ticket.id),
        )


class ConfirmCloseView(discord.ui.View):
    def __init__(self, parent: TicketGroup, ticket_id: int):
        super().__init__(timeout=60)
        self.parent = parent
        self.ticket_id = ticket_id

    @discord.ui.button(label="Confirm close", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore[override]
        await interaction.response.defer(ephemeral=True, thinking=False)
        await self.parent._perform_close(interaction, self.ticket_id)
        try:
            await interaction.followup.send("Ticket closed.", ephemeral=True)
        except discord.HTTPException:
            pass
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore[override]
        await interaction.response.send_message("Cancelled.", ephemeral=True)
        self.stop()


class CloseOptionsView(discord.ui.View):
    def __init__(self, parent: TicketGroup, ticket_id: int):
        super().__init__(timeout=3600)
        self.parent = parent
        self.ticket_id = ticket_id

    async def _get_ticket_and_channel(self, interaction: discord.Interaction):
        if not interaction.guild or not interaction.channel or not isinstance(interaction.channel, discord.TextChannel):
            return None, None
        ticket = await self.parent.db.get_ticket_by_channel(interaction.channel.id)
        if not ticket or ticket.id != self.ticket_id:
            return None, interaction.channel
        return ticket, interaction.channel

    async def _deny(self, interaction: discord.Interaction, msg: str):
        if not interaction.response.is_done():
            await interaction.response.send_message(msg, ephemeral=True)
        else:
            await interaction.followup.send(msg, ephemeral=True)

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger, custom_id="ticket_close_opt:delete")
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore[override]
        if not await self.parent._ensure_allowed(interaction):
            return
        ticket, channel = await self._get_ticket_and_channel(interaction)
        if not ticket or not channel:
            return await self._deny(interaction, "This is not a ticket channel.")
        if not await self.parent._is_staff(interaction):
            return await self._deny(interaction, "Staff only.")

        await interaction.response.send_message("Deleting ticket channel...", ephemeral=True)
        try:
            await channel.delete(reason=f"Ticket deleted by {interaction.user}")
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Save transcript", style=discord.ButtonStyle.secondary, custom_id="ticket_close_opt:transcript")
    async def transcript(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore[override]
        if not await self.parent._ensure_allowed(interaction):
            return
        ticket, channel = await self._get_ticket_and_channel(interaction)
        if not ticket or not channel or not interaction.guild:
            return await self._deny(interaction, "This is not a ticket channel.")
        if not await self.parent._is_staff(interaction):
            return await self._deny(interaction, "Staff only.")

        settings = await self.parent.db.get_guild_settings(interaction.guild.id)
        if not settings.transcript_channel_id:
            return await self._deny(
                interaction,
                "Transcript channel is not set. Use `/ticket set_transcript_channel`.",
            )
        transcript_channel = interaction.guild.get_channel(settings.transcript_channel_id)
        if not isinstance(transcript_channel, discord.TextChannel):
            return await self._deny(interaction, "Transcript channel not found.")

        await interaction.response.defer(ephemeral=True, thinking=True)

        # Build transcript (requires Message Content Intent enabled in the bot settings)
        lines: list[str] = []
        header = (
            f"Ticket Transcript\n"
            f"Guild: {interaction.guild.name} ({interaction.guild.id})\n"
            f"Channel: #{channel.name} ({channel.id})\n"
            f"Opener: {ticket.opener_id}\n"
            f"Target: {ticket.target_id}\n"
            f"Platform: {ticket.platform or ''}\n"
            f"Exported at: {datetime.now(timezone.utc).isoformat()}\n"
            f"{'-'*60}\n"
        )
        lines.append(header)

        async for msg in channel.history(limit=5000, oldest_first=True):
            ts = msg.created_at.replace(tzinfo=timezone.utc).isoformat()
            author = f"{msg.author} ({msg.author.id})"
            content = msg.content or ""
            attach = ""
            if msg.attachments:
                attach = " | attachments: " + ", ".join(a.url for a in msg.attachments)
            lines.append(f"[{ts}] {author}: {content}{attach}")

        data = "\n".join(lines).encode("utf-8", errors="replace")
        file = discord.File(fp=io.BytesIO(data), filename=f"ticket-{channel.id}-transcript.txt")

        await transcript_channel.send(
            content=f"Transcript saved for {channel.mention} (opener <@{ticket.opener_id}>, target <@{ticket.target_id}>)",
            file=file,
        )
        await interaction.followup.send(f"Transcript saved in {transcript_channel.mention}.", ephemeral=True)

    @discord.ui.button(label="Reopen", style=discord.ButtonStyle.success, custom_id="ticket_close_opt:reopen")
    async def reopen(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore[override]
        if not await self.parent._ensure_allowed(interaction):
            return
        ticket, channel = await self._get_ticket_and_channel(interaction)
        if not ticket or not channel or not interaction.guild:
            return await self._deny(interaction, "This is not a ticket channel.")
        if not await self.parent._is_staff(interaction):
            return await self._deny(interaction, "Staff only.")

        # Restore perms for both users
        try:
            await channel.set_permissions(
                discord.Object(id=ticket.opener_id),
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                reason=f"Ticket reopened by {interaction.user}",
            )
            await channel.set_permissions(
                discord.Object(id=ticket.target_id),
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                reason=f"Ticket reopened by {interaction.user}",
            )
        except discord.HTTPException:
            pass

        # Reopen in DB (simple update)
        await self.parent.db.reopen_ticket(ticket.id)
        await interaction.response.send_message("Ticket reopened.", ephemeral=True)
        # Ping staff + both users again
        settings = await self.parent.db.get_guild_settings(interaction.guild.id)
        staff_role = interaction.guild.get_role(settings.staff_role_id) if settings.staff_role_id else None
        staff_ping = staff_role.mention if staff_role else ""
        await channel.send(
            f"Ticket reopened by <@{interaction.user.id}>.\n{staff_ping} <@{ticket.opener_id}> <@{ticket.target_id}>"
        )


async def setup(bot: commands.Bot, db: Database) -> None:
    # Register the slash-command group
    group = TicketGroup(bot, db)
    bot.tree.add_command(group)
    await group.add_persistent_views()
