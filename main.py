import os
import asyncio
import sqlite3
import time
import hmac
import hashlib
import secrets
from datetime import datetime
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager

import discord
from discord import app_commands
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse


# ============================================================
# Настройки
# ============================================================

load_dotenv()


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Липсва задължителната променлива {name} в .env")
    return value


TOKEN = required_env("DISCORD_TOKEN")
CHANNEL_ID = int(required_env("CHANNEL_ID"))
PANEL_PASSWORD = required_env("PANEL_PASSWORD")

# Админ сесия: 3 минути неактивност по подразбиране.
SESSION_TTL_SECONDS = int(os.getenv("PANEL_SESSION_SECONDS", "180"))
SESSION_COOKIE_NAME = "itclub_admin_session"
SESSION_COOKIE_SECURE = os.getenv("PANEL_COOKIE_SECURE", "1") != "0"

# Ако няма отделен SESSION_SECRET, извеждаме стабилен ключ от вече
# наличните тайни. По-добре е по желание да зададеш SESSION_SECRET в .env.
_session_secret_source = os.getenv("SESSION_SECRET") or f"{TOKEN}|{PANEL_PASSWORD}|itclub-admin"
SESSION_SIGNING_KEY = hashlib.sha256(_session_secret_source.encode("utf-8")).digest()

TZ = ZoneInfo("Europe/Sofia")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notifications.db")

DAYS_BG = [
    "понеделник", "вторник", "сряда", "четвъртък",
    "петък", "събота", "неделя",
]

MONTHS_BG = [
    "", "януари", "февруари", "март", "април", "май", "юни",
    "юли", "август", "септември", "октомври", "ноември", "декември",
]


# ============================================================
# Админ сесии
# ============================================================

def make_session_token() -> str:
    """
    Stateless, подписан session token.
    Съдържа срок на валидност + случаен nonce + HMAC подпис.
    """
    expires_at = int(time.time()) + SESSION_TTL_SECONDS
    nonce = secrets.token_urlsafe(18)
    payload = f"{expires_at}.{nonce}"
    signature = hmac.new(
        SESSION_SIGNING_KEY,
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}.{signature}"


def is_valid_session_token(token: str | None) -> bool:
    if not token:
        return False

    try:
        expires_raw, nonce, signature = token.split(".", 2)
        expires_at = int(expires_raw)
    except (ValueError, TypeError):
        return False

    if expires_at < int(time.time()):
        return False

    payload = f"{expires_at}.{nonce}"
    expected = hmac.new(
        SESSION_SIGNING_KEY,
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(signature, expected)


def request_has_valid_session(request: Request) -> bool:
    return is_valid_session_token(
        request.cookies.get(SESSION_COOKIE_NAME)
    )


def set_session_cookie(response):
    """
    Sliding session: при всяко успешно админ действие
    срокът отново става SESSION_TTL_SECONDS.
    """
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=make_session_token(),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
        samesite="strict",
        path="/",
    )
    return response


def clear_session_cookie(response):
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        secure=SESSION_COOKIE_SECURE,
        httponly=True,
        samesite="strict",
    )
    return response


def session_required_response():
    return JSONResponse(
        {
            "message": (
                "Сесията е изтекла. Влез отново в админ панела."
            )
        },
        status_code=401,
    )


def admin_json(request: Request, data, status_code=200):
    """
    Връща JSON и подновява 3-минутната сесия.
    Използва се само след успешна проверка на сесията.
    """
    response = JSONResponse(data, status_code=status_code)
    return set_session_cookie(response)


# ============================================================
# Discord
# ============================================================

intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

_slash_commands_synced = False
_last_bot_description = None
_last_presence_text = None
_description_edit_supported = True


# ============================================================
# База данни и миграции
# ============================================================

def ensure_column(db, table: str, column: str, definition: str):
    columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                meeting_date TEXT NOT NULL,
                meeting_time TEXT NOT NULL,
                sender TEXT NOT NULL,
                custom_message TEXT,
                tag_everyone INTEGER NOT NULL,
                success INTEGER NOT NULL,
                error TEXT,
                meeting_id INTEGER,
                discord_message_id INTEGER,
                discord_channel_id INTEGER,
                notification_kind TEXT NOT NULL DEFAULT 'announcement',
                deleted_from_discord INTEGER NOT NULL DEFAULT 0
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS meetings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_date TEXT NOT NULL,
                meeting_time TEXT NOT NULL,
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL,
                custom_message TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                updated_at TEXT,
                updated_by TEXT,
                UNIQUE(meeting_date, meeting_time)
            )
        """)

        # Миграция от предишната версия без тези колони.
        ensure_column(db, "notifications", "meeting_id", "INTEGER")
        ensure_column(db, "notifications", "discord_message_id", "INTEGER")
        ensure_column(db, "notifications", "discord_channel_id", "INTEGER")
        ensure_column(
            db,
            "notifications",
            "notification_kind",
            "TEXT NOT NULL DEFAULT 'announcement'",
        )
        ensure_column(
            db,
            "notifications",
            "deleted_from_discord",
            "INTEGER NOT NULL DEFAULT 0",
        )

        ensure_column(db, "meetings", "status", "TEXT NOT NULL DEFAULT 'active'")
        ensure_column(db, "meetings", "updated_at", "TEXT")
        ensure_column(db, "meetings", "updated_by", "TEXT")

        # Прехвърля старите успешни известия в meetings.
        db.execute("""
            INSERT OR IGNORE INTO meetings (
                meeting_date,
                meeting_time,
                created_at,
                created_by,
                custom_message,
                status
            )
            SELECT
                meeting_date,
                meeting_time,
                MIN(created_at),
                MIN(sender),
                MAX(custom_message),
                'active'
            FROM notifications
            WHERE success = 1
            GROUP BY meeting_date, meeting_time
        """)

        # Свързва старите notification записи със съответната сбирка.
        # Старите Discord съобщения нямат message ID и не могат да се
        # редактират/трият автоматично, но поне историята остава свързана.
        db.execute("""
            UPDATE notifications
            SET meeting_id = (
                SELECT m.id
                FROM meetings AS m
                WHERE m.meeting_date = notifications.meeting_date
                  AND m.meeting_time = notifications.meeting_time
                LIMIT 1
            )
            WHERE meeting_id IS NULL
              AND success = 1
        """)

        db.execute("""
            UPDATE notifications
            SET notification_kind = 'announcement'
            WHERE notification_kind IS NULL OR notification_kind = ''
        """)

        db.commit()


def db_row_to_dict(row):
    return dict(row) if row is not None else None


def save_notification(
    meeting_date,
    meeting_time,
    sender,
    custom,
    tag_everyone,
    success,
    error=None,
    meeting_id=None,
    discord_message_id=None,
    discord_channel_id=None,
    notification_kind="announcement",
):
    with sqlite3.connect(DB_PATH) as db:
        cur = db.execute(
            """
            INSERT INTO notifications (
                created_at,
                meeting_date,
                meeting_time,
                sender,
                custom_message,
                tag_everyone,
                success,
                error,
                meeting_id,
                discord_message_id,
                discord_channel_id,
                notification_kind,
                deleted_from_discord
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
                meeting_date,
                meeting_time,
                sender,
                custom or None,
                int(tag_everyone),
                int(success),
                error,
                meeting_id,
                discord_message_id,
                discord_channel_id,
                notification_kind,
            ),
        )
        db.commit()
        return cur.lastrowid


