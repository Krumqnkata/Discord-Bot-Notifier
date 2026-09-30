import asyncio
import importlib
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from discord import app_commands

import club_fun as fun


def interaction():
    user = Mock(spec=discord.Member)
    user.id = 123
    user.mention = "<@123>"
    user.guild_permissions = discord.Permissions(administrator=False)
    result = SimpleNamespace(
        user=user, guild_id=10, channel_id=20,
        guild=SimpleNamespace(voice_client=None, me=Mock()),
        app_permissions=discord.Permissions(mention_everyone=False),
        response=SimpleNamespace(is_done=lambda: False, send_message=AsyncMock(), defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    async def defer(**kwargs):
        result.response.is_done = lambda: True
    result.response.defer.side_effect = defer
    return result


def test_single_mention_cannot_ping_everyone_or_roles():
    i = interaction()
    prefix, policy = fun.mention_policy(fun.Settings(), i, None, False)
    assert prefix == "<@123>"
    assert policy.to_dict() == {"users": [123], "parse": []}


@pytest.mark.parametrize("enabled,admin,bot_permission", [
    (False, True, True), (True, False, True), (True, True, False),
])
def test_everyone_requires_all_three_gates(enabled, admin, bot_permission):
    i = interaction()
    i.user.guild_permissions.administrator = admin
    i.app_permissions.mention_everyone = bot_permission
    with pytest.raises(fun.FunError):
        fun.mention_policy(fun.Settings(allow_everyone=enabled), i, None, True)


def test_everyone_can_be_enabled_explicitly():
    i = interaction()
    i.user.guild_permissions.administrator = True
    i.app_permissions.mention_everyone = True
    prefix, policy = fun.mention_policy(fun.Settings(allow_everyone=True), i, None, True)
    assert prefix == "@everyone"
    assert policy.to_dict() == {"parse": ["everyone"]}
    with pytest.raises(fun.FunError):
        fun.mention_policy(fun.Settings(allow_everyone=True), i, i.user, True)


def test_cooldown_expires_and_is_scoped_to_guild(monkeypatch):
    monkeypatch.setattr(fun.time, "monotonic", lambda: 100)
    cooldown = fun.Cooldowns()
    cooldown.take((10, "voice"), 30)
    with pytest.raises(fun.FunError):
        cooldown.take((10, "voice"), 30)
    cooldown.take((11, "voice"), 30)
    monkeypatch.setattr(fun.time, "monotonic", lambda: 131)
    cooldown.take((10, "voice"), 30)
    assert (11, "voice") not in cooldown.deadlines


def test_channel_configuration_fails_closed(monkeypatch):
    monkeypatch.setenv("FUN_TEXT_CHANNEL_IDS", "12, 34")
    assert fun.Settings.from_env().text_channels == frozenset({12, 34})
    monkeypatch.setenv("FUN_TEXT_CHANNEL_IDS", "12,not-an-id")
    with pytest.raises(RuntimeError):
        fun.Settings.from_env()


def test_existing_command_is_preserved():
    bot = discord.Client(intents=discord.Intents.default())
    tree = app_commands.CommandTree(bot)

    @tree.command(name="сбирки")
    async def meetings(i: discord.Interaction):
        pass

    fun.install(bot, tree, fun.Settings())
    assert {c.name for c in tree.get_commands()} == {
        "сбирки", "psuvai", "voice_psuvai", "scammer", "stop"}
    assert tree.get_command("сбирки").callback is meetings.callback


@pytest.mark.asyncio
async def test_roast_blocks_wrong_channel_and_cooldown():
    i = interaction()
    service = fun.ClubFun(None, None, fun.Settings(text_channels=frozenset({99})))
    await service.roast(i)
    assert i.response.send_message.call_args.kwargs["ephemeral"]
    service.settings = fun.Settings()
    await service.roast(i)
    assert "ephemeral" not in i.response.send_message.call_args.kwargs
    await service.roast(i)
    assert i.response.send_message.call_args.kwargs["ephemeral"]


def voice_fixture(monkeypatch):
    i = interaction()
    channel = Mock(spec=discord.VoiceChannel)
    channel.id = 30
    channel.permissions_for.return_value = discord.Permissions(
        view_channel=True, connect=True, speak=True)
    i.user.voice = SimpleNamespace(channel=channel)
    voice = Mock()
    voice.disconnect = AsyncMock()
    channel.connect = AsyncMock(return_value=voice)
    source = Mock()
    monkeypatch.setattr(fun, "synthesize", AsyncMock())
    monkeypatch.setattr(fun.discord, "FFmpegOpusAudio", Mock(return_value=source))
    monkeypatch.setattr(fun.shutil, "which", lambda _: "/mock/executable")
    return i, channel, voice, source


@pytest.mark.asyncio
async def test_playback_finishes_disconnects_and_releases_busy(monkeypatch):
    i, channel, voice, source = voice_fixture(monkeypatch)
    voice.play.side_effect = lambda source, after: after(None)
    service = fun.ClubFun(None, None, fun.Settings())
    await service.speak(i)
    await service.jobs[i.guild_id]
    voice.disconnect.assert_awaited_once_with(force=True)
    source.cleanup.assert_called_once()
    assert not service.busy and not service.jobs


@pytest.mark.asyncio
async def test_stop_cancels_active_audio_and_disconnects(monkeypatch):
    i, channel, voice, source = voice_fixture(monkeypatch)
    started = asyncio.Event()
    voice.play.side_effect = lambda source, after: started.set()
    service = fun.ClubFun(None, None, fun.Settings())
    await service.speak(i)
    await asyncio.wait_for(started.wait(), 2)
    await service.stop(i)
    voice.stop.assert_called_once()
    voice.disconnect.assert_awaited_once()
    assert not service.jobs and not service.busy


@pytest.mark.asyncio
async def test_failed_connect_cleans_partial_voice_client(monkeypatch):
    i, channel, voice, source = voice_fixture(monkeypatch)

    async def fail(**kwargs):
        i.guild.voice_client = voice
        raise TimeoutError

    channel.connect.side_effect = fail
    service = fun.ClubFun(None, None, fun.Settings())
    await service.speak(i)
    await service.jobs[i.guild_id]
    voice.disconnect.assert_awaited_once()
    assert not service.busy
    assert i.followup.send.call_args.kwargs["ephemeral"]


@pytest.mark.asyncio
async def test_duplicate_voice_request_does_not_start_another_job(monkeypatch):
    i, channel, voice, source = voice_fixture(monkeypatch)
    service = fun.ClubFun(None, None, fun.Settings())
    service.busy.add(i.guild_id)
    await service.speak(i)
    channel.connect.assert_not_awaited()
    assert i.response.send_message.call_args.kwargs["ephemeral"]


@pytest.mark.asyncio
async def test_shutdown_before_job_starts_does_not_leave_busy(monkeypatch):
    i, channel, voice, source = voice_fixture(monkeypatch)
    service = fun.ClubFun(None, None, fun.Settings())
    await service.speak(i)
    await service.close()
    assert not service.jobs and not service.busy


@pytest.mark.asyncio
async def test_synthesis_failure_releases_busy(monkeypatch):
    i, channel, voice, source = voice_fixture(monkeypatch)
    monkeypatch.setattr(fun, "synthesize", AsyncMock(side_effect=fun.FunError("test failure")))
    service = fun.ClubFun(None, None, fun.Settings())
    await service.speak(i)
    await service.jobs[i.guild_id]
    channel.connect.assert_not_awaited()
    assert not service.jobs and not service.busy


def test_runner_uses_same_app_client_and_original_file(tmp_path, monkeypatch):
    # main initializes SQLite on import: isolate that side effect from real data.
    root = Path(__file__).resolve().parents[1]
    for name in ("main.py", "run_club.py"):
        shutil.copy(root / name, tmp_path / name)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("DISCORD_TOKEN", "offline-test-token")
    monkeypatch.setenv("CHANNEL_ID", "123")
    monkeypatch.setenv("PANEL_PASSWORD", "offline-test-password")
    try:
        runner = importlib.import_module("run_club")
        assert runner.app is runner.main.app
        assert runner.fun.bot is runner.main.bot
        assert len(runner.main.tree.get_commands()) == 5
        assert runner.main.bot.intents.voice_states
        assert "/health" in {getattr(r, "path", None) for r in runner.app.routes}
        assert 'href="fun/"' in runner.main.HTML_TEMPLATE
        assert (root / "main.py").read_bytes() == (tmp_path / "main.py").read_bytes()
    finally:
        sys.modules.pop("run_club", None)
        sys.modules.pop("main", None)
