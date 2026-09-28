import os
import asyncio
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager

import discord
from discord import app_commands
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Form
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

TZ = ZoneInfo("Europe/Sofia")
DB_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "notifications.db",
)

DAYS_BG = [
    "понеделник",
    "вторник",
    "сряда",
    "четвъртък",
    "петък",
    "събота",
    "неделя",
]

MONTHS_BG = [
    "",
    "януари",
    "февруари",
    "март",
    "април",
    "май",
    "юни",
    "юли",
    "август",
    "септември",
    "октомври",
    "ноември",
    "декември",
]


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
# База данни
# ============================================================

def init_db():
    with sqlite3.connect(DB_PATH) as db:
        # История на всички опити за изпращане на известия.
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
                error TEXT
            )
        """)

        # Реалните сбирки са отделени от известията.
        # Така две известия за една и съща дата/час не броят две сбирки.
        db.execute("""
            CREATE TABLE IF NOT EXISTS meetings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_date TEXT NOT NULL,
                meeting_time TEXT NOT NULL,
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL,
                custom_message TEXT,
                UNIQUE(meeting_date, meeting_time)
            )
        """)

        # Автоматична миграция на старите успешни известия.
        # INSERT OR IGNORE пази уникалността по дата + час.
        db.execute("""
            INSERT OR IGNORE INTO meetings (
                meeting_date,
                meeting_time,
                created_at,
                created_by,
                custom_message
            )
            SELECT
                meeting_date,
                meeting_time,
                MIN(created_at),
                MIN(sender),
                MAX(custom_message)
            FROM notifications
            WHERE success = 1
            GROUP BY meeting_date, meeting_time
        """)

        db.commit()


def save_notification(
    meeting_date,
    meeting_time,
    sender,
    custom,
    tag_everyone,
    success,
    error=None,
):
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            """
            INSERT INTO notifications (
                created_at,
                meeting_date,
                meeting_time,
                sender,
                custom_message,
                tag_everyone,
                success,
                error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )
        db.commit()