def create_meeting(meeting_date, meeting_time, sender, custom):
    with sqlite3.connect(DB_PATH) as db:
        try:
            cur = db.execute(
                """
                INSERT INTO meetings (
                    meeting_date,
                    meeting_time,
                    created_at,
                    created_by,
                    custom_message,
                    status
                )
                VALUES (?, ?, ?, ?, ?, 'active')
                """,
                (
                    meeting_date,
                    meeting_time,
                    datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
                    sender,
                    custom or None,
                ),
            )
            db.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError as exc:
            raise ValueError("Вече има сбирка с тази дата и час.") from exc


def get_meeting(meeting_id: int):
    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT * FROM meetings WHERE id = ?",
            (meeting_id,),
        ).fetchone()
    return db_row_to_dict(row)


def delete_meeting_local(meeting_id: int):
    with sqlite3.connect(DB_PATH) as db:
        db.execute("DELETE FROM notifications WHERE meeting_id = ?", (meeting_id,))
        db.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
        db.commit()


def update_meeting_local(meeting_id, meeting_date, meeting_time, sender, custom):
    with sqlite3.connect(DB_PATH) as db:
        conflict = db.execute(
            """
            SELECT id FROM meetings
            WHERE meeting_date = ? AND meeting_time = ? AND id <> ?
            """,
            (meeting_date, meeting_time, meeting_id),
        ).fetchone()

        if conflict:
            raise ValueError("Вече има друга сбирка с тази дата и час.")

        cur = db.execute(
            """
            UPDATE meetings
            SET meeting_date = ?,
                meeting_time = ?,
                custom_message = ?,
                updated_at = ?,
                updated_by = ?
            WHERE id = ?
            """,
            (
                meeting_date,
                meeting_time,
                custom or None,
                datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
                sender,
                meeting_id,
            ),
        )

        if cur.rowcount == 0:
            raise LookupError("Сбирката не е намерена.")

        db.commit()


def set_meeting_status(meeting_id: int, status: str, sender: str):
    if status not in {"active", "cancelled"}:
        raise ValueError("Невалиден статус.")

    with sqlite3.connect(DB_PATH) as db:
        cur = db.execute(
            """
            UPDATE meetings
            SET status = ?, updated_at = ?, updated_by = ?
            WHERE id = ?
            """,
            (
                status,
                datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
                sender,
                meeting_id,
            ),
        )

        if cur.rowcount == 0:
            raise LookupError("Сбирката не е намерена.")

        db.commit()


def parse_meeting_datetime(date_str: str, time_str: str):
    try:
        return datetime.strptime(
            f"{date_str} {time_str}",
            "%Y-%m-%d %H:%M",
        ).replace(tzinfo=TZ)
    except ValueError:
        return None


def get_active_meetings():
    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """
            SELECT * FROM meetings
            WHERE status = 'active'
            ORDER BY meeting_date ASC, meeting_time ASC
            """
        ).fetchall()

    result = []
    for row in rows:
        item = dict(row)
        when = parse_meeting_datetime(item["meeting_date"], item["meeting_time"])
        if when is not None:
            item["when"] = when
            result.append(item)
    return result


def get_meeting_stats():
    now = datetime.now(TZ)
    meetings = get_active_meetings()
    past = [m for m in meetings if m["when"] < now]
    future = [m for m in meetings if m["when"] >= now]

    return {
        "next": future[0] if future else None,
        "past": past,
        "future": future,
        "past_count": len(past),
        "future_count": len(future),
        "total_count": len(meetings),
    }


def get_admin_meetings():
    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """
            SELECT
                m.*,
                COALESCE(SUM(CASE WHEN n.success = 1 THEN 1 ELSE 0 END), 0) AS message_count,
                COALESCE(SUM(CASE
                    WHEN n.success = 1
                     AND n.discord_message_id IS NOT NULL
                    THEN 1 ELSE 0 END), 0) AS tracked_message_count,
                COALESCE(SUM(CASE
                    WHEN n.success = 1
                     AND n.discord_message_id IS NOT NULL
                     AND n.deleted_from_discord = 0
                    THEN 1 ELSE 0 END), 0) AS live_tracked_message_count
            FROM meetings AS m
            LEFT JOIN notifications AS n ON n.meeting_id = m.id
            GROUP BY m.id
            ORDER BY m.meeting_date DESC, m.meeting_time DESC
            """
        ).fetchall()

    return [dict(row) for row in rows]


def get_linked_notifications(meeting_id: int):
    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """
            SELECT * FROM notifications
            WHERE meeting_id = ? AND success = 1
            ORDER BY id ASC
            """,
            (meeting_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def mark_discord_message_deleted(notification_id: int):
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            "UPDATE notifications SET deleted_from_discord = 1 WHERE id = ?",
            (notification_id,),
        )
        db.commit()


init_db()


# ============================================================
# Форматиране и Discord embed-и
# ============================================================

def format_short_meeting(meeting):
    when = parse_meeting_datetime(meeting["meeting_date"], meeting["meeting_time"])
    if when is None:
        return f'{meeting["meeting_date"]} • {meeting["meeting_time"]}'
    return f"{when:%d.%m.%Y} • {when:%H:%M}"


def format_long_meeting(meeting):
    when = parse_meeting_datetime(meeting["meeting_date"], meeting["meeting_time"])
    if when is None:
        return format_short_meeting(meeting)
    return (
        f"{when.day} {MONTHS_BG[when.month]} {when.year} "
        f"({DAYS_BG[when.weekday()]}) • {when:%H:%M} ч."
    )


def build_meeting_embed(meeting, notification_kind="announcement"):
    when = parse_meeting_datetime(meeting["meeting_date"], meeting["meeting_time"])
    cancelled = meeting.get("status") == "cancelled"

    if cancelled:
        title = "❌ Сбирката е отменена"
        color = 0xED4245
    elif notification_kind == "reminder":
        title = "🔔 Напомняне за сбирка!"
        color = 0xFEE75C
    else:
        title = "📢 Ново събиране!"
        color = 0x5865F2

    embed = discord.Embed(
        title=title,
        description=meeting.get("custom_message") or None,
        color=color,
    )

    if when is not None:
        embed.add_field(
            name="📅 Дата",
            value=f"{when:%d.%m.%Y}\n{DAYS_BG[when.weekday()]}",
            inline=True,
        )
        embed.add_field(
            name="⏰ Час",
            value=f"{when:%H:%M} ч.",
            inline=True,
        )

        if cancelled:
            embed.add_field(name="📌 Статус", value="Отменена", inline=True)
        elif when >= datetime.now(TZ):
            embed.add_field(
                name="⏳ Остава",
                value=f"<t:{int(when.timestamp())}:R>",
                inline=True,
            )
        else:
            embed.add_field(name="📌 Статус", value="Проведена", inline=True)

    actor = meeting.get("updated_by") or meeting.get("created_by") or "ИТ клуб"

    if cancelled:
        footer = f"Отменено от {actor}"
    elif meeting.get("updated_at"):
        footer = f"Последно редактирано от {actor}"
    else:
        footer = f"Изпратено от {actor}"

    embed.set_footer(text=footer)
    return embed


