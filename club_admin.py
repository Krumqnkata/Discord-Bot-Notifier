"""Authenticated administration routes for the optional club module."""
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator


class PhraseInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["roast", "scammer"]
    text: str = Field(min_length=1, max_length=1000)
    language: Literal["bg", "en"]
    enabled: bool = True

    @field_validator("text")
    @classmethod
    def clean_text(cls, text):
        text = text.strip()
        if not text or any(ord(char) < 32 and char not in "\n\t" for char in text):
            raise ValueError("Въведи непразна реплика без контролни символи.")
        return text


class SettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool
    voice_enabled: bool
    allow_everyone: bool
    text_channels: list[str] = Field(max_length=100)
    voice_channels: list[str] = Field(max_length=100)
    text_cooldown: int = Field(ge=5, le=3600)
    voice_cooldown: int = Field(ge=10, le=3600)
    everyone_cooldown: int = Field(ge=60, le=86400)

    @field_validator("text_channels", "voice_channels")
    @classmethod
    def ids(cls, values):
        if any(not value.isascii() or not value.isdecimal()
               or not 0 < int(value) < 2**64 for value in values):
            raise ValueError("Невалидно ID на канал.")
        return list(dict.fromkeys(values))


def install_admin(main, store):
    async def require_session(request: Request):
        if not main.request_has_valid_session(request):
            raise HTTPException(401, "Сесията е изтекла. Влез отново в панела.")
        # Custom header + JSON forbids cross-origin HTML form submissions.
        # No CORS is enabled on these routes.
        if request.method != "GET" and request.headers.get("X-Club-Panel") != "1":
            raise HTTPException(403, "Невалидна заявка от панела.")

    router = APIRouter(prefix="/fun/api", dependencies=[Depends(require_session)])

    def response(request, data, status_code=200):
        result = main.admin_json(request, data, status_code)
        result.headers["Cache-Control"] = "no-store"
        return result

    def public_settings():
        values = store.settings()
        # Snowflakes must stay strings in JavaScript, avoiding precision loss.
        for key in ("text_channels", "voice_channels"):
            values[key] = [str(value) for value in values[key]]
        return values

    @router.get("/bootstrap")
    async def bootstrap(request: Request):
        return response(request, {"phrases": store.phrases(), "settings": public_settings()})

    @router.post("/phrases")
    async def create_phrase(request: Request, phrase: PhraseInput):
        phrase_id = store.save_phrase(phrase.model_dump())
        return response(request, {"id": phrase_id, "message": "Репликата е добавена."}, 201)

    @router.put("/phrases/{phrase_id}")
    async def edit_phrase(request: Request, phrase_id: int, phrase: PhraseInput):
        if store.save_phrase(phrase.model_dump(), phrase_id) is None:
            raise HTTPException(404, "Тази реплика вече не съществува.")
        return response(request, {"message": "Репликата е запазена."})

    @router.delete("/phrases/{phrase_id}")
    async def delete_phrase(request: Request, phrase_id: int):
        if not store.delete_phrase(phrase_id):
            raise HTTPException(404, "Тази реплика вече не съществува.")
        return response(request, {"message": "Репликата е изтрита. Историята е запазена."})

    @router.put("/settings")
    async def settings(request: Request, values: SettingsInput):
        data = values.model_dump()
        for key in ("text_channels", "voice_channels"):
            data[key] = [int(value) for value in data[key]]
        store.save_settings(data)
        return response(request, {"message": "Настройките са запазени. Важат за следващите команди."})

    @router.get("/history")
    async def history(request: Request, command: str = Query("", max_length=30),
                      status: str = Query("", max_length=30), q: str = Query("", max_length=200),
                      page: int = Query(1, ge=1, le=1000000)):
        return response(request, store.history(command, status, q, page))

    main.app.include_router(router)

    @main.app.get("/fun", include_in_schema=False)
    async def canonical_panel():
        # Relative Location works behind /itc-notif/ without trusting forwarded headers.
        return RedirectResponse("fun/", status_code=307)

    @main.app.get("/fun/", response_class=HTMLResponse)
    async def panel(request: Request):
        if not main.request_has_valid_session(request):
            return RedirectResponse("../", status_code=303, headers={"Cache-Control": "no-store"})
        html = Path(__file__).with_name("club_admin.html").read_text(encoding="utf-8")
        return main.set_session_cookie(HTMLResponse(html, headers={"Cache-Control": "no-store"}))

    # Add navigation at runtime: the original main.py on disk stays byte-for-byte intact.
    marker = '<div class="w-full max-w-6xl mx-auto space-y-6">'
    if marker not in main.HTML_TEMPLATE:
        raise RuntimeError("Не намирам мястото за навигацията в шаблона на основния панел.")
    main.HTML_TEMPLATE = main.HTML_TEMPLATE.replace(marker, marker + '''
      <nav class="glass rounded-xl px-4 py-3 flex gap-4" aria-label="Раздели">
        <span class="text-slate-300">📅 Сбирки</span>
        <a href="fun/" class="text-indigo-300 hover:text-white">🎙 Забавни команди</a>
        <a href="music/" class="text-indigo-300 hover:text-white">🎵 Музика →</a>
      </nav>''', 1)