def upsert_meeting(meeting_date, meeting_time, sender, custom):
    """
    Записва сбирката само веднъж.
    Ако същата дата и час вече съществуват, обновява подателя
    и допълнителното съобщение, ако има ново.
    """
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            """
            INSERT INTO meetings (
                meeting_date,
                meeting_time,
                created_at,
                created_by,
                custom_message
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(meeting_date, meeting_time)
            DO UPDATE SET
                created_by = excluded.created_by,
                custom_message = CASE
                    WHEN excluded.custom_message IS NOT NULL
                         AND excluded.custom_message <> ''
                    THEN excluded.custom_message
                    ELSE meetings.custom_message
                END
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


def parse_meeting_datetime(date_str: str, time_str: str):
    try:
        return datetime.strptime(
            f"{date_str} {time_str}",
            "%Y-%m-%d %H:%M",
        ).replace(tzinfo=TZ)
    except ValueError:
        return None


def get_all_meetings():
    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """
            SELECT *
            FROM meetings
            ORDER BY meeting_date ASC, meeting_time ASC
            """
        ).fetchall()

    meetings = []

    for row in rows:
        item = dict(row)
        when = parse_meeting_datetime(
            item["meeting_date"],
            item["meeting_time"],
        )

        if when is None:
            continue

        item["when"] = when
        meetings.append(item)

    return meetings


def get_meeting_stats():
    now = datetime.now(TZ)
    meetings = get_all_meetings()

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


init_db()


# ============================================================
# Форматиране
# ============================================================

def format_short_meeting(meeting):
    when = meeting["when"]
    return f"{when:%d.%m.%Y} • {when:%H:%M}"


def format_long_meeting(meeting):
    when = meeting["when"]
    return (
        f"{when.day} {MONTHS_BG[when.month]} {when.year} "
        f"({DAYS_BG[when.weekday()]}) • {when:%H:%M} ч."
    )


def build_bot_description():
    """
    Кратко описание за Discord профила на приложението.

    Държим го нарочно кратко, защото профилът не е място
    за пълен архив.
    """
    stats = get_meeting_stats()
    next_meeting = stats["next"]

    lines = [
        "🤖 Ботът на ИТ клуба",
    ]

    if next_meeting:
        lines.append(
            f"📅 Следваща: {format_short_meeting(next_meeting)}"
        )
    else:
        lines.append("📅 Следваща: още няма обявена")

    recent_past = list(reversed(stats["past"][-3:]))

    if recent_past:
        past_text = ", ".join(
            m["when"].strftime("%d.%m")
            for m in recent_past
        )
        lines.append(f"🕘 Последни: {past_text}")
    else:
        lines.append("🕘 Последни: още няма")

    lines.append(f"📊 Минали сбирки: {stats['past_count']}")
    lines.append("💡 /сбирки за подробности")

    return "\n".join(lines)


def build_presence_text():
    stats = get_meeting_stats()
    next_meeting = stats["next"]

    if next_meeting:
        when = next_meeting["when"]
        return f"следваща сбирка: {when:%d.%m} • {when:%H:%M}"

    return "за следващата сбирка 👀"


# ============================================================
# Discord профил и статус
# ============================================================

async def update_bot_profile():
    global _last_bot_description
    global _last_presence_text
    global _description_edit_supported

    if not bot.is_ready():
        return

    # --------------------
    # Application description
    # --------------------
    description = build_bot_description()

    if (
        _description_edit_supported
        and description != _last_bot_description
    ):
        try:
            app_info = await bot.application_info()

            if not hasattr(app_info, "edit"):
                _description_edit_supported = False
                print(
                    "⚠️ Тази версия на discord.py не поддържа "
                    "AppInfo.edit(). Обнови до discord.py >= 2.4."
                )
            else:
                await app_info.edit(description=description)
                _last_bot_description = description
                print("✅ Описанието на бота е обновено.")

        except discord.HTTPException as exc:
            print(
                f"⚠️ Неуспешно обновяване на описанието: {exc}"
            )

    # --------------------
    # Activity / Presence
    # --------------------
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
            print(
                f"⚠️ Неуспешно обновяване на статуса: {exc}"
            )


async def bot_profile_loop():
    await bot.wait_until_ready()

    while not bot.is_closed():
        try:
            await update_bot_profile()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Профилът не трябва да може да срине целия бот.
            print(
                f"⚠️ Неочаквана грешка в profile loop: "
                f"{type(exc).__name__}: {exc}"
            )

        await asyncio.sleep(60)


# ============================================================
# Slash команди
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
        unix = int(when.timestamp())

        next_text = (
            f"**{format_long_meeting(next_meeting)}**\n"
            f"⏳ <t:{unix}:R>"
        )

        custom = next_meeting.get("custom_message")
        if custom:
            next_text += f"\n\n💬 {custom}"

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
        past_lines = []

        for meeting in recent_past:
            when = meeting["when"]
            unix = int(when.timestamp())
            past_lines.append(
                f"• <t:{unix}:D> — **{when:%H:%M} ч.**"
            )

        past_value = "\n".join(past_lines)
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
            f"Общо записани: **{stats['total_count']}**"
        ),
        inline=False,
    )

    embed.set_footer(
        text="ИТ клуб • Данните се вземат от системата за известия"
    )

    await interaction.response.send_message(embed=embed)


async def sync_slash_commands():
    """
    Синхронизира командите директно в сървъра, в който се намира
    CHANNEL_ID. Guild командите се появяват веднага.
    """
    global _slash_commands_synced

    if _slash_commands_synced:
        return

    await bot.wait_until_ready()

    channel = bot.get_channel(CHANNEL_ID)

    if channel is None:
        try:
            channel = await bot.fetch_channel(CHANNEL_ID)
        except discord.DiscordException as exc:
            print(
                f"⚠️ Не мога да намеря канала за sync на slash командите: "
                f"{exc}"
            )
            return

    guild = getattr(channel, "guild", None)

    if guild is None:
        print(
            "⚠️ CHANNEL_ID не сочи към канал в Discord сървър. "
            "Slash командите не бяха синхронизирани."
        )
        return

    guild_object = discord.Object(id=guild.id)

    try:
        # Командите са дефинирани глобално в tree, но ги копираме
        # като guild commands, за да се появяват веднага в този сървър.
        tree.copy_global_to(guild=guild_object)
        synced = await tree.sync(guild=guild_object)

        _slash_commands_synced = True
        print(
            f"✅ Синхронизирани slash команди: {len(synced)} "
            f"в {guild.name}"
        )

    except discord.DiscordException as exc:
        print(f"⚠️ Грешка при sync на slash командите: {exc}")


@bot.event
async def on_ready():
    print(
        f"🤖 Влязъл като {bot.user} "
        f"(ID: {bot.user.id if bot.user else 'unknown'})"
    )

    if not _slash_commands_synced:
        await sync_slash_commands()

    await update_bot_profile()


# ============================================================
# FastAPI приложение
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    bot_task = asyncio.create_task(
        bot.start(TOKEN),
        name="discord-bot",
    )

    profile_task = asyncio.create_task(
        bot_profile_loop(),
        name="discord-profile-loop",
    )

    try:
        yield
    finally:
        if not bot.is_closed():
            await bot.close()

        profile_task.cancel()

        if not bot_task.done():
            bot_task.cancel()

        await asyncio.gather(
            profile_task,
            bot_task,
            return_exceptions=True,
        )


app = FastAPI(lifespan=lifespan)


# ============================================================
# Web панел
# ============================================================

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="bg">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Дискорд Известия</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  body {
    background:
      radial-gradient(circle at 20% 20%, #1e1b4b, #0f172a 60%);
  }

  .glass {
    background: rgba(30,41,59,.65);
    backdrop-filter: blur(12px);
    border: 1px solid rgba(148,163,184,.15);
  }

  .field {
    width: 100%;
    padding: .65rem .8rem;
    border-radius: .6rem;
    background: #0f172a;
    border: 1px solid #334155;
    color: #fff;
    outline: none;
    transition: .15s;
  }

  .field:focus {
    border-color: #6366f1;
    box-shadow: 0 0 0 3px rgba(99,102,241,.25);
  }

  ::-webkit-calendar-picker-indicator {
    filter: invert(1);
    cursor: pointer;
  }
</style>
</head>

<body class="min-h-screen text-slate-100 p-4 md:p-8 flex items-center justify-center">

<div class="w-full max-w-5xl grid md:grid-cols-2 gap-6">

  <!-- ФОРМА -->
  <form
    id="form"
    class="glass rounded-2xl p-6 md:p-8 space-y-4 shadow-2xl"
  >
    <div>
      <h1 class="text-2xl font-bold">📢 Ново събиране</h1>
      <p class="text-sm text-slate-400">
        Изпрати известие до сървъра в Discord
      </p>
    </div>

    <div class="grid grid-cols-2 gap-3">

      <div>
        <label
          class="block text-xs uppercase tracking-wide text-slate-400 mb-1"
        >
          Ден
        </label>

        <input
          id="date"
          type="date"
          name="date"
          required
          class="field"
        >
      </div>

      <div>
        <label
          class="block text-xs uppercase tracking-wide text-slate-400 mb-1"
        >
          Час
        </label>

        <input
          id="time"
          type="time"
          name="time"
          required
          class="field"
        >
      </div>

    </div>

    <div>
      <label
        class="block text-xs uppercase tracking-wide text-slate-400 mb-1"
      >
        Изпратено от
      </label>

      <input
        id="sender"
        type="text"
        name="sender"
        maxlength="40"
        placeholder="Твоето име"
        required
        class="field"
      >
    </div>

    <div>
      <label
        class="block text-xs uppercase tracking-wide text-slate-400 mb-1"
      >
        Допълнително съобщение
        <span class="normal-case text-slate-500">(по избор)</span>
      </label>

      <textarea
        id="custom"
        name="custom"
        rows="3"
        maxlength="1000"
        placeholder="Напр. Ще работим по проекта за..."
        class="field resize-none"
      ></textarea>
    </div>

    <label
      class="flex items-center gap-2 text-sm cursor-pointer select-none"
    >
      <input
        id="everyone"
        type="checkbox"
        name="everyone"
        value="1"
        checked
        class="w-4 h-4 accent-indigo-500"
      >

      Тагни всички (@everyone)
    </label>

    <div>
      <label
        class="block text-xs uppercase tracking-wide text-slate-400 mb-1"
      >
        Парола за достъп
      </label>

      <input
        type="password"
        name="password"
        required
        class="field"
      >
    </div>

    <button
      id="btn"
      type="submit"
      class="w-full bg-indigo-600 hover:bg-indigo-500 active:scale-[.99]
             font-semibold py-3 rounded-lg transition"
    >
      Изпрати в Discord
    </button>

    <div
      id="status"
      class="text-center text-sm min-h-[1.25rem]"
    ></div>

    <button
      id="histBtn"
      type="button"
      class="w-full border border-slate-600 hover:bg-slate-700
             text-sm py-2 rounded-lg transition"
    >
      📜 Покажи история
    </button>

    <div
      id="history"
      class="text-xs space-y-2 max-h-64 overflow-y-auto"
    ></div>

  </form>


  <!-- ПРЕГЛЕД -->
  <div
    class="glass rounded-2xl p-6 md:p-8 shadow-2xl self-start"
  >
    <p
      class="text-xs uppercase tracking-wide text-slate-400 mb-3"
    >
      Преглед
    </p>

    <div
      class="rounded-lg p-4"
      style="background:#313338"
    >
      <div
        id="p-everyone"
        class="mb-2"
      >
        <span
          class="px-1 rounded"
          style="background:rgba(88,101,242,.3);color:#c9cdfb"
        >
          @everyone
        </span>
      </div>

      <div
        class="rounded-md p-4"
        style="background:#2b2d31;border-left:4px solid #5865F2"
      >
        <div class="font-bold mb-2">
          📢 Ново събиране!
        </div>

        <div
          id="p-custom"
          class="text-sm text-slate-300 mb-3 whitespace-pre-wrap hidden"
        ></div>

        <div class="grid grid-cols-2 gap-3 text-sm">

          <div>
            <div class="font-semibold text-xs mb-0.5">
              📅 Дата
            </div>
            <div
              id="p-date"
              class="text-slate-300"
            >
              —
            </div>
          </div>

          <div>
            <div class="font-semibold text-xs mb-0.5">
              ⏰ Час
            </div>
            <div
              id="p-time"
              class="text-slate-300"
            >
              —
            </div>
          </div>

        </div>

        <div class="text-xs text-slate-400 mt-3">
          Изпратено от
          <span id="p-sender">…</span>
        </div>
      </div>
    </div>

    <p class="text-xs text-slate-500 mt-3">
      Така ще изглежда съобщението в канала.
    </p>

    <div
      class="mt-6 rounded-xl border border-slate-700 bg-slate-900/40 p-4"
    >
      <div class="font-semibold text-sm mb-2">
        🤖 Ботът автоматично ще обнови:
      </div>

      <ul class="text-xs text-slate-400 space-y-1">
        <li>• следващата сбирка в профила;</li>
        <li>• последните минали сбирки;</li>
        <li>• Activity статуса под името;</li>
        <li>• командата <span class="text-indigo-300">/сбирки</span>.</li>
      </ul>
    </div>

  </div>

</div>


<script>
const DAYS = [
  "неделя",
  "понеделник",
  "вторник",
  "сряда",
  "четвъртък",
  "петък",
  "събота"
];

const $ = id => document.getElementById(id);

const esc = s => String(s ?? "").replace(
  /[&<>"']/g,
  c => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;"
  }[c])
);


function updatePreview() {
  const d = $("date").value;

  if (d) {
    const [y, m, day] = d.split("-");
    const wd = DAYS[
      new Date(+y, +m - 1, +day).getDay()
    ];

    $("p-date").textContent =
      `${day}.${m}.${y} (${wd})`;

  } else {
    $("p-date").textContent = "—";
  }

  $("p-time").textContent =
    $("time").value
      ? `${$("time").value} ч.`
      : "—";

  $("p-sender").textContent =
    $("sender").value || "…";

  const c = $("custom").value.trim();

  $("p-custom").textContent = c;
  $("p-custom").classList.toggle("hidden", !c);

  $("p-everyone").classList.toggle(
    "hidden",
    !$("everyone").checked
  );
}


[
  "date",
  "time",
  "sender",
  "custom",
  "everyone"
].forEach(id =>
  $(id).addEventListener("input", updatePreview)
);

updatePreview();


$("form").addEventListener("submit", async e => {
  e.preventDefault();

  const btn = $("btn");
  const st = $("status");

  btn.disabled = true;
  btn.textContent = "Изпращане...";
  st.textContent = "";

  try {
    const res = await fetch(
      "send",
      {
        method: "POST",
        body: new FormData($("form"))
      }
    );

    const data = await res.json();

    st.textContent = data.message;

    st.className =
      "text-center text-sm min-h-[1.25rem] " +
      (res.ok
        ? "text-emerald-400"
        : "text-red-400");

  } catch {
    st.textContent =
      "Грешка при връзката със сървъра.";

    st.className =
      "text-center text-sm min-h-[1.25rem] text-red-400";
  }

  btn.disabled = false;
  btn.textContent = "Изпрати в Discord";
});


$("histBtn").addEventListener("click", async () => {
  const box = $("history");

  const fd = new FormData();

  fd.append(
    "password",
    document.querySelector(
      'input[name="password"]'
    ).value
  );

  try {
    const res = await fetch(
      "history",
      {
        method: "POST",
        body: fd
      }
    );

    const data = await res.json();

    if (!res.ok) {
      box.innerHTML =
        `<div class="text-red-400">
          ${esc(data.message)}
        </div>`;

      return;
    }

    if (!data.items.length) {
      box.innerHTML =
        '<div class="text-slate-400">' +
        'Още няма изпратени известия.' +
        '</div>';

      return;
    }

    box.innerHTML = data.items.map(i => `
      <div
        class="rounded-lg p-3 bg-slate-900/60
               border border-slate-700"
      >
        <div class="flex justify-between">
          <span class="font-semibold">
            ${esc(i.meeting_date)}
            •
            ${esc(i.meeting_time)}
          </span>

          <span>
            ${i.success ? "✅" : "❌"}
          </span>
        </div>

        <div class="text-slate-400">
          от ${esc(i.sender)}
          •
          ${esc(i.created_at)}
        </div>

        ${
          i.custom_message
            ? `<div class="mt-1 text-slate-300">
                ${esc(i.custom_message)}
               </div>`
            : ""
        }

        ${
          i.error
            ? `<div class="mt-1 text-red-400">
                ${esc(i.error)}
               </div>`
            : ""
        }
      </div>
    `).join("");

  } catch {
    box.innerHTML =
      '<div class="text-red-400">' +
      'Грешка при връзката със сървъра.' +
      '</div>';
  }
});
</script>

</body>
</html>
"""


