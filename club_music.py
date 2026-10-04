"""Music add-on for the existing IT Club Discord bot.

Accepts plain song searches, YouTube URLs, Spotify track URLs, and other
single-item URLs supported by yt-dlp. Spotify is used to identify the song;
audio playback is resolved through yt-dlp (normally from YouTube).
"""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
import json
import logging
import os
import re
import shutil
from typing import Deque
from urllib.parse import quote
from urllib.request import Request, urlopen

import discord
from discord import app_commands
import yt_dlp

log = logging.getLogger(__name__)

POT_PROVIDER_URL = os.getenv(
    "MUSIC_POT_PROVIDER_URL",
    "http://127.0.0.1:4416",
).strip()

COOKIE_FILE = os.getenv(
    "MUSIC_YTDLP_COOKIES",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "youtube-cookies.txt"),
).strip()

YTDLP_OPTIONS = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "default_search": "ytsearch1",
    "skip_download": True,
    "extract_flat": False,
    "source_address": "0.0.0.0",
    # Keep the PO-token provider available, but let yt-dlp choose the YouTube
    # client automatically. Account cookies currently work with the default
    # client selection on this VPS, while forcing mweb can trigger LOGIN_REQUIRED.
    "extractor_args": {
        "youtubepot-bgutilhttp": {
            "base_url": [POT_PROVIDER_URL],
        },
    },
}

if COOKIE_FILE and os.path.isfile(COOKIE_FILE):
    YTDLP_OPTIONS["cookiefile"] = COOKIE_FILE
else:
    log.warning(
        "Music cookie file not found at %s; YouTube may reject VPS requests",
        COOKIE_FILE,
    )

FFMPEG_BEFORE_OPTIONS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTIONS = "-vn"

MAX_QUEUE_SIZE = max(1, int(os.getenv("MUSIC_MAX_QUEUE", "25")))
DEFAULT_VOLUME = max(0, min(100, int(os.getenv("MUSIC_DEFAULT_VOLUME", "60"))))

SPOTIFY_RE = re.compile(
    r"https?://(?:open\.)?spotify\.com/(?P<kind>track|album|playlist)/(?P<id>[A-Za-z0-9]+)",
    re.IGNORECASE,
)


class MusicError(Exception):
    """Short error safe to show to a Discord user."""


