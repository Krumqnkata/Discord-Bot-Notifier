from dataclasses import asdict
import importlib
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

from fastapi.testclient import TestClient
from fastapi import FastAPI
import pytest

from club_fun import ClubFun, FunError, Settings
from club_store import ClubStore
from test_club_fun import interaction, voice_fixture


def defaults():
    data = asdict(Settings())
    data["text_channels"] = []
    data["voice_channels"] = []
    return data


@pytest.fixture
def store(tmp_path):
    return ClubStore(tmp_path / "club.db", defaults(), ["Original phrase"], ["Original call"])


def test_restart_preserves_edits_settings_and_deleted_phrases(store):
    for phrase in store.phrases():
        store.delete_phrase(phrase["id"])
    store.save_settings({"allow_everyone": True})
    reopened = ClubStore(store.path, defaults(), ["Should not return"], ["Nor this"])
    assert reopened.phrases() == []
    assert reopened.settings()["allow_everyone"] is True


def test_disabled_pool_does_not_fall_back_to_hardcoded_lines(store):
    phrase = next(p for p in store.phrases() if p["kind"] == "roast")
    store.save_phrase({**phrase, "enabled": False}, phrase["id"])
    service = ClubFun(None, None, Settings(), store)
    with pytest.raises(FunError):
        service.phrase()


@pytest.mark.asyncio
async def test_live_edit_and_history_snapshot_survive_later_delete(store):
    i = interaction()
    i.user.display_name = "Test user"
    i.channel = SimpleNamespace(name="club")
    i.user.voice = None
    phrase = next(p for p in store.phrases() if p["kind"] == "roast")
    store.save_phrase({**phrase, "text": "Edited phrase"}, phrase["id"])
    service = ClubFun(None, None, Settings(), store)
    await service.roast(i)
    assert "Edited phrase" in i.response.send_message.call_args.args[0]
    store.delete_phrase(phrase["id"])
    row = store.history()["items"][0]
    assert row["text"] == "Edited phrase" and row["status"] == "success"
    assert row["actor_name"] == "Test user" and row["target_id"] == "123"
    assert row["channel_name"] == "club"
    store.save_settings({"enabled": False})
    await service.roast(i)
    assert store.history()["items"][0]["status"] == "blocked"


def test_history_filter_pagination_and_restart_recovery(store):
    i = interaction()
    i.user.voice = None
    i.user.display_name = "Alice"
    for index in range(53):
        event = store.begin(i, "psuvai")
        store.record(event, "success", text=f"line {index}")
    assert len(store.history()["items"]) == 50
    assert len(store.history(page=2)["items"]) == 3
    assert store.history(command="scammer")["total"] == 0
    assert store.history(query="line 52")["total"] == 1
    assert store.history(query="Alice")["total"] == 53
    pending = store.begin(i, "scammer")
    reopened = ClubStore(store.path, defaults(), [], [])
    row = reopened.history()["items"][0]
    assert row["id"] == pending and row["status"] == "interrupted"


@pytest.mark.asyncio
async def test_stopped_voice_and_stop_command_are_both_recorded(store, monkeypatch):
    i, channel, voice, source = voice_fixture(monkeypatch)
    i.user.display_name = "Caller"
    service = ClubFun(None, None, Settings(), store)
    await service.speak(i)
    await service.stop(i)
    rows = store.history()["items"]
    assert {(r["command"], r["status"]) for r in rows} == {
        ("stop", "success"), ("voice_psuvai", "stopped")}
    assert rows[1]["text"] == "Original phrase"


@pytest.fixture
def panel(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    for name in ("main.py", "run_club.py"):
        shutil.copy(root / name, tmp_path / name)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("DISCORD_TOKEN", "offline-test-token")
    monkeypatch.setenv("CHANNEL_ID", "123")
    monkeypatch.setenv("PANEL_PASSWORD", "offline-test-password")
    monkeypatch.setenv("PANEL_COOKIE_SECURE", "1")
    runner = importlib.import_module("run_club")
    client = TestClient(runner.app, base_url="https://testserver")
    # Do not enter the client's context: that would start the real Discord lifespan.
    try:
        yield client, runner
    finally:
        client.close()
        sys.modules.pop("run_club", None)
        sys.modules.pop("main", None)


def login(client):
    assert client.post("/login", data={"password": "offline-test-password"}).status_code == 200
    client.headers["X-Club-Panel"] = "1"


def test_routes_require_existing_panel_session_and_custom_header(panel):
    client, runner = panel
    payload = {"kind": "roast", "text": "test", "language": "bg", "enabled": True}
    assert client.get("/fun/api/history").status_code == 401
    assert client.get("/fun/api/bootstrap").status_code == 401
    assert client.post("/fun/api/phrases", json=payload).status_code == 401
    assert client.get("/fun/", follow_redirects=False).headers["location"] == "../"
    login(client)
    page = client.get("/fun/")
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    assert 'href="fun/"' in client.get("/").text
    del client.headers["X-Club-Panel"]
    assert client.post("/fun/api/phrases", json=payload).status_code == 403
    assert client.put("/fun/api/settings", json={}).status_code == 403
    assert client.delete("/fun/api/phrases/1").status_code == 403
    client.post("/logout")
    assert client.get("/fun/api/history").status_code == 401


def test_api_crud_validation_and_snowflake_precision(panel):
    client, runner = panel
    login(client)
    payload = {"kind": "roast", "text": "<script>alert(1)</script>", "language": "bg", "enabled": True}
    created = client.post("/fun/api/phrases", json=payload)
    assert created.status_code == 201
    phrase_id = created.json()["id"]
    assert client.put(f"/fun/api/phrases/{phrase_id}", json={**payload, "text": "Updated"}).status_code == 200
    assert client.post("/fun/api/phrases", json={**payload, "text": "   "}).status_code == 422
    assert client.post("/fun/api/phrases", json={**payload, "text": "x" * 1001}).status_code == 422
    assert client.post("/fun/api/phrases", json={**payload, "language": "arbitrary"}).status_code == 422
    settings = client.get("/fun/api/bootstrap").json()["settings"]
    settings["text_channels"] = ["123456789012345678"]
    settings["allow_everyone"] = True
    assert client.put("/fun/api/settings", json=settings).status_code == 200
    assert client.get("/fun/api/bootstrap").json()["settings"]["text_channels"] == ["123456789012345678"]
    assert runner.fun.settings.text_channels == [123456789012345678]
    assert runner.fun.settings.allow_everyone
    assert client.put("/fun/api/settings", json={**settings, "text_cooldown": 0}).status_code == 422
    assert client.put("/fun/api/settings", json={**settings, "text_channels": ["oops"]}).status_code == 422
    assert client.delete(f"/fun/api/phrases/{phrase_id}").status_code == 200
    assert client.delete(f"/fun/api/phrases/{phrase_id}").status_code == 404
    assert client.get("/fun/api/history?page=0").status_code == 422
    assert client.get("/fun", follow_redirects=False).headers["location"] == "fun/"


def test_prefixed_panel_links_and_api(panel):
    _, runner = panel
    host = FastAPI()
    host.mount("/itc-notif", runner.app)
    client = TestClient(host, base_url="https://testserver")
    try:
        assert client.post("/itc-notif/login", data={"password": "offline-test-password"}).status_code == 200
        page = client.get("/itc-notif/fun")
        assert str(page.url).endswith("/itc-notif/fun/")
        assert page.status_code == 200
        assert client.get("/itc-notif/fun/api/bootstrap").status_code == 200
        assert 'href="fun/"' in client.get("/itc-notif/").text
    finally:
        client.close()