# ============================================================
# FastAPI endpoints
# ============================================================

@app.get("/")
async def get_panel():
    return HTMLResponse(content=HTML_TEMPLATE)


@app.post("/send")
async def send_notification(
    date: str = Form(...),
    time: str = Form(...),
    sender: str = Form(...),
    password: str = Form(...),
    custom: str = Form(""),
    everyone: str = Form(""),
):
    if password != PANEL_PASSWORD:
        return JSONResponse(
            {"message": "Грешна парола!"},
            status_code=401,
        )

    try:
        d = datetime.strptime(date, "%Y-%m-%d")
        t = datetime.strptime(time, "%H:%M")
    except ValueError:
        return JSONResponse(
            {"message": "Невалидна дата или час."},
            status_code=400,
        )

    sender_clean = sender.strip()[:40]
    custom = custom.strip()[:1000]
    tag_all = bool(everyone)

    if not sender_clean:
        return JSONResponse(
            {"message": "Полето „Изпратено от“ е задължително."},
            status_code=400,
        )

    await bot.wait_until_ready()

    channel = bot.get_channel(CHANNEL_ID)

    if channel is None:
        try:
            channel = await bot.fetch_channel(CHANNEL_ID)
        except discord.DiscordException:
            save_notification(
                date,
                time,
                sender_clean,
                custom,
                tag_all,
                False,
                "Каналът не е намерен",
            )

            return JSONResponse(
                {"message": "Каналът не е намерен."},
                status_code=500,
            )

    when = d.replace(
        hour=t.hour,
        minute=t.minute,
        tzinfo=TZ,
    )

    embed = discord.Embed(
        title="📢 Ново събиране!",
        description=custom or None,
        color=0x5865F2,
    )

    embed.add_field(
        name="📅 Дата",
        value=(
            f"{d:%d.%m.%Y}\n"
            f"{DAYS_BG[d.weekday()]}"
        ),
        inline=True,
    )

    embed.add_field(
        name="⏰ Час",
        value=f"{time} ч.",
        inline=True,
    )

    embed.add_field(
        name="⏳ Остава",
        value=f"<t:{int(when.timestamp())}:R>",
        inline=True,
    )

    embed.set_footer(
        text=f"Изпратено от {sender_clean}"
    )

    try:
        await channel.send(
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
            date,
            time,
            sender_clean,
            custom,
            tag_all,
            False,
            "Forbidden",
        )

        return JSONResponse(
            {
                "message":
                    "Ботът няма права "
                    "(Send Messages / Embed Links / Mention Everyone)."
            },
            status_code=500,
        )

    except discord.DiscordException as exc:
        save_notification(
            date,
            time,
            sender_clean,
            custom,
            tag_all,
            False,
            str(exc)[:200],
        )

        return JSONResponse(
            {"message": "Грешка от Discord."},
            status_code=500,
        )

    # Известието е изпратено успешно.
    save_notification(
        date,
        time,
        sender_clean,
        custom,
        tag_all,
        True,
    )

    # Записваме самата сбирка отделно.
    upsert_meeting(
        date,
        time,
        sender_clean,
        custom,
    )

    # Обновяваме профила веднага, без да чакаме 60-секундния loop.
    await update_bot_profile()

    return JSONResponse(
        {
            "message":
                "✅ Изпратено успешно! "
                "Профилът на бота също е обновен."
        }
    )


@app.post("/history")
async def history(password: str = Form(...)):
    if password != PANEL_PASSWORD:
        return JSONResponse(
            {"message": "Грешна парола!"},
            status_code=401,
        )

    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row

        rows = db.execute(
            """
            SELECT *
            FROM notifications
            ORDER BY id DESC
            LIMIT 20
            """
        ).fetchall()

    return JSONResponse(
        {"items": [dict(row) for row in rows]}
    )


@app.get("/health")
async def health():
    """
    Прост health endpoint, удобен за Apache/reverse proxy проверка.
    Не показва чувствителни данни.
    """
    return JSONResponse(
        {
            "ok": True,
            "discord_ready": bot.is_ready(),
        }
    )


# ============================================================
# Стартиране
# ============================================================

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8000,
    )
