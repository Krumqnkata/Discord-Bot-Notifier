"""Optional slash commands. No second client, token, or message-content intent."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import random
import shutil
import tempfile
import time

import discord
from discord import app_commands

log = logging.getLogger(__name__)

# Edit these fixed lines to add the club's own jokes. No arbitrary TTS input.
ROASTS = (
    "Ебаси кода — и кошчето отказва да го приеме!",
    "Мамка му, пак ли дебъгваш с молитва и рестарт?",
    "По дяволите, ти си единственият бъг, който оцелява след преинсталация!",
    "Ебаси майстора — написа Hello World и срина локалната мрежа!",
    "Мамка му, сложи една точка и запетая, че компилаторът вече плаче!",
    "Да му се не види, паролата ти е по-силна от аргументите ти!",
)
SCAMMER_LINES = (
    "Hello, this is totally legitimate potato technical support. "
    "Your computer has downloaded too much RAM. Please remain calm. "
    "I am hacking the school toaster. Access denied. The toaster has a better password than me. "
    "This was a comedy call. Goodbye!",
    "Hello, IT club? Very urgent technical nonsense. Your Wi-Fi has escaped through the window. "
    "I am entering the mainframe. One moment. My mother is calling. "
    "Mission cancelled. Please reboot the sandwich. This was a comedy call. Goodbye!",
    "Hello, this is the Department of Suspicious Computer Noises. "
    "I have hacked your calculator. Two plus two is now a premium feature. "
    "Wait. You are using Linux? My entire script has stopped working. "
    "This was a comedy call. Goodbye!",
)


@dataclass(frozen=True)
class Settings:
    allow_everyone: bool = False
    text_channels: frozenset[int] = frozenset()
    voice_channels: frozenset[int] = frozenset()

    @classmethod
    def from_env(cls):
        def ids(name):
            try:
                result = frozenset(int(x.strip()) for x in os.getenv(name, "").split(",") if x.strip())
                if any(x <= 0 for x in result):
                    raise ValueError
                return result
            except ValueError as exc:
                raise RuntimeError(f"{name} трябва да съдържа ID-та, разделени със запетая") from exc

        return cls(os.getenv("FUN_ALLOW_EVERYONE", "0") == "1",
                   ids("FUN_TEXT_CHANNEL_IDS"), ids("FUN_VOICE_CHANNEL_IDS"))


class FunError(Exception):
    """A short, safe error that can be shown privately in Discord."""


class Cooldowns:
    def __init__(self):
        self.deadlines = {}

    def take(self, key: tuple, seconds: float):
        now = time.monotonic()
        remaining = self.deadlines.get(key, 0) - now
        if remaining > 0:
            raise FunError(f"Почакай още {int(remaining) + 1} сек., преди да повториш.")
        self.deadlines = {k: v for k, v in self.deadlines.items() if v > now}
        self.deadlines[key] = now + seconds


def mention_policy(settings, interaction, member, everyone):
    if everyone:
        if member is not None:
            raise FunError("Избери човек ИЛИ всички, не и двете.")
        if not settings.allow_everyone:
            raise FunError("Масовото тагване е изключено. Настройката е FUN_ALLOW_EVERYONE=1.")
        if not interaction.user.guild_permissions.administrator:
            raise FunError("Само администратор може да използва everyone:true.")
        if not interaction.app_permissions.mention_everyone:
            raise FunError("Ботът няма право Mention @everyone в този канал.")
        return "@everyone", discord.AllowedMentions(everyone=True, users=False, roles=False, replied_user=False)
    target = member or interaction.user
    return target.mention, discord.AllowedMentions(everyone=False, users=[target], roles=False, replied_user=False)


async def synthesize(text: str, language: str, path: Path):
    executable = shutil.which("espeak-ng")
    if not executable:
        raise FunError("Липсва espeak-ng. Инсталирай: sudo apt install espeak-ng ffmpeg")
    process = await asyncio.create_subprocess_exec(
        executable, "-v", language, "-s", "155", "-a", "85", "-w", str(path), "--stdin",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(text.encode("utf-8")), timeout=15)
        if process.returncode != 0 or not path.exists() or path.stat().st_size <= 44:
            log.error("Speech synthesis failed: %s", stderr.decode(errors="replace")[:500])
            raise FunError("Не успях да създам гласа. Провери espeak-ng на сървъра.")
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()


class ClubFun:
    def __init__(self, bot, tree, settings):
        self.bot, self.tree, self.settings = bot, tree, settings
        self.cooldowns = Cooldowns()
        self.jobs: dict[int, asyncio.Task] = {}
        self.busy: set[int] = set()

    def check_context(self, interaction, *, restrict_channel=True):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            raise FunError("Тази команда работи само в сървъра.")
        if (restrict_channel and self.settings.text_channels
                and interaction.channel_id not in self.settings.text_channels):
            raise FunError("Забавните команди не са разрешени в този текстов канал.")

    async def error(self, interaction, message):
        kwargs = dict(ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        if interaction.response.is_done():
            await interaction.followup.send(message, **kwargs)
        else:
            await interaction.response.send_message(message, **kwargs)

    async def roast(self, interaction, member=None, everyone=False):
        try:
            self.check_context(interaction)
            prefix, mentions = mention_policy(self.settings, interaction, member, everyone)
            # A server-wide limit prevents alternating users from bypassing it.
            self.cooldowns.take((interaction.guild_id, "everyone" if everyone else "roast"),
                                300 if everyone else 15)
            await interaction.response.send_message(
                f"{prefix} {random.choice(ROASTS)} 😂", allowed_mentions=mentions)
        except FunError as exc:
            await self.error(interaction, str(exc))

    async def speak(self, interaction, scammer=False):
        try:
            self.check_context(interaction)
            state = interaction.user.voice
            if state is None or not isinstance(state.channel, discord.VoiceChannel):
                raise FunError("Първо влез в обикновен гласов канал (не Stage).")
            channel = state.channel
            if self.settings.voice_channels and channel.id not in self.settings.voice_channels:
                raise FunError("Гласовите шеги не са разрешени в този канал.")
            permissions = channel.permissions_for(interaction.guild.me)
            if not (permissions.view_channel and permissions.connect and permissions.speak):
                raise FunError("Ботът се нуждае от View Channel, Connect и Speak в твоя канал.")
            if interaction.guild_id in self.busy or interaction.guild.voice_client is not None:
                raise FunError("Вече говоря или съм свързан. Използвай /stop, ако трябва да спра.")
            if not shutil.which("ffmpeg") or not shutil.which("espeak-ng"):
                raise FunError("Липсва гласов пакет. Инсталирай: sudo apt install espeak-ng ffmpeg")
            self.cooldowns.take((interaction.guild_id, "voice"), 30)
        except FunError as exc:
            await self.error(interaction, str(exc))
            return

        self.busy.add(interaction.guild_id)
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            # Recheck after acknowledging: the requester may have left while we awaited Discord.
            if not interaction.user.voice or interaction.user.voice.channel != channel:
                raise FunError("Вече не си в същия гласов канал. Опитай отново.")
            job = asyncio.create_task(self.play(interaction, channel, scammer))
            self.jobs[interaction.guild_id] = job
        except BaseException as exc:
            self.busy.discard(interaction.guild_id)
            if isinstance(exc, FunError):
                await self.error(interaction, str(exc))
            else:
                raise

    async def play(self, interaction, channel, scammer):
        voice = source = None
        try:
            text = random.choice(SCAMMER_LINES if scammer else ROASTS)
            with tempfile.TemporaryDirectory(prefix="itclub-voice-") as folder:
                path = Path(folder) / "speech.wav"
                await synthesize(text, "en" if scammer else "bg", path)
                if not interaction.user.voice or interaction.user.voice.channel != channel:
                    raise FunError("Излезе от канала, затова отмених репликата.")
                voice = await channel.connect(timeout=15, reconnect=False, self_deaf=True)
                # Encode with FFmpeg, with telephone EQ for the fictional caller.
                filters = "highpass=f=350,lowpass=f=3000,volume=0.65" if scammer else "volume=0.65"
                source = discord.FFmpegOpusAudio(str(path), options=f"-vn -t 35 -af {filters}")
                loop = asyncio.get_running_loop()
                finished = loop.create_future()

                def complete(error):
                    if not finished.done():
                        if error:
                            finished.set_exception(error)
                        else:
                            finished.set_result(None)

                voice.play(source, after=lambda error: loop.call_soon_threadsafe(complete, error))
                await interaction.followup.send(
                    "☎️ Пускам пародийното обаждане." if scammer else "🔊 Пускам репликата.",
                    ephemeral=True)
                await asyncio.wait_for(finished, timeout=45)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Club voice playback failed")
            message = str(exc) if isinstance(exc, FunError) else (
                "Не успях да пусна звука. Провери voice зависимостите, правата и лога на сървъра.")
            with suppress(discord.HTTPException):
                await self.error(interaction, message)
        finally:
            # A partially failed connect may still have registered a voice client.
            voice = voice or interaction.guild.voice_client
            try:
                if voice:
                    voice.stop()
                    await voice.disconnect(force=True)
            except Exception:
                log.exception("Could not disconnect club voice client")
            finally:
                if source:
                    source.cleanup()
                self.jobs.pop(interaction.guild_id, None)
                self.busy.discard(interaction.guild_id)

    async def stop(self, interaction):
        try:
            self.check_context(interaction, restrict_channel=False)
            voice = interaction.guild.voice_client
            job = self.jobs.get(interaction.guild_id)
            if voice is None and job is None:
                raise FunError("В момента не говоря.")
            # Any server member can stop a joke. No cooldown on the stop button.
            await interaction.response.defer(ephemeral=True, thinking=True)
            if job:
                job.cancel()
                with suppress(asyncio.CancelledError):
                    await job
                self.jobs.pop(interaction.guild_id, None)
                self.busy.discard(interaction.guild_id)
            elif voice:
                await voice.disconnect(force=True)
            await interaction.followup.send("⏹️ Спрях и излязох от гласовия канал.", ephemeral=True)
        except FunError as exc:
            await self.error(interaction, str(exc))

    async def close(self):
        jobs = list(self.jobs.values())
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        self.jobs.clear()
        self.busy.clear()


def install(bot, tree, settings=None):
    """Register before the notifier's on_ready copies/syncs its command tree."""
    fun = ClubFun(bot, tree, settings or Settings.from_env())

    @tree.command(name="psuvai", description="Шеговита псувня към теб, избран човек или всички")
    @app_commands.guild_only()
    @app_commands.describe(member="Кого да тагна? Без избор — теб.",
                           everyone="Всички: само администратор, ако е разрешено в настройките")
    async def psuvai(interaction: discord.Interaction, member: discord.Member | None = None,
                     everyone: bool = False):
        await fun.roast(interaction, member, everyone)

    @tree.command(name="voice_psuvai", description="Влизам при теб, казвам шеговита псувня и излизам")
    @app_commands.guild_only()
    async def voice_psuvai(interaction: discord.Interaction):
        await fun.speak(interaction)

    @tree.command(name="scammer", description="Пародийно обаждане от измислен техник-хакер във voice")
    @app_commands.guild_only()
    async def scammer(interaction: discord.Interaction):
        await fun.speak(interaction, scammer=True)

    @tree.command(name="stop", description="Спира гласовата шега и изкарва бота от канала")
    @app_commands.guild_only()
    async def stop(interaction: discord.Interaction):
        await fun.stop(interaction)

    return fun
