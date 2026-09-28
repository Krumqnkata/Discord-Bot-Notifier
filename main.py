import os
import asyncio
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager

import discord
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, JSONResponse


load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
PANEL_PASSWORD = os.getenv("PANEL_PASSWORD")
TZ = ZoneInfo("Europe/Sofia")

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notifications.db")

DAYS_BG = ["понеделник", "вторник", "сряда", "четвъртък",
           "петък", "събота", "неделя"]

bot = discord.Client(intents=discord.Intents.default())


# ---------- База данни ----------
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
                error TEXT
            )
        """)


def save_notification(meeting_date, meeting_time, sender, custom,
                      tag_everyone, success, error=None):
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            "INSERT INTO notifications (created_at, meeting_date, meeting_time, sender, "
            "custom_message, tag_everyone, success, error) VALUES (?,?,?,?,?,?,?,?)",
            (datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"), meeting_date, meeting_time,
             sender, custom or None, int(tag_everyone), int(success), error),
        )


init_db()


# ---------- Приложение ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(bot.start(TOKEN))
    yield
    await bot.close()
    task.cancel()


app = FastAPI(lifespan=lifespan)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="bg">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Дискорд Известия</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  body { background: radial-gradient(circle at 20% 20%, #1e1b4b, #0f172a 60%); }
  .glass { background: rgba(30,41,59,.65); backdrop-filter: blur(12px);
           border: 1px solid rgba(148,163,184,.15); }
  .field { width:100%; padding:.65rem .8rem; border-radius:.6rem; background:#0f172a;
           border:1px solid #334155; color:#fff; outline:none; transition:.15s; }
  .field:focus { border-color:#6366f1; box-shadow:0 0 0 3px rgba(99,102,241,.25); }
  ::-webkit-calendar-picker-indicator { filter: invert(1); cursor:pointer; }
</style>
</head>
<body class="min-h-screen text-slate-100 p-4 md:p-8 flex items-center justify-center">
<div class="w-full max-w-5xl grid md:grid-cols-2 gap-6">

  <!-- ФОРМА -->
  <form id="form" class="glass rounded-2xl p-6 md:p-8 space-y-4 shadow-2xl">
    <div>
      <h1 class="text-2xl font-bold">📢 Ново събиране</h1>
      <p class="text-sm text-slate-400">Изпрати известие до сървъра в Discord</p>
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
      <label class="block text-xs uppercase tracking-wide text-slate-400 mb-1">Изпратено от</label>
      <input id="sender" type="text" name="sender" maxlength="40" placeholder="Твоето име" required class="field">
    </div>

    <div>
      <label class="block text-xs uppercase tracking-wide text-slate-400 mb-1">
        Допълнително съобщение <span class="normal-case text-slate-500">(по избор)</span>
      </label>
      <textarea id="custom" name="custom" rows="3" maxlength="1000"
        placeholder="Напр. Каним се на Discord канала за проекта..." class="field resize-none"></textarea>
    </div>

    <label class="flex items-center gap-2 text-sm cursor-pointer select-none">
      <input id="everyone" type="checkbox" name="everyone" value="1" checked
             class="w-4 h-4 accent-indigo-500">
      Тагни всички (@everyone)
    </label>

    <div>
      <label class="block text-xs uppercase tracking-wide text-slate-400 mb-1">Парола за достъп</label>
      <input type="password" name="password" required class="field">
    </div>

    <button id="btn" type="submit"
      class="w-full bg-indigo-600 hover:bg-indigo-500 active:scale-[.99] font-semibold py-3 rounded-lg transition">
      Изпрати в Discord
    </button>
    <div id="status" class="text-center text-sm min-h-[1.25rem]"></div>

    <button id="histBtn" type="button"
      class="w-full border border-slate-600 hover:bg-slate-700 text-sm py-2 rounded-lg transition">
      📜 Покажи история
    </button>
    <div id="history" class="text-xs space-y-2 max-h-64 overflow-y-auto"></div>
  </form>

  <!-- ПРЕГЛЕД -->
  <div class="glass rounded-2xl p-6 md:p-8 shadow-2xl self-start">
    <p class="text-xs uppercase tracking-wide text-slate-400 mb-3">Преглед</p>
    <div class="rounded-lg p-4" style="background:#313338">
      <div id="p-everyone" class="mb-2">
        <span class="px-1 rounded" style="background:rgba(88,101,242,.3);color:#c9cdfb">@everyone</span>
      </div>
      <div class="rounded-md p-4" style="background:#2b2d31;border-left:4px solid #5865F2">
        <div class="font-bold mb-2">📢 Ново събиране!</div>
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
        <div class="text-xs text-slate-400 mt-3">Изпратено от <span id="p-sender">…</span></div>
      </div>
    </div>
    <p class="text-xs text-slate-500 mt-3">Така ще изглежда съобщението в канала.</p>
  </div>
</div>

<script>
const DAYS = ["неделя","понеделник","вторник","сряда","четвъртък","петък","събота"];
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

function updatePreview() {
  const d = $("date").value;
  if (d) {
    const [y, m, day] = d.split("-");
    const wd = DAYS[new Date(+y, +m - 1, +day).getDay()];
    $("p-date").textContent = `${day}.${m}.${y} (${wd})`;
  } else $("p-date").textContent = "—";
  $("p-time").textContent = $("time").value ? `${$("time").value} ч.` : "—";
  $("p-sender").textContent = $("sender").value || "…";
  const c = $("custom").value.trim();
  $("p-custom").textContent = c;
  $("p-custom").classList.toggle("hidden", !c);
  $("p-everyone").classList.toggle("hidden", !$("everyone").checked);
}
["date","time","sender","custom","everyone"].forEach(id =>
  $(id).addEventListener("input", updatePreview));
updatePreview();

$("form").addEventListener("submit", async e => {
  e.preventDefault();
  const btn = $("btn"), st = $("status");
  btn.disabled = true; btn.textContent = "Изпращане...";
  st.textContent = "";
  try {
    const res = await fetch("send", { method: "POST", body: new FormData($("form")) });
    const data = await res.json();
    st.textContent = data.message;
    st.className = "text-center text-sm min-h-[1.25rem] " +
      (res.ok ? "text-emerald-400" : "text-red-400");
  } catch {
    st.textContent = "Грешка при връзката със сървъра.";
    st.className = "text-center text-sm min-h-[1.25rem] text-red-400";
  }
  btn.disabled = false; btn.textContent = "Изпрати в Discord";
});

$("histBtn").addEventListener("click", async () => {
  const box = $("history");
  const fd = new FormData();
  fd.append("password", document.querySelector('input[name="password"]').value);
  try {
    const res = await fetch("history", { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok) {
      box.innerHTML = `<div class="text-red-400">${esc(data.message)}</div>`;
      return;
    }
    if (!data.items.length) {
      box.innerHTML = '<div class="text-slate-400">Още няма изпратени известия.</div>';
      return;
    }
    box.innerHTML = data.items.map(i => `
      <div class="rounded-lg p-3 bg-slate-900/60 border border-slate-700">
        <div class="flex justify-between">
          <span class="font-semibold">${esc(i.meeting_date)} • ${esc(i.meeting_time)}</span>
          <span>${i.success ? "✅" : "❌"}</span>
        </div>
        <div class="text-slate-400">от ${esc(i.sender)} • ${esc(i.created_at)}</div>
        ${i.custom_message ? `<div class="mt-1 text-slate-300">${esc(i.custom_message)}</div>` : ""}
        ${i.error ? `<div class="mt-1 text-red-400">${esc(i.error)}</div>` : ""}
      </div>`).join("");
  } catch {
    box.innerHTML = '<div class="text-red-400">Грешка при връзката със сървъра.</div>';
  }
});
</script>
</body>
</html>
"""


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
        return JSONResponse({"message": "Грешна парола!"}, status_code=401)

    try:
        d = datetime.strptime(date, "%Y-%m-%d")
        t = datetime.strptime(time, "%H:%M")
    except ValueError:
        return JSONResponse({"message": "Невалидна дата или час."}, status_code=400)

    sender_clean = sender.strip()[:40]
    custom = custom.strip()
    tag_all = bool(everyone)

    await bot.wait_until_ready()
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        try:
            channel = await bot.fetch_channel(CHANNEL_ID)
        except discord.DiscordException:
            save_notification(date, time, sender_clean, custom, tag_all,
                              False, "Каналът не е намерен")
            return JSONResponse({"message": "Каналът не е намерен."}, status_code=500)

    when = d.replace(hour=t.hour, minute=t.minute, tzinfo=TZ)

    embed = discord.Embed(
        title="📢 Ново събиране!",
        description=custom or None,
        color=0x5865F2,
    )
    embed.add_field(
        name="📅 Дата",
        value=f"{d:%d.%m.%Y}\n{DAYS_BG[d.weekday()]}",
        inline=True,
    )
    embed.add_field(name="⏰ Час", value=f"{time} ч.", inline=True)
    embed.add_field(name="⏳ Остава", value=f"<t:{int(when.timestamp())}:R>", inline=True)
    embed.set_footer(text=f"Изпратено от {sender_clean}")

    try:
        await channel.send(
            content="@everyone" if tag_all else None,
            embed=embed,
            allowed_mentions=discord.AllowedMentions(
                everyone=tag_all, users=False, roles=False
            ),
        )
    except discord.Forbidden:
        save_notification(date, time, sender_clean, custom, tag_all, False, "Forbidden")
        return JSONResponse(
            {"message": "Ботът няма права (Send Messages / Embed Links / Mention Everyone)."},
            status_code=500,
        )
    except discord.DiscordException as e:
        save_notification(date, time, sender_clean, custom, tag_all, False, str(e)[:200])
        return JSONResponse({"message": "Грешка от Discord."}, status_code=500)

    save_notification(date, time, sender_clean, custom, tag_all, True)
    return JSONResponse({"message": "✅ Изпратено успешно!"})


@app.post("/history")
async def history(password: str = Form(...)):
    if password != PANEL_PASSWORD:
        return JSONResponse({"message": "Грешна парола!"}, status_code=401)

    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT * FROM notifications ORDER BY id DESC LIMIT 20"
        ).fetchall()

    return JSONResponse({"items": [dict(r) for r in rows]})


if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000)