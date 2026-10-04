"""Authenticated web control panel for the music add-on."""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from club_music import MusicError


class GuildInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    guild_id: str

    @field_validator("guild_id")
    @classmethod
    def valid_guild_id(cls, value: str):
        if not value.isascii() or not value.isdecimal() or not 0 < int(value) < 2**64:
            raise ValueError("Невалидно Discord server ID.")
        return value


class PlayInput(GuildInput):
    voice_channel_id: str
    query: str = Field(min_length=1, max_length=500)

    @field_validator("voice_channel_id")
    @classmethod
    def valid_channel_id(cls, value: str):
        if not value.isascii() or not value.isdecimal() or not 0 < int(value) < 2**64:
            raise ValueError("Невалидно ID на гласов канал.")
        return value

    @field_validator("query")
    @classmethod
    def clean_query(cls, value: str):
        value = value.strip()
        if not value:
            raise ValueError("Въведи песен или линк.")
        return value


class VolumeInput(GuildInput):
    percent: int = Field(ge=0, le=100)


class RemoveInput(GuildInput):
    index: int = Field(ge=1, le=1000)


def install_music_admin(main, music):
    async def require_session(request: Request):
        if not main.request_has_valid_session(request):
            raise HTTPException(401, "Сесията е изтекла. Влез отново в панела.")
        if request.method != "GET" and request.headers.get("X-Music-Panel") != "1":
            raise HTTPException(403, "Невалидна заявка от музикалния панел.")

    router = APIRouter(prefix="/music/api", dependencies=[Depends(require_session)])

    def response(request, data, status_code=200):
        result = main.admin_json(request, data, status_code)
        result.headers["Cache-Control"] = "no-store"
        return result

    def parse_guild_id(value: str) -> int:
        if not value.isascii() or not value.isdecimal() or not 0 < int(value) < 2**64:
            raise HTTPException(400, "Невалидно Discord server ID.")
        return int(value)

    async def action(request: Request, call, success: str):
        try:
            result = await call()
        except MusicError as exc:
            raise HTTPException(409, str(exc)) from exc
        return response(request, {"message": success, "result": result})

    @router.get("/bootstrap")
    async def bootstrap(request: Request):
        await main.bot.wait_until_ready()
        return response(request, {"guilds": music.admin_guilds()})

    @router.get("/status")
    async def status(request: Request, guild_id: str = Query(..., max_length=24)):
        await main.bot.wait_until_ready()
        try:
            data = await music.admin_snapshot(parse_guild_id(guild_id))
        except MusicError as exc:
            raise HTTPException(404, str(exc)) from exc
        return response(request, data)

    @router.post("/play")
    async def play(request: Request, values: PlayInput):
        await main.bot.wait_until_ready()
        return await action(
            request,
            lambda: music.admin_play(
                int(values.guild_id),
                int(values.voice_channel_id),
                values.query,
            ),
            "Песента е добавена.",
        )

    @router.post("/pause")
    async def pause(request: Request, values: GuildInput):
        return await action(
            request,
            lambda: music.admin_pause(int(values.guild_id)),
            "Музиката е паузирана.",
        )

    @router.post("/resume")
    async def resume(request: Request, values: GuildInput):
        return await action(
            request,
            lambda: music.admin_resume(int(values.guild_id)),
            "Музиката продължава.",
        )

    @router.post("/skip")
    async def skip(request: Request, values: GuildInput):
        return await action(
            request,
            lambda: music.admin_skip(int(values.guild_id)),
            "Песента е прескочена.",
        )

    @router.post("/clear")
    async def clear(request: Request, values: GuildInput):
        return await action(
            request,
            lambda: music.admin_clear_queue(int(values.guild_id)),
            "Опашката е изчистена.",
        )

    @router.post("/remove")
    async def remove(request: Request, values: RemoveInput):
        return await action(
            request,
            lambda: music.admin_remove(int(values.guild_id), values.index),
            "Песента е махната от опашката.",
        )

    @router.post("/volume")
    async def volume(request: Request, values: VolumeInput):
        return await action(
            request,
            lambda: music.admin_set_volume(int(values.guild_id), values.percent),
            f"Силата е зададена на {values.percent}%.",
        )

    @router.post("/leave")
    async def leave(request: Request, values: GuildInput):
        return await action(
            request,
            lambda: music.admin_leave(int(values.guild_id)),
            "Музиката е спряна и ботът напусна voice канала.",
        )

    main.app.include_router(router)

    @main.app.get("/music", include_in_schema=False)
    async def canonical_music_panel():
        return RedirectResponse("music/", status_code=307)

    @main.app.get("/music/", response_class=HTMLResponse)
    async def music_panel(request: Request):
        if not main.request_has_valid_session(request):
            return RedirectResponse("../", status_code=303, headers={"Cache-Control": "no-store"})
        html = Path(__file__).with_name("music_admin.html").read_text(encoding="utf-8")
        return main.set_session_cookie(HTMLResponse(html, headers={"Cache-Control": "no-store"}))
