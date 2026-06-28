from __future__ import annotations

import asyncio
import io
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from ..config import DASHBOARD_URL, OWNER_ID
from ..db import Database

log = logging.getLogger(__name__)


DEFAULT_CATEGORY_NAME = "Challenge Tickets"
MOBILE_CATEGORY_NAME = "Challenge Tickets - Top Mobile"
ALL_CATEGORY_NAME = "Challenge Tickets - Top All"
CLAN_CATEGORY_NAME = "Challenge Tickets - Top Clan"
CLOSED_MOBILE_CATEGORY_NAME = "Closed (Top Mobile)"
CLOSED_ALL_CATEGORY_NAME = "Closed (Top All)"
CLOSED_CLAN_CATEGORY_NAME = "Closed (Top Clan)"

PANEL_VIEW_CUSTOM_ID_PREFIX = "challenge_ticket_panel:"
PANEL_BTN_MOBILE_ID = f"{PANEL_VIEW_CUSTOM_ID_PREFIX}mobile"
PANEL_BTN_ALL_ID = f"{PANEL_VIEW_CUSTOM_ID_PREFIX}all"
PANEL_BTN_CLAN_ID = f"{PANEL_VIEW_CUSTOM_ID_PREFIX}clan"
DEFAULT_MESSAGE_TEMPLATES = {
    "panel_author": "เปิด challenge",
    "panel_title": "เลือก top mobile / top all / top clan",
    "panel_description": "เลือกประเภทให้ถูกก่อนเปิดตั๋ว",
    "button_mobile_label": "Top Mobile",
    "button_all_label": "Top All",
    "button_clan_label": "Top Clan",
    "button_mobile_emoji": "📱",
    "button_all_emoji": "🏆",
    "button_clan_emoji": "🛡️",
    "panel_locked": "0",
    "locked_message": "Ticket opening is currently locked.",
    "ticket_ping_message": "{staff} <@{opener_id}> <@{target_id}>",
    "ticket_open_message": "Challenge ticket opened: <@{opener_id}> vs <@{target_id}>{platform_suffix}",
    "ticket_created_reply": "Ticket created: {channel}\nOpen: {channel_url}",
    "close_confirm_message": "Are you sure you want to close this ticket?",
    "close_notice_message": "Ticket closed by <@{closer_id}>.\nStaff options:",
    "claim_success_message": "Claimed by <@{claimer_id}>.",
    "reopen_message": "Ticket reopened by <@{reopener_id}>.\n{staff} <@{opener_id}> <@{target_id}>",
}