def build_bot_description():
    stats = get_meeting_stats()
    next_meeting = stats["next"]

    lines = ["🤖 Ботът на ИТ клуба"]

    if next_meeting:
        lines.append(f"📅 Следваща: {format_short_meeting(next_meeting)}")
    else:
        lines.append("📅 Следваща: още няма обявена")

    recent_past = list(reversed(stats["past"][-3:]))
    if recent_past:
        past_text = ", ".join(m["when"].strftime("%d.%m") for m in recent_past)
        lines.append(f"🕘 Последни: {past_text}")
    else:
        lines.append("🕘 Последни: още няма")

    lines.append(f"📊 Минали сбирки: {stats['past_count']}")
    lines.append("💡 /сбирки за подробности")
    return "\n".join(lines)


def build_presence_text():
    next_meeting = get_meeting_stats()["next"]
    if next_meeting:
        when = next_meeting["when"]
        return f"следваща сбирка: {when:%d.%m} • {when:%H:%M}"
    return "за следващата сбирка 👀"


# ============================================================
# Discord помощни функции
# ============================================================

async def get_discord_channel(channel_id=None):
    target_id = int(channel_id or CHANNEL_ID)
    channel = bot.get_channel(target_id)
    if channel is None:
        channel = await bot.fetch_channel(target_id)
    return channel


async def update_linked_discord_messages(meeting_id: int):
    meeting = get_meeting(meeting_id)
    if not meeting:
        return {"edited": 0, "missing": 0, "untracked": 0, "errors": []}

    notifications = get_linked_notifications(meeting_id)
    result = {"edited": 0, "missing": 0, "untracked": 0, "errors": []}

    for item in notifications:
        if not item.get("discord_message_id"):
            result["untracked"] += 1
            continue

        if item.get("deleted_from_discord"):
            continue

        try:
            channel = await get_discord_channel(item.get("discord_channel_id"))

            if not hasattr(channel, "fetch_message"):
                result["errors"].append(
                    f"Каналът за notification #{item['id']} не поддържа fetch_message."
                )
                continue

            message = await channel.fetch_message(int(item["discord_message_id"]))
            embed = build_meeting_embed(
                meeting,
                item.get("notification_kind") or "announcement",
            )
            await message.edit(embed=embed)
            result["edited"] += 1

        except discord.NotFound:
            mark_discord_message_deleted(item["id"])
            result["missing"] += 1
        except discord.Forbidden:
            result["errors"].append(
                f"Нямам права да редактирам Discord съобщение #{item['discord_message_id']}."
            )
        except discord.HTTPException as exc:
            result["errors"].append(
                f"Discord грешка при #{item['discord_message_id']}: {str(exc)[:120]}"
            )

    return result


async def delete_linked_discord_messages(meeting_id: int):
    notifications = get_linked_notifications(meeting_id)
    result = {"deleted": 0, "missing": 0, "untracked": 0, "errors": []}

    for item in notifications:
        if not item.get("discord_message_id"):
            result["untracked"] += 1
            continue

        if item.get("deleted_from_discord"):
            continue

        try:
            channel = await get_discord_channel(item.get("discord_channel_id"))

            if not hasattr(channel, "fetch_message"):
                result["errors"].append(
                    f"Каналът за notification #{item['id']} не поддържа fetch_message."
                )
                continue

            message = await channel.fetch_message(int(item["discord_message_id"]))
            await message.delete()
            mark_discord_message_deleted(item["id"])
            result["deleted"] += 1

        except discord.NotFound:
            mark_discord_message_deleted(item["id"])
            result["missing"] += 1
        except discord.Forbidden:
            result["errors"].append(
                f"Нямам права да изтрия Discord съобщение #{item['discord_message_id']}."
            )
        except discord.HTTPException as exc:
            result["errors"].append(
                f"Discord грешка при #{item['discord_message_id']}: {str(exc)[:120]}"
            )

    return result


async def update_bot_profile():
    global _last_bot_description, _last_presence_text, _description_edit_supported

    if not bot.is_ready():
        return

    description = build_bot_description()

    if _description_edit_supported and description != _last_bot_description:
        try:
            app_info = await bot.application_info()

            if not hasattr(app_info, "edit"):
                _description_edit_supported = False
                print("⚠️ Тази версия на discord.py не поддържа AppInfo.edit().")
            else:
                await app_info.edit(description=description)
                _last_bot_description = description
                print("✅ Описанието на бота е обновено.")

        except discord.HTTPException as exc:
            print(f"⚠️ Неуспешно обновяване на описанието: {exc}")

    presence_text = build_presence_text()

    if presence_text != _last_presence_text:
        try:
            activity = discord.Activity(
                type=discord.ActivityType.watching,
                name=presence_text,
            )
            await bot.change_presence(
                status=discord.Status.online,
                activity=activity,
            )
            _last_presence_text = presence_text
            print(f"✅ Статусът е обновен: {presence_text}")
        except discord.DiscordException as exc:
            print(f"⚠️ Неуспешно обновяване на статуса: {exc}")


async def bot_profile_loop():
    await bot.wait_until_ready()

    while not bot.is_closed():
        try:
            await update_bot_profile()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"⚠️ Грешка в profile loop: {type(exc).__name__}: {exc}")

        await asyncio.sleep(60)


# ============================================================
# Slash команда /сбирки
# ============================================================

@tree.command(
    name="сбирки",
    description="Показва следващата и последните сбирки на ИТ клуба",
)
async def meetings_command(interaction: discord.Interaction):
    stats = get_meeting_stats()
    next_meeting = stats["next"]

    embed = discord.Embed(
        title="📅 Сбирки на ИТ клуба",
        color=0x5865F2,
        timestamp=datetime.now(TZ),
    )

    if next_meeting:
        when = next_meeting["when"]
        next_text = (
            f"**{format_long_meeting(next_meeting)}**\n"
            f"⏳ <t:{int(when.timestamp())}:R>"
        )

        if next_meeting.get("custom_message"):
            next_text += f"\n\n💬 {next_meeting['custom_message']}"

        embed.add_field(
            name="🟢 Следваща сбирка",
            value=next_text,
            inline=False,
        )
    else:
        embed.add_field(
            name="🟢 Следваща сбирка",
            value="Все още няма обявена следваща сбирка.",
            inline=False,
        )

    recent_past = list(reversed(stats["past"][-5:]))
    if recent_past:
        past_value = "\n".join(
            f"• <t:{int(m['when'].timestamp())}:D> — **{m['when']:%H:%M} ч.**"
            for m in recent_past
        )
    else:
        past_value = "Все още няма минали сбирки."

    embed.add_field(
        name="🕘 Последни сбирки",
        value=past_value,
        inline=False,
    )
    embed.add_field(
        name="📊 Статистика",
        value=(
            f"Минали: **{stats['past_count']}**\n"
            f"Предстоящи: **{stats['future_count']}**\n"
            f"Общо активни: **{stats['total_count']}**"
        ),
        inline=False,
    )
    embed.set_footer(text="ИТ клуб • Управлява се от системата за сбирки")

    await interaction.response.send_message(embed=embed)


