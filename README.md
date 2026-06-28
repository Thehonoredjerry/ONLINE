# Discord Challenge Ticket Bot (Railway + PostgreSQL)

A Discord slash-command bot that opens **private challenge tickets**:
- User runs `/ticket open` and **inputs a target user ID**
- Bot creates a private channel, adds both users, and pings them
- Staff can `/ticket claim`, `/ticket add`, `/ticket close`
- Settings and ticket records are stored in **Railway PostgreSQL**

## 1) Create the Discord bot
1. Go to the Discord Developer Portal → Applications → **New Application**
2. Bot → **Add Bot**
3. Copy the bot token → set as `DISCORD_TOKEN`
4. Bot → **Privileged Gateway Intents** → enable:
   - **Message Content Intent** (needed for transcripts)
   - **Server Members Intent** (recommended for reliable user/member checks)
4. OAuth2 → URL Generator:
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions (recommended): `Manage Channels`, `Manage Roles`, `Read Messages/View Channels`, `Send Messages`, `Manage Messages`
5. Invite the bot to your server using the generated URL

## 2) Local run (optional)
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m src.main
```

## 3) Railway deploy
1. Create a new Railway project
2. Add a **PostgreSQL** database
3. Add a **Service** from this repo (GitHub) or upload code
4. Set Railway Variables:
   - `DISCORD_TOKEN`
   - `DATABASE_URL` (Railway provides this for the Postgres plugin)
   - (optional) `DEV_GUILD_ID` for faster command syncing
   - (optional) `DASHBOARD_URL` to point users/admins to your website dashboard
5. Set Start Command:
   - `python -m src.main`

## 4) First-time setup in your server
**Important:** the bot only works in servers you explicitly enable with `/allow-bot` (owner only).

All settings should be managed from your website/dashboard:
- staff role IDs
- top mobile category ID
- top all category ID
- transcript channel ID
- panel text
- button labels / emoji
- panel lock toggle and locked message
- custom ticket messages

## Commands
- `/allow-bot` → enable the bot in the current server (owner only)
- `/ticket open target_user_id:<id>` → (staff only) manual/backup way to create a ticket
- `/ticket panel` → send a message with **Top Mobile / Top All / Top Clan** buttons that opens a modal (admin)
- `/ticket add user:<user>` → add extra user (staff only)
- `/ticket claim` → mark as claimed (staff roles or owner only)
- `/ticket unclaim` → only the staff member who claimed the ticket can unclaim it
- `/ticket force-unclaim` → owner only, removes any existing claim
- `/ticket close` → asks confirmation, then closes ticket (opener or staff). After close: **only staff** can see the channel and choose: Delete / Save transcript / Reopen.

## Notes / Tips
- To copy a user ID: enable Developer Mode in Discord → right-click user → **Copy ID**
- Global slash commands can take time to appear. For testing, set `DEV_GUILD_ID` to your server ID (guild-only sync).
 - Recommended flow: run `/ticket panel` in the channel where you want users to open challenges, then users click **Top Mobile**, **Top All**, or **Top Clan**.
 - `Top Mobile` and `Top All` only ask for **Target user ID**.
 - `Top Clan` asks for:
   - `แคลนตัวเอง`
   - `แคลนที่ท้า`
   - `user id หัวแคลนที่ท้า`
 - When a ticket is created, the bot pings: **all staff roles + opener + target** inside the ticket.
 - Ticket channels are named like `opener-vs-target`, or `openerclan-vs-targetclan` for clan tickets.
 - A ticket can only be claimed by one staff member at a time.
 - Closed tickets are moved into auto-created categories like `Closed (Top Mobile)`, `Closed (Top All)`, or `Closed (Top Clan)`.