def _is_admin(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    perms = interaction.user.guild_permissions
    return perms.administrator or perms.manage_guild


def _has_any_staff_role(member: discord.Member, staff_role_ids: list[int]) -> bool:
    return any(r.id in set(staff_role_ids) for r in member.roles)


def _sanitize_channel_name(name: str) -> str:
    name = name.lower()
    name = re.sub(r"[^a-z0-9\-]+", "-", name)
    name = re.sub(r"-+", "-", name).strip("-")
    return name[:90] or "challenge"


def _dashboard_hint() -> str:
    if DASHBOARD_URL:
        return f" Configure it in the dashboard: {DASHBOARD_URL}"
    return " Configure it in the website dashboard."


def _message_template(settings, key: str) -> str:
    return str(settings.message_templates.get(key) or DEFAULT_MESSAGE_TEMPLATES[key])


def _format_message(settings, key: str, **kwargs) -> str:
    template = _message_template(settings, key)
    safe_values = {k: ("" if v is None else str(v)) for k, v in kwargs.items()}
    try:
        return template.format(**safe_values)
    except KeyError:
        return template


def _setting_enabled(settings, key: str, default: bool = False) -> bool:
    raw = str(settings.message_templates.get(key, "1" if default else "0")).strip().lower()
    return raw in {"1", "true", "yes", "y", "on"}


class ChallengeOpenModal(discord.ui.Modal):
    def __init__(self, parent: "TicketGroup", platform: str, title: str):
        super().__init__(title=title)
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

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("modal_submit_failed guild=%s user=%s", getattr(interaction.guild, "id", None), getattr(interaction.user, "id", None), exc_info=error)
        if not interaction.response.is_done():
            await interaction.response.send_message("Something failed. Staff should check bot logs.", ephemeral=True)


class TopClanOpenModal(discord.ui.Modal):
    def __init__(self, parent: "TicketGroup", title: str):
        super().__init__(title=title)
        self.parent = parent

        self.opener_clan = discord.ui.TextInput(label="แคลนตัวเอง", required=True, max_length=80)
        self.target_clan = discord.ui.TextInput(label="แคลนที่ท้า", required=True, max_length=80)
        self.target_user_id = discord.ui.TextInput(
            label="user id หัวแคลนที่ท้า",
            placeholder="Paste Discord user ID (numbers)",
            required=True,
            max_length=25,
        )

        self.add_item(self.opener_clan)
        self.add_item(self.target_clan)
        self.add_item(self.target_user_id)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.parent._handle_open(
            interaction,
            target_user_id=str(self.target_user_id.value),
            platform="clan",
            challenge_data={
                "opener_clan": str(self.opener_clan.value).strip(),
                "target_clan": str(self.target_clan.value).strip(),
            },
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("modal_submit_failed guild=%s user=%s", getattr(interaction.guild, "id", None), getattr(interaction.user, "id", None), exc_info=error)
        if not interaction.response.is_done():
            await interaction.response.send_message("Something failed. Staff should check bot logs.", ephemeral=True)


class TicketPanelView(discord.ui.View):
    def __init__(self, parent: "TicketGroup", settings=None):
        super().__init__(timeout=None)
        self.parent = parent
        if settings is not None:
            self.mobile.label = f"{_message_template(settings, 'button_mobile_emoji')} {_message_template(settings, 'button_mobile_label')}".strip()
            self.all.label = f"{_message_template(settings, 'button_all_emoji')} {_message_template(settings, 'button_all_label')}".strip()
            self.clan.label = f"{_message_template(settings, 'button_clan_emoji')} {_message_template(settings, 'button_clan_label')}".strip()

    async def _guard_locked(self, interaction: discord.Interaction) -> bool:
        if not await self.parent._ensure_allowed(interaction):
            return False
        settings = await self.parent.db.get_guild_settings(interaction.guild.id)
        if _setting_enabled(settings, "panel_locked", False):
            await interaction.response.send_message(_message_template(settings, "locked_message"), ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Top Mobile", style=discord.ButtonStyle.primary, custom_id=PANEL_BTN_MOBILE_ID)
    async def mobile(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore[override]
        settings = await self.parent.db.get_guild_settings(interaction.guild.id)
        if not await self._guard_locked(interaction):
            return
        await interaction.response.send_modal(
            ChallengeOpenModal(self.parent, "mobile", _message_template(settings, "button_mobile_label"))
        )

    @discord.ui.button(label="Top All", style=discord.ButtonStyle.success, custom_id=PANEL_BTN_ALL_ID)
    async def all(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore[override]
        settings = await self.parent.db.get_guild_settings(interaction.guild.id)
        if not await self._guard_locked(interaction):
            return
        await interaction.response.send_modal(
            ChallengeOpenModal(self.parent, "all", _message_template(settings, "button_all_label"))
        )

    @discord.ui.button(label="Top Clan", style=discord.ButtonStyle.secondary, custom_id=PANEL_BTN_CLAN_ID)
    async def clan(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore[override]
        settings = await self.parent.db.get_guild_settings(interaction.guild.id)
        if not await self._guard_locked(interaction):
            return
        await interaction.response.send_modal(
            TopClanOpenModal(self.parent, _message_template(settings, "button_clan_label"))
        )


class TicketManageView(discord.ui.View):
    def __init__(self, parent: "TicketGroup", ticket_id: int):
        super().__init__(timeout=86400)
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

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.primary)
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore[override]
        if not await self.parent._ensure_allowed(interaction):
            return
        ticket, channel = await self._get_ticket_and_channel(interaction)
        if not ticket or not channel or not interaction.guild:
            return await self._deny(interaction, "This is not a ticket channel.")
        if ticket.status == "closed":
            return await self._deny(interaction, "This ticket is already closed.")
        if not await self.parent._is_staff(interaction):
            return await self._deny(interaction, "Only staff roles or the owner can claim.")
        if ticket.claimed_by:
            if ticket.claimed_by == interaction.user.id:
                return await self._deny(interaction, "You already claimed this ticket.")
            return await self._deny(interaction, f"This ticket is already claimed by <@{ticket.claimed_by}>.")

        await self.parent.db.claim_ticket(ticket.id, interaction.user.id)
        settings = await self.parent.db.get_guild_settings(interaction.guild.id)
        await interaction.response.send_message(
            _format_message(settings, "claim_success_message", claimer_id=interaction.user.id)
        )

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore[override]
        if not await self.parent._ensure_allowed(interaction):
            return
        ticket, channel = await self._get_ticket_and_channel(interaction)
        if not ticket or not channel:
            return await self._deny(interaction, "This is not a ticket channel.")
        if ticket.status == "closed":
            return await self._deny(interaction, "This ticket is already closed.")
        if not await self.parent._can_manage_ticket(interaction, ticket):
            return await self._deny(interaction, "Only the opener or staff can close this ticket.")

        settings = await self.parent.db.get_guild_settings(interaction.guild.id)
        await interaction.response.send_message(
            _format_message(settings, "close_confirm_message", opener_id=ticket.opener_id, target_id=ticket.target_id),
            ephemeral=True,
            view=ConfirmCloseView(self.parent, ticket_id=ticket.id),
        )


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
        elif platform == "all":
            wanted_name = ALL_CATEGORY_NAME
            wanted_id = settings.ticket_category_pc_id
        elif platform == "clan":
            wanted_name = CLAN_CATEGORY_NAME
            wanted_id = None
        else:
            # fallback: if platform isn't specified, prefer All category if set, else Mobile, else default
            wanted_id = settings.ticket_category_pc_id or settings.ticket_category_mobile_id
            if settings.ticket_category_pc_id:
                wanted_name = ALL_CATEGORY_NAME
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
                elif platform == "all":
                    await self.db.set_ticket_category_pc(guild.id, c.id)
                else:
                    if platform is None:
                        await self.db.set_ticket_category_mobile(guild.id, c.id)
                        await self.db.set_ticket_category_pc(guild.id, c.id)
                return c

        # Create
        category = await guild.create_category(wanted_name, reason="Ticket category setup")
        if platform == "mobile":
            await self.db.set_ticket_category_mobile(guild.id, category.id)
        elif platform == "all":
            await self.db.set_ticket_category_pc(guild.id, category.id)
        else:
            if platform is None:
                await self.db.set_ticket_category_mobile(guild.id, category.id)
                await self.db.set_ticket_category_pc(guild.id, category.id)
        return category

    async def _ensure_closed_category(self, guild: discord.Guild, platform: str | None) -> discord.CategoryChannel:
        wanted_name = CLOSED_ALL_CATEGORY_NAME
        if platform == "mobile":
            wanted_name = CLOSED_MOBILE_CATEGORY_NAME
        elif platform == "clan":
            wanted_name = CLOSED_CLAN_CATEGORY_NAME

        for c in guild.categories:
            if c.name == wanted_name:
                return c
        return await guild.create_category(wanted_name, reason="Closed ticket category setup")

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
        if await self._is_staff(interaction):
            return True
        if _is_admin(interaction):
            return True
        return False

    async def _is_staff(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        if interaction.user.id == OWNER_ID:
            return True
        settings = await self.db.get_guild_settings(interaction.guild.id)
        if settings.staff_role_ids and _has_any_staff_role(interaction.user, settings.staff_role_ids):
            return True
        return False

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

        settings = await self.db.get_guild_settings(interaction.guild.id)
        embed = discord.Embed(
            title=_message_template(settings, "panel_title"),
            description=_message_template(settings, "panel_description"),
            color=discord.Color.blurple(),
        )
        embed.set_author(name=_message_template(settings, "panel_author"))

        await interaction.response.send_message("Panel sent.", ephemeral=True)
        await interaction.channel.send(embed=embed, view=TicketPanelView(self, settings=settings))  # type: ignore[union-attr]

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
        target_member = interaction.guild.get_member(target_id)
        if target_member is None:
            try:
                target_member = await interaction.guild.fetch_member(target_id)
            except discord.NotFound:
                return await interaction.response.send_message(
                    "That user is not in this server.", ephemeral=True
                )
            except discord.Forbidden:
                return await interaction.response.send_message(
                    "I don't have permission to verify that user in this server.", ephemeral=True
                )
            except discord.HTTPException:
                return await interaction.response.send_message(
                    "Discord lookup failed. Try again in a moment.", ephemeral=True
                )

        category = await self._ensure_category(interaction.guild, platform)

        settings = await self.db.get_guild_settings(interaction.guild.id)
        staff_roles = [
            role for role_id in settings.staff_role_ids
            if (role := interaction.guild.get_role(role_id)) is not None
        ]

        opener_id = interaction.user.id
        opener_display = interaction.user.display_name
        target_display = target_member.display_name
        bot_member = interaction.guild.me or interaction.guild.get_member(self.bot.user.id if self.bot.user else 0)

        if platform == "clan":
            opener_clan = str(challenge_data.get("opener_clan") or opener_display)
            target_clan = str(challenge_data.get("target_clan") or target_display)
            channel_name = _sanitize_channel_name(f"{opener_clan}-vs-{target_clan}")
        else:
            channel_name = _sanitize_channel_name(f"{opener_display}-vs-{target_display}")

        overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            ),
            discord.Object(id=target_id): discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            ),
        }
        if bot_member is not None:
            overwrites[bot_member] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_channels=True,
                manage_messages=True,
                read_message_history=True,
            )
        for staff_role in staff_roles:
            overwrites[staff_role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_messages=True,
                read_message_history=True,
            )

        # Defer only if this came from a slash command; for modal submits the response may already be used.
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)

        channel: discord.TextChannel | None = None
        try:
            log.info("ticket_open_stage=create_channel guild=%s opener=%s target=%s platform=%s", interaction.guild.id, opener_id, target_id, platform)
            channel = await interaction.guild.create_text_channel(
                name=channel_name,
                category=category,
                overwrites=overwrites,
                reason=f"Challenge ticket opened by {interaction.user} against {target_id}",
            )

            log.info("ticket_open_stage=create_db guild=%s channel=%s", interaction.guild.id, channel.id)
            ticket = await self.db.create_ticket(
                guild_id=interaction.guild.id,
                channel_id=channel.id,
                opener_id=opener_id,
                target_id=target_id,
                platform=platform,
                challenge_data=challenge_data,
            )

            try:
                log.info("ticket_open_stage=ping guild=%s channel=%s", interaction.guild.id, channel.id)
                staff_ping = " ".join(role.mention for role in staff_roles).strip()
                await channel.send(
                    _format_message(
                        settings,
                        "ticket_ping_message",
                        staff=staff_ping,
                        opener_id=opener_id,
                        target_id=target_id,
                        channel=channel.mention,
                    ).strip(),
                    allowed_mentions=discord.AllowedMentions(users=True, roles=True, everyone=False),
                )
            except Exception as error:
                log.exception(
                    "ticket_open_stage_failed stage=ping guild=%s channel=%s",
                    interaction.guild.id,
                    channel.id,
                    exc_info=error,
                )

            try:
                log.info("ticket_open_stage=setup_message guild=%s channel=%s", interaction.guild.id, channel.id)
                embed = discord.Embed(
                    title="Challenge Ticket",
                    description=(
                        "Use this channel to discuss the challenge.\n\n"
                        "Use the buttons below or staff commands: `/ticket claim`, `/ticket add`, `/ticket close`"
                    ),
                    color=discord.Color.blurple(),
                )
                embed.add_field(name="Opener", value=f"<@{opener_id}>", inline=True)
                embed.add_field(name="Target", value=f"<@{target_id}>", inline=True)
                if platform:
                    embed.add_field(name="Platform", value=platform, inline=True)
                if platform == "clan":
                    embed.add_field(name="Opener Clan", value=str(challenge_data.get("opener_clan") or "-"), inline=True)
                    embed.add_field(name="Target Clan", value=str(challenge_data.get("target_clan") or "-"), inline=True)

                content = _format_message(
                    settings,
                    "ticket_open_message",
                    opener_id=opener_id,
                    target_id=target_id,
                    channel=channel.mention,
                    channel_url=channel.jump_url,
                    platform=platform or "",
                    platform_suffix=f" ({platform})" if platform else "",
                )
                await channel.send(
                    content=content,
                    embed=embed,
                    view=TicketManageView(self, ticket_id=ticket.id),
                    allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
                )
            except Exception as error:
                log.exception(
                    "ticket_open_stage_failed stage=setup_message guild=%s channel=%s",
                    interaction.guild.id,
                    channel.id,
                    exc_info=error,
                )
                try:
                    await channel.send("Ticket created, but the setup message failed. Staff should check bot logs.")
                except Exception:
                    pass

            try:
                log.info("ticket_open_stage=followup guild=%s channel=%s", interaction.guild.id, channel.id)
                reply_text = _format_message(
                    settings,
                    "ticket_created_reply",
                    channel=channel.mention,
                    channel_url=channel.jump_url,
                    opener_id=opener_id,
                    target_id=target_id,
                )
                await interaction.followup.send(reply_text, ephemeral=True)
            except Exception as error:
                log.exception(
                    "ticket_open_stage_failed stage=followup guild=%s channel=%s",
                    interaction.guild.id,
                    channel.id,
                    exc_info=error,
                )
        except Exception as error:
            log.exception(
                "ticket_open_fatal guild=%s opener=%s target=%s platform=%s channel=%s",
                interaction.guild.id,
                opener_id,
                target_id,
                platform,
                getattr(channel, "id", None),
                exc_info=error,
            )
            if channel is not None:
                try:
                    await channel.send("Ticket created, but the setup message failed. Staff should check bot logs.")
                except Exception:
                    pass
            try:
                await interaction.followup.send(
                    "The ticket channel was created, but setup failed. Please tell staff to check the bot logs.",
                    ephemeral=True,
                )
            except Exception:
                pass

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

        settings = await self.db.get_guild_settings(interaction.guild.id)
        if not settings.staff_role_ids:
            return await interaction.response.send_message(
                f"Staff role is not set.{_dashboard_hint()}",
                ephemeral=True,
            )
        if not await self._is_staff(interaction):
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

        settings = await self.db.get_guild_settings(interaction.guild.id)
        if not settings.staff_role_ids:
            return await interaction.response.send_message(
                f"Staff role is not set.{_dashboard_hint()}",
                ephemeral=True,
            )
        if not await self._is_staff(interaction):
            return await interaction.response.send_message("Only staff roles or the owner can claim.", ephemeral=True)

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
        if ticket.claimed_by:
            if ticket.claimed_by == interaction.user.id:
                return await interaction.response.send_message("You already claimed this ticket.", ephemeral=True)
            return await interaction.response.send_message(
                f"This ticket is already claimed by <@{ticket.claimed_by}>.", ephemeral=True
            )

        await self.db.claim_ticket(ticket.id, interaction.user.id)
        await interaction.response.send_message(
            _format_message(settings, "claim_success_message", claimer_id=interaction.user.id)
        )

    @app_commands.command(
        name="unclaim",
        description="Unclaim the current ticket (only the claimer)",
    )
    async def unclaim(self, interaction: discord.Interaction):
        if not await self._ensure_allowed(interaction):
            return
        if not interaction.guild:
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)
        if not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Could not read your member info.", ephemeral=True)

        channel = await self._get_ticket_channel(interaction)
        if not channel:
            return await interaction.response.send_message("Use this inside a ticket channel.", ephemeral=True)

        ticket = await self.db.get_ticket_by_channel(channel.id)
        if not ticket:
            return await interaction.response.send_message(
                "This channel is not a tracked ticket.", ephemeral=True
            )
        if not ticket.claimed_by:
            return await interaction.response.send_message("This ticket is not currently claimed.", ephemeral=True)
        if ticket.claimed_by != interaction.user.id:
            return await interaction.response.send_message(
                "Only the staff member who claimed this ticket can unclaim it.", ephemeral=True
            )

        await self.db.unclaim_ticket(ticket.id)
        await interaction.response.send_message("Ticket unclaimed.")

    @app_commands.command(
        name="force-unclaim",
        description="Owner only: force unclaim the current ticket",
    )
    async def force_unclaim(self, interaction: discord.Interaction):
        if not await self._ensure_allowed(interaction):
            return
        if not interaction.guild:
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)
        if interaction.user.id != OWNER_ID:
            return await interaction.response.send_message("Owner only.", ephemeral=True)

        channel = await self._get_ticket_channel(interaction)
        if not channel:
            return await interaction.response.send_message("Use this inside a ticket channel.", ephemeral=True)

        ticket = await self.db.get_ticket_by_channel(channel.id)
        if not ticket:
            return await interaction.response.send_message(
                "This channel is not a tracked ticket.", ephemeral=True
            )
        if not ticket.claimed_by:
            return await interaction.response.send_message("This ticket is not currently claimed.", ephemeral=True)

        claimed_by = ticket.claimed_by
        await self.db.unclaim_ticket(ticket.id)
        await interaction.response.send_message(f"Force-unclaimed ticket from <@{claimed_by}>.")

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
        settings = await self.db.get_guild_settings(interaction.guild.id)
        await interaction.response.send_message(
            _format_message(settings, "close_confirm_message", opener_id=ticket.opener_id, target_id=ticket.target_id),
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

        try:
            closed_category = await self._ensure_closed_category(interaction.guild, ticket.platform)
            await interaction.channel.edit(category=closed_category, reason=f"Ticket closed by {interaction.user}")
        except discord.HTTPException:
            pass

        settings = await self.db.get_guild_settings(interaction.guild.id)
        await interaction.channel.send(
            _format_message(
                settings,
                "close_notice_message",
                closer_id=interaction.user.id,
                opener_id=ticket.opener_id,
                target_id=ticket.target_id,
            ),
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
                f"Transcript channel is not set.{_dashboard_hint()}",
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

        try:
            open_category = await self.parent._ensure_category(interaction.guild, ticket.platform)
            await channel.edit(category=open_category, reason=f"Ticket reopened by {interaction.user}")
        except discord.HTTPException:
            pass

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
        staff_roles = [
            role for role_id in settings.staff_role_ids
            if (role := interaction.guild.get_role(role_id)) is not None
        ]
        staff_ping = " ".join(role.mention for role in staff_roles).strip()
        await channel.send(
            _format_message(
                settings,
                "reopen_message",
                reopener_id=interaction.user.id,
                opener_id=ticket.opener_id,
                target_id=ticket.target_id,
                staff=staff_ping,
                channel=channel.mention,
            ),
            allowed_mentions=discord.AllowedMentions(users=True, roles=True, everyone=False),
        )


async def setup(bot: commands.Bot, db: Database) -> None:
    # Register the slash-command group
    group = TicketGroup(bot, db)
    bot.tree.add_command(group)
    await group.add_persistent_views()
