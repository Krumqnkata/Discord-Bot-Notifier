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
import sys
from typing import Deque
from urllib.parse import quote
from urllib.request import Request, urlopen

import discord
from discord import app_commands

log = logging.getLogger(__name__)

COOKIE_FILE = os.getenv(
    "MUSIC_YTDLP_COOKIES",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "youtube-cookies.txt"),
).strip()

if not (COOKIE_FILE and os.path.isfile(COOKIE_FILE)):
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

YOUTUBE_VIDEO_RE = re.compile(
    r"https?://(?:(?:www\.)?youtube\.com/(?:watch\?v=|shorts/|live/|embed/)|youtu\.be/)(?P<id>[A-Za-z0-9_-]{6,})",
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

    @staticmethod
    def _duration_value(value: str | None) -> int | None:
        if not value or value in {"NA", "None", "null"}:
            return None
        try:
            return max(0, int(float(value)))
        except (TypeError, ValueError):
            return None

    async def youtube_oembed_title(self, url: str) -> str:
        def fetch() -> str:
            endpoint = (
                "https://www.youtube.com/oembed?format=json&url="
                + quote(url, safe="")
            )
            request = Request(endpoint, headers={"User-Agent": "ITClubDiscordBot/1.0"})
            with urlopen(request, timeout=10) as response:
                payload = json.load(response)
            return str(payload.get("title") or "").strip()

        try:
            return await asyncio.wait_for(asyncio.to_thread(fetch), timeout=12)
        except Exception as exc:
            log.info("YouTube oEmbed metadata unavailable for %r: %s", url, exc)
            return ""

    async def cli_search_track(
        self,
        search_text: str,
        requester_name: str,
        source_label: str,
    ) -> Track:
        base = self._cli_base_args()
        template = "%(id)s\t%(title)s\t%(duration)s\t%(webpage_url)s"
        out, _ = await self._run_cli([
            *base,
            "--flat-playlist",
            "--playlist-end", "1",
            "--print", template,
            f"ytsearch1:{search_text}",
        ])

        rows = [
            line.strip()
            for line in out.splitlines()
            if line.strip() and not line.lstrip().startswith("[")
        ]
        if not rows:
            raise MusicError("YouTube търсенето не върна резултат.")

        parts = rows[-1].split("\t", 3)
        if len(parts) < 2:
            raise MusicError("Не успях да прочета резултата от YouTube търсенето.")

        video_id = parts[0].strip()
        title = parts[1].strip() or "Неизвестно заглавие"
        duration = self._duration_value(parts[2].strip() if len(parts) > 2 else None)
        webpage_url = parts[3].strip() if len(parts) > 3 else ""
        if not webpage_url or webpage_url in {"NA", "None"}:
            webpage_url = f"https://www.youtube.com/watch?v={video_id}"

        return Track(
            query=search_text,
            webpage_url=webpage_url,
            title=title,
            duration=duration,
            requested_by=requester_name,
            source_label=source_label,
        )

    async def track_from_query(
        self,
        query: str,
        requester: discord.Member | str,
    ) -> Track:
        query = query.strip()
        if not query:
            raise MusicError("Напиши име на песен или постави линк.")

        requester_name = (
            requester.display_name
            if isinstance(requester, discord.Member)
            else str(requester)
        )

        spotify_match = SPOTIFY_RE.search(query)
        if spotify_match:
            search_text = await self.spotify_search_text(query)
            return await self.cli_search_track(
                search_text,
                requester_name,
                "Spotify → YouTube",
            )

        youtube_match = YOUTUBE_VIDEO_RE.search(query)
        if youtube_match:
            title = await self.youtube_oembed_title(query)
            return Track(
                query=query,
                webpage_url=query,
                title=title or f"YouTube видео {youtube_match.group('id')}",
                duration=None,
                requested_by=requester_name,
                source_label="YouTube/линк",
            )

        if re.match(r"^https?://", query, flags=re.IGNORECASE):
            # Keep other yt-dlp-supported URLs usable. Stream resolution is
            # still done by the proven yt-dlp CLI path.
            return Track(
                query=query,
                webpage_url=query,
                title=query,
                duration=None,
                requested_by=requester_name,
                source_label="Външен линк",
            )

        return await self.cli_search_track(
            query,
            requester_name,
            "YouTube/търсене",
        )

    async def _run_cli(self, args: list[str], *, timeout: int = 35) -> tuple[str, str]:
        """Run the same yt-dlp CLI installed in this Python environment."""
        cmd = [sys.executable, "-m", "yt_dlp", *args]
        env = os.environ.copy()

        # Deno was installed for the krum account on this VPS. Preserve PATH,
        # but also add the common per-user Deno location when present.
        deno_dir = os.getenv("MUSIC_DENO_DIR", "/home/krum/.deno/bin")
        if deno_dir and os.path.isdir(deno_dir):
            env["PATH"] = deno_dir + os.pathsep + env.get("PATH", "")

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            raise MusicError("yt-dlp отне твърде дълго. Опитай пак.")

        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")
        if process.returncode != 0:
            detail = (err or out).strip().splitlines()
            last = detail[-1] if detail else f"exit {process.returncode}"
            raise MusicError(f"yt-dlp CLI грешка: {last}")
        return out, err

    def _cli_base_args(self) -> list[str]:
        args = ["--no-playlist", "--no-color"]
        if COOKIE_FILE and os.path.isfile(COOKIE_FILE):
            args += ["--cookies", COOKIE_FILE]
        return args

    @staticmethod
    def _choose_format_from_table(table: str) -> str:
        """Choose a format ID from yt-dlp -F output (which is sorted worst→best)."""
        audio_only: list[str] = []
        combined: list[str] = []

        for raw_line in table.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("["):
                continue

            parts = line.split(None, 2)
            if len(parts) < 3:
                continue
            format_id, ext, rest = parts
            if format_id.upper() == "ID" or ext.upper() == "EXT":
                continue

            lower = line.lower()
            if any(word in lower for word in ("storyboard", "images", "mhtml")):
                continue
            if "video only" in lower:
                continue

            if "audio only" in lower:
                audio_only.append(format_id)
            else:
                # A normal muxed row from -F contains both video and audio.
                combined.append(format_id)

        if audio_only:
            return audio_only[-1]
        if combined:
            return combined[-1]
        raise MusicError("yt-dlp -F не показа формат с аудио.")

    async def cli_stream_url(self, url: str) -> str:
        """Resolve a stream by reproducing the yt-dlp CLI path that works on the VPS."""
        base = self._cli_base_args()

        table, _ = await self._run_cli([*base, "-F", url])
        format_id = self._choose_format_from_table(table)
        log.info("yt-dlp CLI selected format %s for %s", format_id, url)

        out, _ = await self._run_cli([*base, "-f", format_id, "-g", url])
        urls = [
            line.strip()
            for line in out.splitlines()
            if line.strip().startswith(("http://", "https://"))
        ]
        if not urls:
            raise MusicError(
                f"yt-dlp показа формат {format_id}, но не върна директен URL."
            )
        return urls[-1]

    async def stream_url(self, track: Track) -> tuple[str, str]:
        stream = await self.cli_stream_url(track.webpage_url)
        return stream, track.title

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

    def _admin_guild(self, guild_id: int) -> discord.Guild:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            raise MusicError("Discord сървърът не е намерен.")
        return guild

    def admin_guilds(self) -> list[dict]:
        result = []
        for guild in sorted(self.bot.guilds, key=lambda item: item.name.lower()):
            channels = [
                {
                    "id": str(channel.id),
                    "name": channel.name,
                }
                for channel in guild.voice_channels
            ]
            result.append(
                {
                    "id": str(guild.id),
                    "name": guild.name,
                    "voice_channels": channels,
                }
            )
        return result

    @staticmethod
    def _track_payload(track: Track | None) -> dict | None:
        if track is None:
            return None
        return {
            "title": track.title,
            "duration": track.duration_text,
            "requested_by": track.requested_by,
            "source": track.source_label,
            "url": track.webpage_url,
        }

    async def admin_snapshot(self, guild_id: int) -> dict:
        guild = self._admin_guild(guild_id)
        player = self.player(guild_id)
        async with player.lock:
            queued = list(player.queue)

        voice = guild.voice_client
        return {
            "guild_id": str(guild.id),
            "guild_name": guild.name,
            "connected": bool(voice and voice.is_connected()),
            "voice_channel_id": (
                str(voice.channel.id)
                if voice and voice.is_connected() and voice.channel
                else None
            ),
            "voice_channel_name": (
                voice.channel.name
                if voice and voice.is_connected() and voice.channel
                else None
            ),
            "playing": bool(voice and voice.is_playing()),
            "paused": bool(voice and voice.is_paused()),
            "volume": player.volume,
            "current": self._track_payload(player.current),
            "queue": [
                {"index": index, **self._track_payload(track)}
                for index, track in enumerate(queued, start=1)
            ],
        }

    async def _admin_connect(
        self,
        guild: discord.Guild,
        channel_id: int,
    ) -> discord.VoiceClient:
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            raise MusicError("Гласовият канал не е намерен.")

        voice = guild.voice_client
        player = self.player(guild.id)

        if (
            voice is not None
            and (voice.is_playing() or voice.is_paused())
            and player.current is None
        ):
            raise MusicError(
                "Ботът в момента използва voice канала за друга команда."
            )

        if voice is not None and voice.is_connected():
            if voice.channel != channel:
                try:
                    await voice.move_to(channel)
                except discord.DiscordException as exc:
                    raise MusicError(
                        "Не успях да преместя бота в избрания voice канал."
                    ) from exc
            return voice

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
        except discord.DiscordException as exc:
            raise MusicError(
                "Не успях да се свържа с гласовия канал."
            ) from exc

    async def admin_play(
        self,
        guild_id: int,
        voice_channel_id: int,
        query: str,
    ) -> dict:
        guild = self._admin_guild(guild_id)
        player = self.player(guild_id)

        async with player.lock:
            if len(player.queue) >= MAX_QUEUE_SIZE:
                raise MusicError(
                    f"Опашката е пълна ({MAX_QUEUE_SIZE} песни)."
                )

        track = await self.track_from_query(query, "Уеб панел")
        voice = await self._admin_connect(guild, voice_channel_id)
        was_idle = (
            not voice.is_playing()
            and not voice.is_paused()
            and player.current is None
        )

        async with player.lock:
            player.queue.append(track)

        self.ensure_worker(guild)
        return {
            "title": track.title,
            "duration": track.duration_text,
            "started": was_idle,
        }

    async def admin_pause(self, guild_id: int) -> dict:
        guild = self._admin_guild(guild_id)
        voice = guild.voice_client
        if voice is None or not voice.is_playing():
            raise MusicError("В момента няма песен, която да паузирам.")
        voice.pause()
        return {"paused": True}

    async def admin_resume(self, guild_id: int) -> dict:
        guild = self._admin_guild(guild_id)
        voice = guild.voice_client
        if voice is None or not voice.is_paused():
            raise MusicError("Музиката не е на пауза.")
        voice.resume()
        return {"paused": False}

    async def admin_skip(self, guild_id: int) -> dict:
        guild = self._admin_guild(guild_id)
        voice = guild.voice_client
        if voice is None or not (voice.is_playing() or voice.is_paused()):
            raise MusicError("Няма активна песен за прескачане.")
        voice.stop()
        return {"skipped": True}

    async def admin_clear_queue(self, guild_id: int) -> dict:
        self._admin_guild(guild_id)
        player = self.player(guild_id)
        async with player.lock:
            removed = len(player.queue)
            player.queue.clear()
        return {"removed": removed}

    async def admin_remove(self, guild_id: int, index: int) -> dict:
        self._admin_guild(guild_id)
        player = self.player(guild_id)
        async with player.lock:
            items = list(player.queue)
            if index < 1 or index > len(items):
                raise MusicError("Тази позиция вече не съществува в опашката.")
            removed = items.pop(index - 1)
            player.queue = deque(items)
        return {"title": removed.title}

    async def admin_set_volume(self, guild_id: int, percent: int) -> dict:
        guild = self._admin_guild(guild_id)
        player = self.player(guild_id)
        player.volume = max(0, min(100, int(percent)))

        voice = guild.voice_client
        if (
            voice is not None
            and isinstance(voice.source, discord.PCMVolumeTransformer)
        ):
            voice.source.volume = player.volume / 100

        return {"volume": player.volume}

    async def admin_leave(self, guild_id: int) -> dict:
        guild = self._admin_guild(guild_id)
        voice = guild.voice_client
        player = self.player(guild_id)
        player.stopping = True

        async with player.lock:
            player.queue.clear()

        if voice is not None and (voice.is_playing() or voice.is_paused()):
            voice.stop()

        if player.worker:
            player.worker.cancel()
            with suppress(asyncio.CancelledError):
                await player.worker
            player.worker = None

        if voice is not None and voice.is_connected():
            await voice.disconnect(force=True)

        player.current = None
        player.stopping = False
        return {"disconnected": True}

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