@dataclass(slots=True)
class Track:
    query: str
    webpage_url: str
    title: str
    duration: int | None
    requested_by: str
    source_label: str = "YouTube/търсене"

    @property
    def duration_text(self) -> str:
        if not self.duration:
            return "на живо/неизвестно"
        minutes, seconds = divmod(self.duration, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"


@dataclass
class GuildPlayer:
    guild_id: int
    queue: Deque[Track] = field(default_factory=deque)
    current: Track | None = None
    volume: int = DEFAULT_VOLUME
    worker: asyncio.Task | None = None
    stopping: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class MusicService:
    def __init__(self, bot: discord.Client):
        self.bot = bot
        self.players: dict[int, GuildPlayer] = {}

    def player(self, guild_id: int) -> GuildPlayer:
        return self.players.setdefault(guild_id, GuildPlayer(guild_id))

    async def error(self, interaction: discord.Interaction, message: str):
        kwargs = {
            "ephemeral": True,
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        if interaction.response.is_done():
            await interaction.followup.send(message, **kwargs)
        else:
            await interaction.response.send_message(message, **kwargs)

    def check_guild(self, interaction: discord.Interaction) -> discord.Member:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            raise MusicError("Тази команда работи само в Discord сървър.")
        return interaction.user

    def requester_channel(self, interaction: discord.Interaction) -> discord.VoiceChannel:
        member = self.check_guild(interaction)
        state = member.voice
        if state is None or not isinstance(state.channel, discord.VoiceChannel):
            raise MusicError("Първо влез в обикновен гласов канал.")
        return state.channel

    def ensure_same_voice(self, interaction: discord.Interaction) -> discord.VoiceClient:
        channel = self.requester_channel(interaction)
        voice = interaction.guild.voice_client
        if voice is None or not voice.is_connected():
            raise MusicError("Ботът не е свързан с гласов канал.")
        if voice.channel != channel:
            raise MusicError("Трябва да си в същия гласов канал като бота.")
        return voice

    async def spotify_search_text(self, url: str) -> str:
        match = SPOTIFY_RE.search(url)
        if not match:
            raise MusicError("Невалиден Spotify линк.")

        kind = match.group("kind").lower()
        if kind != "track":
            raise MusicError(
                "Засега Spotify поддръжката е за отделни песни. "
                "Album и playlist линкове ще добавим отделно."
            )

        def fetch_oembed() -> str:
            endpoint = "https://open.spotify.com/oembed?url=" + quote(url, safe="")
            request = Request(endpoint, headers={"User-Agent": "ITClubDiscordBot/1.0"})
            with urlopen(request, timeout=10) as response:
                payload = json.load(response)
            return str(payload.get("title") or "").strip()

        try:
            title = await asyncio.wait_for(asyncio.to_thread(fetch_oembed), timeout=12)
        except Exception as exc:
            log.warning("Spotify metadata lookup failed: %s", exc)
            raise MusicError("Не успях да прочета този Spotify линк.") from exc

        if not title:
            raise MusicError("Spotify не върна заглавие за тази песен.")

        title = re.sub(r"\s*\|\s*Spotify\s*$", "", title, flags=re.IGNORECASE)
        title = re.sub(
            r"\s*-\s*song and lyrics by\s+",
            " ",
            title,
            flags=re.IGNORECASE,
        )
        return title.strip()

    async def normalize_query(self, query: str) -> tuple[str, str]:
        query = query.strip()
        if not query:
            raise MusicError("Напиши име на песен или постави линк.")

        spotify_match = SPOTIFY_RE.search(query)
        if spotify_match:
            search_text = await self.spotify_search_text(query)
            return f"ytsearch1:{search_text}", "Spotify → YouTube"

        return query, "YouTube/търсене"

    async def extract(self, query: str) -> dict:
        if not shutil.which("ffmpeg"):
            raise MusicError(
                "Липсва FFmpeg на сървъра. Инсталирай: sudo apt install ffmpeg"
            )

        def do_extract():
            with yt_dlp.YoutubeDL(YTDLP_OPTIONS) as ydl:
                return ydl.extract_info(query, download=False)

        try:
            info = await asyncio.wait_for(asyncio.to_thread(do_extract), timeout=30)
        except asyncio.TimeoutError as exc:
            raise MusicError("Търсенето отне твърде дълго. Опитай пак.") from exc
        except Exception as exc:
            log.warning("yt-dlp lookup failed for %r: %s", query, exc)
            raise MusicError(
                "Не успях да намеря или отворя този аудио източник."
            ) from exc

        if info and "entries" in info:
            entries = [entry for entry in (info.get("entries") or []) if entry]
            info = entries[0] if entries else None

        if not info:
            raise MusicError("Няма намерен резултат.")

        return info

    async def track_from_query(
        self,
        query: str,
        requester: discord.Member,
    ) -> Track:
        normalized, source_label = await self.normalize_query(query)
        info = await self.extract(normalized)
        webpage_url = (
            info.get("webpage_url")
            or info.get("original_url")
            or normalized
        )
        return Track(
            query=query,
            webpage_url=webpage_url,
            title=info.get("title") or "Неизвестно заглавие",
            duration=info.get("duration"),
            requested_by=requester.display_name,
            source_label=source_label,
        )

    async def stream_url(self, track: Track) -> tuple[str, str]:
        # Refresh immediately before playback because signed stream URLs can expire
        # while a song waits in the queue.
        info = await self.extract(track.webpage_url)
        stream = info.get("url")
        if not stream:
            raise MusicError("Източникът не върна аудио поток.")
        return stream, info.get("title") or track.title

    async def connect(
        self,
        interaction: discord.Interaction,
    ) -> discord.VoiceClient:
        channel = self.requester_channel(interaction)
        guild = interaction.guild
        existing = guild.voice_client

        if existing is not None:
            if existing.channel != channel:
                raise MusicError(
                    f"Ботът вече е в {existing.channel.name}. "
                    "Влез там или първо използвай /music leave."
                )
            return existing

        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.connect and perms.speak):
            raise MusicError(
                "Ботът се нуждае от View Channel, Connect и Speak."
            )

        try:
            return await channel.connect(
                timeout=20,
                reconnect=True,
                self_deaf=True,
            )
        except discord.ClientException as exc:
            raise MusicError(
                "Не успях да се свържа с гласовия канал."
            ) from exc

    def ensure_worker(self, guild: discord.Guild):
        player = self.player(guild.id)
        if player.worker is None or player.worker.done():
            player.stopping = False
            player.worker = asyncio.create_task(
                self.worker_loop(guild),
                name=f"music-player-{guild.id}",
            )

    async def worker_loop(self, guild: discord.Guild):
        player = self.player(guild.id)
        try:
            while not player.stopping:
                async with player.lock:
                    if not player.queue:
                        player.current = None
                        return
                    track = player.queue.popleft()
                    player.current = track

                voice = guild.voice_client
                if voice is None or not voice.is_connected():
                    async with player.lock:
                        player.queue.appendleft(track)
                        player.current = None
                    return

                source = None
                try:
                    stream_url, fresh_title = await self.stream_url(track)
                    track.title = fresh_title

                    pcm = discord.FFmpegPCMAudio(
                        stream_url,
                        before_options=FFMPEG_BEFORE_OPTIONS,
                        options=FFMPEG_OPTIONS,
                    )
                    source = discord.PCMVolumeTransformer(
                        pcm,
                        volume=player.volume / 100,
                    )

                    loop = asyncio.get_running_loop()
                    finished = loop.create_future()

                    def after(error):
                        def finish():
                            if finished.done():
                                return
                            if error:
                                finished.set_exception(error)
                            else:
                                finished.set_result(None)
                        loop.call_soon_threadsafe(finish)

                    voice.play(source, after=after)
                    await finished
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception(
                        "Music playback failed in guild %s",
                        guild.id,
                    )
                finally:
                    if source is not None:
                        source.cleanup()
                    player.current = None
        finally:
            player.worker = None
            player.current = None

    async def play(self, interaction: discord.Interaction, query: str):
        try:
            member = self.check_guild(interaction)
            await interaction.response.defer(thinking=True)

            player = self.player(interaction.guild_id)
            async with player.lock:
                if len(player.queue) >= MAX_QUEUE_SIZE:
                    raise MusicError(
                        f"Опашката е пълна ({MAX_QUEUE_SIZE} песни)."
                    )

            track = await self.track_from_query(query, member)
            voice = await self.connect(interaction)

            was_idle = (
                not voice.is_playing()
                and not voice.is_paused()
                and player.current is None
            )
            async with player.lock:
                player.queue.append(track)

            self.ensure_worker(interaction.guild)

            prefix = "▶️ Пускам" if was_idle else "➕ Добавих в опашката"
            await interaction.followup.send(
                f"{prefix}: **{track.title}** · {track.duration_text} "
                f"· заявено от **{track.requested_by}** · {track.source_label}",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except MusicError as exc:
            await self.error(interaction, str(exc))
        except Exception:
            log.exception("Unexpected /music play error")
            await self.error(
                interaction,
                "Възникна неочаквана грешка при пускането на музиката.",
            )

    async def pause(self, interaction: discord.Interaction):
        try:
            voice = self.ensure_same_voice(interaction)
            if not voice.is_playing():
                raise MusicError("В момента няма песен, която да паузирам.")
            voice.pause()
            await interaction.response.send_message("⏸️ Паузирах музиката.")
        except MusicError as exc:
            await self.error(interaction, str(exc))

    async def resume(self, interaction: discord.Interaction):
        try:
            voice = self.ensure_same_voice(interaction)
            if not voice.is_paused():
                raise MusicError("Музиката не е на пауза.")
            voice.resume()
            await interaction.response.send_message("▶️ Продължавам.")
        except MusicError as exc:
            await self.error(interaction, str(exc))

    async def skip(self, interaction: discord.Interaction):
        try:
            voice = self.ensure_same_voice(interaction)
            if not (voice.is_playing() or voice.is_paused()):
                raise MusicError("Няма активна песен за прескачане.")
            voice.stop()
            await interaction.response.send_message("⏭️ Прескочих песента.")
        except MusicError as exc:
            await self.error(interaction, str(exc))

    async def now(self, interaction: discord.Interaction):
        try:
            self.check_guild(interaction)
            player = self.player(interaction.guild_id)
            track = player.current
            if track is None:
                raise MusicError("В момента не свири нищо.")
            await interaction.response.send_message(
                f"🎵 Сега: **{track.title}** · {track.duration_text} "
                f"· заявено от **{track.requested_by}**"
            )
        except MusicError as exc:
            await self.error(interaction, str(exc))

    async def show_queue(self, interaction: discord.Interaction):
        try:
            self.check_guild(interaction)
            player = self.player(interaction.guild_id)

            async with player.lock:
                items = list(player.queue)

            lines = []
            if player.current:
                lines.append(f"🎵 **Сега:** {player.current.title}")

            if items:
                lines.append("")
                lines.append("**Следващи:**")
                for index, track in enumerate(items[:10], start=1):
                    lines.append(
                        f"{index}. {track.title} · {track.duration_text} "
                        f"· {track.requested_by}"
                    )
                if len(items) > 10:
                    lines.append(f"…и още {len(items) - 10}.")

            if not lines:
                lines.append("📭 Опашката е празна.")

            await interaction.response.send_message(
                "\n".join(lines),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except MusicError as exc:
            await self.error(interaction, str(exc))

    async def set_volume(
        self,
        interaction: discord.Interaction,
        percent: app_commands.Range[int, 0, 100],
    ):
        try:
            self.ensure_same_voice(interaction)
            player = self.player(interaction.guild_id)
            player.volume = int(percent)

            voice = interaction.guild.voice_client
            if (
                voice is not None
                and isinstance(voice.source, discord.PCMVolumeTransformer)
            ):
                voice.source.volume = player.volume / 100

            await interaction.response.send_message(
                f"🔊 Сила на звука: **{player.volume}%**"
            )
        except MusicError as exc:
            await self.error(interaction, str(exc))

    async def leave(self, interaction: discord.Interaction):
        try:
            voice = self.ensure_same_voice(interaction)
            player = self.player(interaction.guild_id)
            player.stopping = True

            async with player.lock:
                player.queue.clear()

            if voice.is_playing() or voice.is_paused():
                voice.stop()

            if player.worker:
                player.worker.cancel()
                with suppress(asyncio.CancelledError):
                    await player.worker
                player.worker = None

            await voice.disconnect(force=True)
            player.current = None
            player.stopping = False

            await interaction.response.send_message(
                "👋 Спрях музиката, изчистих опашката и излязох."
            )
        except MusicError as exc:
            await self.error(interaction, str(exc))

    async def close(self):
        for player in self.players.values():
            player.stopping = True
            if player.worker:
                player.worker.cancel()

        await asyncio.gather(
            *(player.worker for player in self.players.values() if player.worker),
            return_exceptions=True,
        )
        self.players.clear()


def install(
    bot: discord.Client,
    tree: app_commands.CommandTree,
) -> MusicService:
    """Register /music on the existing bot and command tree."""
    service = MusicService(bot)

    music = app_commands.Group(
        name="music",
        description="Музикален плеър за гласовия канал",
    )

    @music.command(
        name="play",
        description="Пуска песен по име, YouTube линк или Spotify track линк",
    )
    @app_commands.describe(
        query="Име на песен, YouTube линк или Spotify track линк"
    )
    async def play(interaction: discord.Interaction, query: str):
        await service.play(interaction, query)

    @music.command(name="pause", description="Паузира текущата песен")
    async def pause(interaction: discord.Interaction):
        await service.pause(interaction)

    @music.command(name="resume", description="Продължава паузираната песен")
    async def resume(interaction: discord.Interaction):
        await service.resume(interaction)

    @music.command(name="skip", description="Прескача текущата песен")
    async def skip(interaction: discord.Interaction):
        await service.skip(interaction)

    @music.command(name="queue", description="Показва текущата музикална опашка")
    async def queue(interaction: discord.Interaction):
        await service.show_queue(interaction)

    @music.command(name="now", description="Показва какво свири в момента")
    async def now(interaction: discord.Interaction):
        await service.now(interaction)

    @music.command(
        name="volume",
        description="Задава сила на звука от 0 до 100 процента",
    )
    @app_commands.describe(percent="Сила на звука от 0 до 100")
    async def volume(
        interaction: discord.Interaction,
        percent: app_commands.Range[int, 0, 100],
    ):
        await service.set_volume(interaction, percent)

    @music.command(
        name="leave",
        description="Спира музиката, чисти опашката и изкарва бота",
    )
    async def leave(interaction: discord.Interaction):
        await service.leave(interaction)

    tree.add_command(music)
    return service