async def sync_slash_commands():
    global _slash_commands_synced

    if _slash_commands_synced:
        return

    await bot.wait_until_ready()

    try:
        channel = await get_discord_channel(CHANNEL_ID)
    except discord.DiscordException as exc:
        print(f"⚠️ Не мога да намеря канала за slash sync: {exc}")
        return

    guild = getattr(channel, "guild", None)
    if guild is None:
        print("⚠️ CHANNEL_ID не сочи към канал в Discord сървър.")
        return

    guild_object = discord.Object(id=guild.id)

    try:
        tree.copy_global_to(guild=guild_object)
        synced = await tree.sync(guild=guild_object)
        _slash_commands_synced = True
        print(f"✅ Синхронизирани slash команди: {len(synced)} в {guild.name}")
    except discord.DiscordException as exc:
        print(f"⚠️ Грешка при sync на slash командите: {exc}")


@bot.event
async def on_ready():
    print(f"🤖 Влязъл като {bot.user} (ID: {bot.user.id if bot.user else 'unknown'})")

    if not _slash_commands_synced:
        await sync_slash_commands()

    await update_bot_profile()


# ============================================================
# FastAPI
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    bot_task = asyncio.create_task(bot.start(TOKEN), name="discord-bot")
    profile_task = asyncio.create_task(bot_profile_loop(), name="discord-profile-loop")

    try:
        yield
    finally:
        if not bot.is_closed():
            await bot.close()

        profile_task.cancel()
        if not bot_task.done():
            bot_task.cancel()

        await asyncio.gather(profile_task, bot_task, return_exceptions=True)


app = FastAPI(lifespan=lifespan)


# ============================================================
# Web панел
# ============================================================

