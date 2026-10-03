"""Start the existing notifier, club commands, and music add-on in one process."""
from contextlib import asynccontextmanager
from dataclasses import asdict
import os
from pathlib import Path

import uvicorn

import main
from club_admin import install_admin
from club_fun import ROASTS, SCAMMER_LINES, Settings, install
import club_music
from club_music import install as install_music
from club_store import ClubStore

# Optional yt-dlp authentication/config for VPS environments where YouTube
# challenges datacenter IPs. Keep cookies outside Git and point to them from .env.
cookie_file = os.getenv("MUSIC_YTDLP_COOKIES", "").strip()
if cookie_file:
    cookie_path = Path(cookie_file).expanduser()
    if not cookie_path.is_absolute():
        cookie_path = Path(__file__).resolve().parent / cookie_path
    club_music.YTDLP_OPTIONS["cookiefile"] = str(cookie_path)

youtube_user_agent = os.getenv("MUSIC_YTDLP_USER_AGENT", "").strip()
if youtube_user_agent:
    club_music.YTDLP_OPTIONS["http_headers"] = {
        "User-Agent": youtube_user_agent,
    }

defaults = Settings.from_env()
initial_settings = asdict(defaults)
for key in ("text_channels", "voice_channels"):
    initial_settings[key] = sorted(initial_settings[key])

store = ClubStore(
    Path(main.DB_PATH).with_name("club_fun.db"),
    initial_settings,
    ROASTS,
    SCAMMER_LINES,
)
fun = install(main.bot, main.tree, defaults, store)
music = install_music(main.bot, main.tree)
install_admin(main, store)

app = main.app
notifier_lifespan = app.router.lifespan_context


@asynccontextmanager
async def lifespan(app):
    async with notifier_lifespan(app):
        try:
            yield
        finally:
            await music.close()
            await fun.close()


app.router.lifespan_context = lifespan

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