LOGIN_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="bg">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ИТ клуб • Вход</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  body {
    background: radial-gradient(circle at 20% 20%, #1e1b4b, #0f172a 60%);
  }
  .glass {
    background: rgba(30,41,59,.72);
    backdrop-filter: blur(14px);
    border: 1px solid rgba(148,163,184,.15);
  }
  .field {
    width: 100%;
    padding: .75rem .85rem;
    border-radius: .65rem;
    background: #0f172a;
    border: 1px solid #334155;
    color: #fff;
    outline: none;
  }
  .field:focus {
    border-color: #6366f1;
    box-shadow: 0 0 0 3px rgba(99,102,241,.25);
  }
</style>
</head>

<body class="min-h-screen text-slate-100 p-4 flex items-center justify-center">

<form id="loginForm" class="glass w-full max-w-md rounded-2xl p-7 md:p-9 shadow-2xl space-y-5">
  <div>
    <div class="text-3xl mb-2">🔐</div>
    <h1 class="text-2xl font-bold">Админ панел на ИТ клуба</h1>
    <p class="text-sm text-slate-400 mt-1">
      Въведи паролата веднъж. След вход сесията остава активна
      3 минути след последното действие.
    </p>
  </div>

  <div>
    <label class="block text-xs uppercase tracking-wide text-slate-400 mb-1">
      Парола
    </label>
    <input
      id="loginPassword"
      name="password"
      type="password"
      required
      autofocus
      autocomplete="current-password"
      class="field"
      placeholder="••••••••"
    >
  </div>

  <button
    id="loginBtn"
    type="submit"
    class="w-full bg-indigo-600 hover:bg-indigo-500 font-semibold py-3 rounded-lg transition"
  >
    Вход
  </button>

  <div id="loginStatus" class="text-center text-sm min-h-[1.25rem]"></div>
</form>

<script>
const form = document.getElementById("loginForm");
const btn = document.getElementById("loginBtn");
const statusBox = document.getElementById("loginStatus");

form.addEventListener("submit", async event => {
  event.preventDefault();
  btn.disabled = true;
  btn.textContent = "Влизане...";
  statusBox.textContent = "";
  statusBox.className = "text-center text-sm min-h-[1.25rem]";

  try {
    const fd = new FormData(form);
    const res = await fetch("login", {
      method: "POST",
      body: fd
    });

    const data = await res.json();

    if (!res.ok) {
      statusBox.textContent = data.message || "Грешка при вход.";
      statusBox.className =
        "text-center text-sm min-h-[1.25rem] text-red-400";
      return;
    }

    statusBox.textContent = "✅ Успешен вход.";
    statusBox.className =
      "text-center text-sm min-h-[1.25rem] text-emerald-400";

    window.location.reload();

  } catch {
    statusBox.textContent = "Грешка при връзката със сървъра.";
    statusBox.className =
      "text-center text-sm min-h-[1.25rem] text-red-400";
  } finally {
    btn.disabled = false;
    btn.textContent = "Вход";
  }
});
</script>

</body>
</html>
"""


HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="bg">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ИТ клуб • Управление на сбирките</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  body {
    background: radial-gradient(circle at 20% 20%, #1e1b4b, #0f172a 60%);
  }
  .glass {
    background: rgba(30,41,59,.68);
    backdrop-filter: blur(12px);
    border: 1px solid rgba(148,163,184,.15);
  }
  .field {
    width: 100%; padding: .65rem .8rem; border-radius: .6rem;
    background: #0f172a; border: 1px solid #334155;
    color: #fff; outline: none; transition: .15s;
  }
  .field:focus {
    border-color: #6366f1;
    box-shadow: 0 0 0 3px rgba(99,102,241,.25);
  }
  .btn {
    border-radius: .55rem; padding: .55rem .75rem;
    font-size: .875rem; transition: .15s;
  }
  .btn:active { transform: scale(.98); }
  ::-webkit-calendar-picker-indicator { filter: invert(1); cursor: pointer; }
</style>
</head>
<body class="min-h-screen text-slate-100 p-4 md:p-8">

<div class="w-full max-w-6xl mx-auto space-y-6">

  <div class="glass rounded-xl px-4 py-3 flex flex-col sm:flex-row sm:items-center justify-between gap-3">
    <div>
      <div class="text-sm font-semibold">🔐 Админ сесията е активна</div>
      <div class="text-xs text-slate-400">
        Изтича след 3 минути бездействие.
      </div>
    </div>
    <button
      id="logoutBtn"
      type="button"
      class="btn border border-slate-600 hover:bg-slate-700"
    >
      🚪 Изход
    </button>
  </div>

  <div class="grid md:grid-cols-2 gap-6">

    <!-- ФОРМА -->
    <form id="form" class="glass rounded-2xl p-6 md:p-8 space-y-4 shadow-2xl">
      <div class="flex items-start justify-between gap-3">
        <div>
          <h1 id="formTitle" class="text-2xl font-bold">📢 Ново събиране</h1>
          <p id="formSubtitle" class="text-sm text-slate-400">
            Изпрати известие и добави сбирката в системата
          </p>
        </div>
        <span id="editBadge" class="hidden text-xs px-2 py-1 rounded bg-amber-500/20 text-amber-300">
          РЕДАКЦИЯ
        </span>
      </div>

      <div class="grid grid-cols-2 gap-3">
        <div>
          <label class="block text-xs uppercase tracking-wide text-slate-400 mb-1">Ден</label>
          <input id="date" type="date" name="date" required class="field">
        </div>
        <div>
          <label class="block text-xs uppercase tracking-wide text-slate-400 mb-1">Час</label>
          <input id="time" type="time" name="time" required class="field">
        </div>
      </div>

      <div>
        <label id="senderLabel" class="block text-xs uppercase tracking-wide text-slate-400 mb-1">
          Изпратено от
        </label>
        <input id="sender" type="text" name="sender" maxlength="40"
               placeholder="Твоето име" required class="field">
      </div>

      <div>
        <label class="block text-xs uppercase tracking-wide text-slate-400 mb-1">
          Допълнително съобщение <span class="normal-case text-slate-500">(по избор)</span>
        </label>
        <textarea id="custom" name="custom" rows="3" maxlength="1000"
                  placeholder="Напр. Ще работим по проекта за..."
                  class="field resize-none"></textarea>
      </div>

      <label id="everyoneWrap" class="flex items-center gap-2 text-sm cursor-pointer select-none">
        <input id="everyone" type="checkbox" name="everyone" value="1" checked
               class="w-4 h-4 accent-indigo-500">
        Тагни всички (@everyone)
      </label>

      <button id="btn" type="submit"
              class="w-full bg-indigo-600 hover:bg-indigo-500 font-semibold py-3 rounded-lg transition">
        Изпрати в Discord
      </button>

      <button id="cancelEditBtn" type="button"
              class="hidden w-full border border-slate-600 hover:bg-slate-700 py-2 rounded-lg transition">
        Откажи редакцията
      </button>

      <div id="status" class="text-center text-sm min-h-[1.25rem]"></div>
    </form>

    <!-- ПРЕГЛЕД -->
    <div class="glass rounded-2xl p-6 md:p-8 shadow-2xl self-start">
      <p class="text-xs uppercase tracking-wide text-slate-400 mb-3">Преглед</p>

      <div class="rounded-lg p-4" style="background:#313338">
        <div id="p-everyone" class="mb-2">
          <span class="px-1 rounded" style="background:rgba(88,101,242,.3);color:#c9cdfb">
            @everyone
          </span>
        </div>

        <div class="rounded-md p-4" style="background:#2b2d31;border-left:4px solid #5865F2">
          <div id="p-title" class="font-bold mb-2">📢 Ново събиране!</div>
          <div id="p-custom" class="text-sm text-slate-300 mb-3 whitespace-pre-wrap hidden"></div>

          <div class="grid grid-cols-2 gap-3 text-sm">
            <div>
              <div class="font-semibold text-xs mb-0.5">📅 Дата</div>
              <div id="p-date" class="text-slate-300">—</div>
            </div>
            <div>
              <div class="font-semibold text-xs mb-0.5">⏰ Час</div>
              <div id="p-time" class="text-slate-300">—</div>
            </div>
          </div>

          <div class="text-xs text-slate-400 mt-3">
            <span id="p-sender-prefix">Изпратено от</span> <span id="p-sender">…</span>
          </div>
        </div>
      </div>

      <div class="mt-6 rounded-xl border border-slate-700 bg-slate-900/40 p-4">
        <div class="font-semibold text-sm mb-2">🤖 Автоматично управление</div>
        <ul class="text-xs text-slate-400 space-y-1">
          <li>• редакцията променя и проследимите Discord съобщения;</li>
          <li>• отмяната ги превръща в червено „Сбирката е отменена“;</li>
          <li>• изтриването премахва съобщенията и записите;</li>
          <li>• профилът и <span class="text-indigo-300">/сбирки</span> се обновяват автоматично.</li>
        </ul>
      </div>
    </div>
  </div>

  <!-- УПРАВЛЕНИЕ -->
  <section class="glass rounded-2xl p-6 md:p-8 shadow-2xl">
    <div class="flex flex-col md:flex-row md:items-center justify-between gap-3 mb-5">
      <div>
        <h2 class="text-xl font-bold">🛠️ Управление на сбирките</h2>
        <p class="text-sm text-slate-400">
          Редактирай, отмени, възстанови, напомни или изтрий.
        </p>
      </div>

      <div class="flex flex-wrap gap-2">
        <button id="loadMeetingsBtn" type="button"
                class="btn bg-indigo-600 hover:bg-indigo-500 font-semibold">
          🔄 Зареди сбирките
        </button>
        <button id="historyBtn" type="button"
                class="btn border border-slate-600 hover:bg-slate-700">
          📜 История
        </button>
      </div>
    </div>

    <div id="meetingsStatus" class="text-sm mb-3"></div>
    <div id="meetings" class="grid lg:grid-cols-2 gap-3"></div>

    <div id="historyWrap" class="hidden mt-6 border-t border-slate-700 pt-5">
      <h3 class="font-semibold mb-3">📜 Последни известия</h3>
      <div id="history" class="text-xs space-y-2 max-h-80 overflow-y-auto"></div>
    </div>
  </section>

</div>

<script>
const DAYS = ["неделя","понеделник","вторник","сряда","четвъртък","петък","събота"];
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

let editingMeetingId = null;
let cachedMeetings = [];

async function apiFetch(url, options={}) {
  const res = await fetch(url, options);

  if (res.status === 401) {
    // Сесията е изтекла. Презареждането ще покаже екрана за вход.
    window.location.reload();
    throw new Error("SESSION_EXPIRED");
  }

  return res;
}

function setStatus(message, ok=true) {
  const st = $("status");
  st.textContent = message || "";
  st.className = "text-center text-sm min-h-[1.25rem] " +
    (ok ? "text-emerald-400" : "text-red-400");
}

function updatePreview() {
  const d = $("date").value;
  if (d) {
    const [y,m,day] = d.split("-");
    const wd = DAYS[new Date(+y,+m-1,+day).getDay()];
    $("p-date").textContent = `${day}.${m}.${y} (${wd})`;
  } else {
    $("p-date").textContent = "—";
  }

  $("p-time").textContent = $("time").value ? `${$("time").value} ч.` : "—";
  $("p-sender").textContent = $("sender").value || "…";

  const c = $("custom").value.trim();
  $("p-custom").textContent = c;
  $("p-custom").classList.toggle("hidden", !c);
  $("p-everyone").classList.toggle("hidden", editingMeetingId !== null || !$("everyone").checked);
}

["date","time","sender","custom","everyone"].forEach(id =>
  $(id).addEventListener("input", updatePreview));

function enterEditMode(meeting) {
  editingMeetingId = meeting.id;
  $("date").value = meeting.meeting_date;
  $("time").value = meeting.meeting_time;
  $("sender").value = meeting.updated_by || meeting.created_by || "";
  $("custom").value = meeting.custom_message || "";

  $("formTitle").textContent = "✏️ Редакция на сбирка";
  $("formSubtitle").textContent = "Промените ще се приложат и върху проследимите Discord съобщения";
  $("editBadge").classList.remove("hidden");
  $("cancelEditBtn").classList.remove("hidden");
  $("everyoneWrap").classList.add("hidden");
  $("btn").textContent = "Запази промените";
  $("senderLabel").textContent = "Редактирано от";
  $("p-title").textContent = meeting.status === "cancelled" ? "❌ Сбирката е отменена" : "📢 Ново събиране!";
  $("p-sender-prefix").textContent = "Редактирано от";
  setStatus("");
  updatePreview();
  window.scrollTo({top: 0, behavior: "smooth"});
}

function leaveEditMode(clearForm=false) {
  editingMeetingId = null;
  $("formTitle").textContent = "📢 Ново събиране";
  $("formSubtitle").textContent = "Изпрати известие и добави сбирката в системата";
  $("editBadge").classList.add("hidden");
  $("cancelEditBtn").classList.add("hidden");
  $("everyoneWrap").classList.remove("hidden");
  $("btn").textContent = "Изпрати в Discord";
  $("senderLabel").textContent = "Изпратено от";
  $("p-title").textContent = "📢 Ново събиране!";
  $("p-sender-prefix").textContent = "Изпратено от";

  if (clearForm) {
    $("date").value = "";
    $("time").value = "";
    $("custom").value = "";
  }
  updatePreview();
}

$("cancelEditBtn").addEventListener("click", () => leaveEditMode(false));

$("form").addEventListener("submit", async e => {
  e.preventDefault();
  const btn = $("btn");
  btn.disabled = true;
  setStatus("");

  try {
    const fd = new FormData($("form"));
    let url = "send";

    if (editingMeetingId !== null) {
      url = "meeting/edit";
      fd.append("meeting_id", editingMeetingId);
      fd.delete("everyone");
      btn.textContent = "Запазване...";
    } else {
      btn.textContent = "Изпращане...";
    }

    const res = await apiFetch(url, {method: "POST", body: fd});
    const data = await res.json();
    setStatus(data.message, res.ok);

    if (res.ok) {
      if (editingMeetingId !== null) leaveEditMode(false);
      await loadMeetings();
    }
  } catch {
    setStatus("Грешка при връзката със сървъра.", false);
  }

  btn.disabled = false;
  btn.textContent = editingMeetingId !== null ? "Запази промените" : "Изпрати в Discord";
});

function meetingState(m) {
  if (m.status === "cancelled") return {label:"ОТМЕНЕНА", cls:"bg-red-500/20 text-red-300"};
  const dt = new Date(`${m.meeting_date}T${m.meeting_time}:00`);
  if (dt < new Date()) return {label:"МИНАЛА", cls:"bg-slate-500/20 text-slate-300"};
  return {label:"ПРЕДСТОЯЩА", cls:"bg-emerald-500/20 text-emerald-300"};
}

function isFuture(m) {
  return new Date(`${m.meeting_date}T${m.meeting_time}:00`) >= new Date();
}

function renderMeetings(items) {
  cachedMeetings = items;
  const box = $("meetings");

  if (!items.length) {
    box.innerHTML = '<div class="text-slate-400">Няма записани сбирки.</div>';
    return;
  }

  box.innerHTML = items.map(m => {
    const state = meetingState(m);
    const oldUntracked = Math.max(0, Number(m.message_count) - Number(m.tracked_message_count));

    return `
      <article class="rounded-xl p-4 bg-slate-900/55 border border-slate-700">
        <div class="flex items-start justify-between gap-3">
          <div>
            <div class="font-semibold text-lg">📅 ${esc(m.meeting_date)} • ${esc(m.meeting_time)}</div>
            <div class="text-xs text-slate-400 mt-1">Създадена от ${esc(m.created_by)}</div>
          </div>
          <span class="text-[11px] px-2 py-1 rounded ${state.cls}">${state.label}</span>
        </div>

        ${m.custom_message ? `<div class="mt-3 text-sm text-slate-300 whitespace-pre-wrap">${esc(m.custom_message)}</div>` : ""}

        <div class="mt-3 text-xs text-slate-500">
          Discord съобщения: ${Number(m.message_count)} • проследими: ${Number(m.tracked_message_count)}
          ${oldUntracked ? `<span class="text-amber-400"> • ${oldUntracked} стар(и) без ID</span>` : ""}
        </div>

        <div class="mt-4 flex flex-wrap gap-2">
          <button class="btn bg-indigo-600 hover:bg-indigo-500" onclick="editMeeting(${m.id})">✏️ Редактирай</button>

          ${m.status === "active" && isFuture(m)
            ? `<button class="btn bg-amber-600 hover:bg-amber-500" onclick="remindMeeting(${m.id})">📣 Напомни</button>`
            : ""}

          ${m.status === "active" && isFuture(m)
            ? `<button class="btn bg-red-700 hover:bg-red-600" onclick="cancelMeeting(${m.id})">❌ Отмени</button>`
            : ""}

          ${m.status === "cancelled"
            ? `<button class="btn bg-emerald-700 hover:bg-emerald-600" onclick="restoreMeeting(${m.id})">♻️ Възстанови</button>`
            : ""}

          <button class="btn border border-red-700 text-red-300 hover:bg-red-950/60" onclick="deleteMeeting(${m.id})">🗑️ Изтрий</button>
        </div>
      </article>
    `;
  }).join("");
}

async function loadMeetings() {
  $("meetingsStatus").innerHTML = '<span class="text-slate-400">Зареждане...</span>';

  try {
    const res = await apiFetch("meetings", {method:"POST"});
    const data = await res.json();

    if (!res.ok) {
      $("meetingsStatus").innerHTML = `<span class="text-red-400">${esc(data.message)}</span>`;
      return;
    }

    $("meetingsStatus").textContent = "";
    renderMeetings(data.items);
  } catch {
    $("meetingsStatus").innerHTML = '<span class="text-red-400">Грешка при връзката.</span>';
  }
}

window.editMeeting = function(id) {
  const meeting = cachedMeetings.find(m => Number(m.id) === Number(id));
  if (meeting) enterEditMode(meeting);
};

async function postAction(url, fields={}) {
  const fd = new FormData();
  Object.entries(fields).forEach(([k,v]) => fd.append(k, v));
  const res = await apiFetch(url, {method:"POST", body:fd});
  const data = await res.json();
  return {res, data};
}

window.cancelMeeting = async function(id) {
  const meeting = cachedMeetings.find(m => Number(m.id) === Number(id));
  const actor = prompt("Кой отменя сбирката?", $("sender").value || "");
  if (actor === null || !actor.trim()) return;

  if (!confirm(`Да отменя ли сбирката ${meeting?.meeting_date || ""} ${meeting?.meeting_time || ""}?\nDiscord съобщенията ще бъдат редактирани.`)) return;

  const {res, data} = await postAction("meeting/cancel", {meeting_id:id, sender:actor.trim()});
  alert(data.message);
  if (res.ok) await loadMeetings();
};

window.restoreMeeting = async function(id) {
  const actor = prompt("Кой възстановява сбирката?", $("sender").value || "");
  if (actor === null || !actor.trim()) return;

  const {res, data} = await postAction("meeting/restore", {meeting_id:id, sender:actor.trim()});
  alert(data.message);
  if (res.ok) await loadMeetings();
};

window.remindMeeting = async function(id) {
  const actor = prompt("Изпратено от:", $("sender").value || "");
  if (actor === null || !actor.trim()) return;

  if (!confirm("Да се изпрати ли ново напомняне за тази сбирка?")) return;
  const tagEveryone = confirm("Да се тагне ли @everyone?\nOK = Да • Cancel = Не");

  const {res, data} = await postAction("meeting/remind", {
    meeting_id:id,
    sender:actor.trim(),
    everyone:tagEveryone ? "1" : ""
  });

  alert(data.message);
  if (res.ok) await loadMeetings();
};

window.deleteMeeting = async function(id) {
  const meeting = cachedMeetings.find(m => Number(m.id) === Number(id));
  const label = meeting ? `${meeting.meeting_date} в ${meeting.meeting_time}` : `#${id}`;

  if (!confirm(`⚠️ ОКОНЧАТЕЛНО ИЗТРИВАНЕ\n\nСбирка: ${label}\n\nВсички проследими Discord съобщения също ще бъдат изтрити. Това действие не може да се върне.`)) return;

  const {res, data} = await postAction("meeting/delete", {meeting_id:id});
  alert(data.message);

  if (res.ok) {
    if (editingMeetingId === Number(id)) leaveEditMode(true);
    await loadMeetings();
  }
};

$("loadMeetingsBtn").addEventListener("click", loadMeetings);

$("historyBtn").addEventListener("click", async () => {
  const wrap = $("historyWrap");
  wrap.classList.remove("hidden");
  const box = $("history");

  try {
    const res = await apiFetch("history", {method:"POST"});
    const data = await res.json();

    if (!res.ok) {
      box.innerHTML = `<div class="text-red-400">${esc(data.message)}</div>`;
      return;
    }

    if (!data.items.length) {
      box.innerHTML = '<div class="text-slate-400">Още няма известия.</div>';
      return;
    }

    box.innerHTML = data.items.map(i => `
      <div class="rounded-lg p-3 bg-slate-900/60 border border-slate-700">
        <div class="flex justify-between gap-3">
          <span class="font-semibold">${esc(i.meeting_date)} • ${esc(i.meeting_time)}</span>
          <span>${i.success ? "✅" : "❌"}</span>
        </div>
        <div class="text-slate-400">
          ${i.notification_kind === "reminder" ? "🔔 Напомняне" : "📢 Известие"}
          • от ${esc(i.sender)} • ${esc(i.created_at)}
        </div>
        ${i.discord_message_id ? `<div class="text-slate-500">Discord ID: ${esc(i.discord_message_id)}${i.deleted_from_discord ? " • изтрито" : ""}</div>` : '<div class="text-amber-500">Стар запис без Discord message ID</div>'}
        ${i.error ? `<div class="mt-1 text-red-400">${esc(i.error)}</div>` : ""}
      </div>
    `).join("");
  } catch {
    box.innerHTML = '<div class="text-red-400">Грешка при връзката със сървъра.</div>';
  }
});

$("logoutBtn").addEventListener("click", async () => {
  try {
    await fetch("logout", {method: "POST"});
  } finally {
    window.location.reload();
  }
});

updatePreview();
loadMeetings();
</script>
</body>
</html>
"""


# ============================================================
# Валидация
# ============================================================

def validate_meeting_input(date: str, time: str, sender: str, custom: str):
    try:
        datetime.strptime(date, "%Y-%m-%d")
        datetime.strptime(time, "%H:%M")
    except ValueError:
        raise ValueError("Невалидна дата или час.")

    sender_clean = sender.strip()[:40]
    if not sender_clean:
        raise ValueError("Полето за име е задължително.")

    return sender_clean, custom.strip()[:1000]


def action_message(base: str, result: dict):
    extras = []

    if result.get("edited"):
        extras.append(f"редактирани Discord съобщения: {result['edited']}")
    if result.get("deleted"):
        extras.append(f"изтрити Discord съобщения: {result['deleted']}")
    if result.get("missing"):
        extras.append(f"вече липсващи: {result['missing']}")
    if result.get("untracked"):
        extras.append(
            f"стари без запазен Discord ID: {result['untracked']} (не могат да се управляват автоматично)"
        )
    if result.get("errors"):
        extras.append("грешки: " + " | ".join(result["errors"]))

    return base + (" • " + " • ".join(extras) if extras else "")


# ============================================================
# Endpoints
# ============================================================

@app.get("/")
async def get_panel(request: Request):
    if request_has_valid_session(request):
        response = HTMLResponse(content=HTML_TEMPLATE)
        return set_session_cookie(response)

    return HTMLResponse(content=LOGIN_TEMPLATE)


@app.post("/login")
async def login(password: str = Form(...)):
    if not hmac.compare_digest(password, PANEL_PASSWORD):
        return JSONResponse(
            {"message": "Грешна парола!"},
            status_code=401,
        )

    response = JSONResponse(
        {
            "message": "✅ Успешен вход.",
            "session_seconds": SESSION_TTL_SECONDS,
        }
    )
    return set_session_cookie(response)


@app.post("/logout")
async def logout():
    response = JSONResponse({"message": "Излязохте от админ панела."})
    return clear_session_cookie(response)


@app.post("/send")
async def send_notification_endpoint(
    request: Request,
    date: str = Form(...),
    time: str = Form(...),
    sender: str = Form(...),
    custom: str = Form(""),
    everyone: str = Form(""),
):
    if not request_has_valid_session(request):
        return session_required_response()

    try:
        sender_clean, custom_clean = validate_meeting_input(date, time, sender, custom)
        meeting_id = create_meeting(date, time, sender_clean, custom_clean)
    except ValueError as exc:
        return admin_json(request, {"message": str(exc)}, status_code=409)

    tag_all = bool(everyone)
    meeting = get_meeting(meeting_id)

    await bot.wait_until_ready()

    try:
        channel = await get_discord_channel(CHANNEL_ID)
    except discord.DiscordException:
        delete_meeting_local(meeting_id)
        save_notification(
            date, time, sender_clean, custom_clean, tag_all, False,
            error="Каналът не е намерен",
            notification_kind="announcement",
        )
        return admin_json(
            request,
            {"message": "Каналът не е намерен."},
            status_code=500,
        )

    embed = build_meeting_embed(meeting, "announcement")

    try:
        message = await channel.send(
            content="@everyone" if tag_all else None,
            embed=embed,
            allowed_mentions=discord.AllowedMentions(
                everyone=tag_all,
                users=False,
                roles=False,
            ),
        )
    except discord.Forbidden:
        delete_meeting_local(meeting_id)
        save_notification(
            date, time, sender_clean, custom_clean, tag_all, False,
            error="Forbidden",
            notification_kind="announcement",
        )
        return admin_json(
            request,
            {"message": "Ботът няма нужните права в канала."},
            status_code=500,
        )
    except discord.DiscordException as exc:
        delete_meeting_local(meeting_id)
        save_notification(
            date, time, sender_clean, custom_clean, tag_all, False,
            error=str(exc)[:200],
            notification_kind="announcement",
        )
        return admin_json(
            request,
            {"message": "Грешка от Discord."},
            status_code=500,
        )

    save_notification(
        date,
        time,
        sender_clean,
        custom_clean,
        tag_all,
        True,
        meeting_id=meeting_id,
        discord_message_id=message.id,
        discord_channel_id=channel.id,
        notification_kind="announcement",
    )

    await update_bot_profile()

    return admin_json(
        request,
        {
            "message": (
                "✅ Сбирката е създадена и Discord съобщението "
                "вече се проследява."
            )
        },
    )


@app.post("/meetings")
async def meetings_admin(request: Request):
    if not request_has_valid_session(request):
        return session_required_response()

    return admin_json(
        request,
        {"items": get_admin_meetings()},
    )


@app.post("/meeting/edit")
async def edit_meeting_endpoint(
    request: Request,
    meeting_id: int = Form(...),
    date: str = Form(...),
    time: str = Form(...),
    sender: str = Form(...),
    custom: str = Form(""),
):
    if not request_has_valid_session(request):
        return session_required_response()

    try:
        sender_clean, custom_clean = validate_meeting_input(date, time, sender, custom)
        update_meeting_local(meeting_id, date, time, sender_clean, custom_clean)
    except ValueError as exc:
        return admin_json(request, {"message": str(exc)}, status_code=409)
    except LookupError as exc:
        return admin_json(request, {"message": str(exc)}, status_code=404)

    await bot.wait_until_ready()
    result = await update_linked_discord_messages(meeting_id)
    await update_bot_profile()

    message = action_message("✅ Сбирката е редактирана.", result)
    status_code = 200 if not result["errors"] else 207
    return admin_json(request, {"message": message}, status_code=status_code)


@app.post("/meeting/cancel")
async def cancel_meeting_endpoint(
    request: Request,
    meeting_id: int = Form(...),
    sender: str = Form(...),
):
    if not request_has_valid_session(request):
        return session_required_response()

    sender_clean = sender.strip()[:40]
    if not sender_clean:
        return admin_json(
            request,
            {"message": "Въведи кой отменя сбирката."},
            status_code=400,
        )

    try:
        set_meeting_status(meeting_id, "cancelled", sender_clean)
    except LookupError as exc:
        return admin_json(request, {"message": str(exc)}, status_code=404)

    await bot.wait_until_ready()
    result = await update_linked_discord_messages(meeting_id)
    await update_bot_profile()

    message = action_message("❌ Сбирката е отменена.", result)
    status_code = 200 if not result["errors"] else 207
    return admin_json(request, {"message": message}, status_code=status_code)


@app.post("/meeting/restore")
async def restore_meeting_endpoint(
    request: Request,
    meeting_id: int = Form(...),
    sender: str = Form(...),
):
    if not request_has_valid_session(request):
        return session_required_response()

    sender_clean = sender.strip()[:40]
    if not sender_clean:
        return admin_json(
            request,
            {"message": "Въведи кой възстановява сбирката."},
            status_code=400,
        )

    try:
        set_meeting_status(meeting_id, "active", sender_clean)
    except LookupError as exc:
        return admin_json(request, {"message": str(exc)}, status_code=404)

    await bot.wait_until_ready()
    result = await update_linked_discord_messages(meeting_id)
    await update_bot_profile()

    message = action_message("♻️ Сбирката е възстановена.", result)
    status_code = 200 if not result["errors"] else 207
    return admin_json(request, {"message": message}, status_code=status_code)


@app.post("/meeting/remind")
async def remind_meeting_endpoint(
    request: Request,
    meeting_id: int = Form(...),
    sender: str = Form(...),
    everyone: str = Form(""),
):
    if not request_has_valid_session(request):
        return session_required_response()

    meeting = get_meeting(meeting_id)
    if not meeting:
        return admin_json(
            request,
            {"message": "Сбирката не е намерена."},
            status_code=404,
        )

    if meeting.get("status") != "active":
        return admin_json(
            request,
            {"message": "Не може да се изпраща напомняне за отменена сбирка."},
            status_code=409,
        )

    when = parse_meeting_datetime(meeting["meeting_date"], meeting["meeting_time"])
    if when is not None and when < datetime.now(TZ):
        return admin_json(
            request,
            {"message": "Не може да се изпраща напомняне за вече минала сбирка."},
            status_code=409,
        )

    sender_clean = sender.strip()[:40]
    if not sender_clean:
        return admin_json(
            request,
            {"message": "Въведи кой изпраща напомнянето."},
            status_code=400,
        )

    tag_all = bool(everyone)
    await bot.wait_until_ready()

    try:
        channel = await get_discord_channel(CHANNEL_ID)
        embed = build_meeting_embed(meeting, "reminder")
        embed.set_footer(text=f"Напомнянето е изпратено от {sender_clean}")

        message = await channel.send(
            content="@everyone" if tag_all else None,
            embed=embed,
            allowed_mentions=discord.AllowedMentions(
                everyone=tag_all,
                users=False,
                roles=False,
            ),
        )
    except discord.Forbidden:
        save_notification(
            meeting["meeting_date"], meeting["meeting_time"], sender_clean,
            meeting.get("custom_message") or "", tag_all, False,
            error="Forbidden", meeting_id=meeting_id,
            notification_kind="reminder",
        )
        return admin_json(
            request,
            {"message": "Ботът няма нужните права в канала."},
            status_code=500,
        )
    except discord.DiscordException as exc:
        save_notification(
            meeting["meeting_date"], meeting["meeting_time"], sender_clean,
            meeting.get("custom_message") or "", tag_all, False,
            error=str(exc)[:200], meeting_id=meeting_id,
            notification_kind="reminder",
        )
        return admin_json(
            request,
            {"message": "Грешка от Discord."},
            status_code=500,
        )

    save_notification(
        meeting["meeting_date"],
        meeting["meeting_time"],
        sender_clean,
        meeting.get("custom_message") or "",
        tag_all,
        True,
        meeting_id=meeting_id,
        discord_message_id=message.id,
        discord_channel_id=channel.id,
        notification_kind="reminder",
    )

    return admin_json(
        request,
        {"message": "📣 Напомнянето е изпратено и се проследява."},
    )


@app.post("/meeting/delete")
async def delete_meeting_endpoint(
    request: Request,
    meeting_id: int = Form(...),
):
    if not request_has_valid_session(request):
        return session_required_response()

    meeting = get_meeting(meeting_id)
    if not meeting:
        return admin_json(
            request,
            {"message": "Сбирката не е намерена."},
            status_code=404,
        )

    await bot.wait_until_ready()
    result = await delete_linked_discord_messages(meeting_id)

    # Ако има реална Discord грешка, НЕ трием локалния запис.
    if result["errors"]:
        message = action_message(
            "⚠️ Сбирката НЕ е изтрита от базата, защото не всички "
            "Discord съобщения можаха да бъдат премахнати.",
            result,
        )
        return admin_json(
            request,
            {"message": message},
            status_code=502,
        )

    delete_meeting_local(meeting_id)
    await update_bot_profile()

    message = action_message("🗑️ Сбирката е изтрита окончателно.", result)
    return admin_json(request, {"message": message})


@app.post("/history")
async def history(request: Request):
    if not request_has_valid_session(request):
        return session_required_response()

    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT * FROM notifications ORDER BY id DESC LIMIT 30"
        ).fetchall()

    return admin_json(
        request,
        {"items": [dict(row) for row in rows]},
    )


@app.get("/health")
async def health():
    return JSONResponse({"ok": True, "discord_ready": bot.is_ready()})


# ============================================================
# Стартиране
# ============================================================

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
