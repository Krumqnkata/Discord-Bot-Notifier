"""Persistent club configuration, editable phrases and command history."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import random
import sqlite3


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ClubStore:
    def __init__(self, path, settings, roasts, scammer_lines):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS club_config (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS club_phrases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL CHECK(kind IN ('roast', 'scammer')),
                    text TEXT NOT NULL,
                    language TEXT NOT NULL CHECK(language IN ('bg', 'en')),
                    enabled INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS club_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    command TEXT NOT NULL, guild_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL, actor_name TEXT NOT NULL,
                    target_id TEXT NOT NULL, target_name TEXT NOT NULL,
                    channel_id TEXT NOT NULL, channel_name TEXT NOT NULL,
                    voice_channel_id TEXT NOT NULL, voice_channel_name TEXT NOT NULL,
                    text TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS club_history_command ON club_history(command, id);
                CREATE INDEX IF NOT EXISTS club_history_status ON club_history(status, id);
            """)
            for key, value in settings.items():
                db.execute("INSERT OR IGNORE INTO club_config VALUES (?, ?)",
                           (key, json.dumps(value)))
            # A marker prevents deliberately deleted phrases reappearing on restart.
            if not db.execute("SELECT 1 FROM club_config WHERE key='seeded'").fetchone():
                for kind, language, lines in (("roast", "bg", roasts), ("scammer", "en", scammer_lines)):
                    db.executemany(
                        "INSERT INTO club_phrases(kind,text,language,updated_at) VALUES (?,?,?,?)",
                        [(kind, text, language, utc_now()) for text in lines])
                db.execute("INSERT INTO club_config VALUES ('seeded', 'true')")
            db.execute("UPDATE club_history SET status='interrupted', detail=?, updated_at=? "
                       "WHERE status='running'", ("Процесът е прекъснат или рестартиран.", utc_now()))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def settings(self):
        with self.connect() as db:
            return {row["key"]: json.loads(row["value"]) for row in db.execute(
                "SELECT * FROM club_config WHERE key != 'seeded'")}

    def save_settings(self, values):
        with self.connect() as db:
            db.executemany("INSERT OR REPLACE INTO club_config VALUES (?,?)",
                           [(key, json.dumps(value)) for key, value in values.items()])

    def phrases(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM club_phrases ORDER BY kind,id DESC")]

    def choose_phrase(self, kind):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM club_phrases WHERE kind=? AND enabled=1", (kind,)).fetchall()
        return dict(random.choice(rows)) if rows else None

    def save_phrase(self, values, phrase_id=None):
        with self.connect() as db:
            args = (values["kind"], values["text"], values["language"], int(values["enabled"]), utc_now())
            if phrase_id is None:
                return db.execute("INSERT INTO club_phrases(kind,text,language,enabled,updated_at) "
                                  "VALUES (?,?,?,?,?)", args).lastrowid
            result = db.execute("UPDATE club_phrases SET kind=?,text=?,language=?,enabled=?,updated_at=? "
                                "WHERE id=?", (*args, phrase_id))
            return phrase_id if result.rowcount else None

    def delete_phrase(self, phrase_id):
        with self.connect() as db:
            return bool(db.execute("DELETE FROM club_phrases WHERE id=?", (phrase_id,)).rowcount)

    def begin(self, interaction, command, member=None, everyone=False):
        actor = interaction.user
        target = member or actor
        channel = getattr(interaction, "channel", None)
        voice = getattr(getattr(actor, "voice", None), "channel", None)
        now = utc_now()
        with self.connect() as db:
            return db.execute("""INSERT INTO club_history(
                created_at,updated_at,command,guild_id,actor_id,actor_name,target_id,target_name,
                channel_id,channel_name,voice_channel_id,voice_channel_name,status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    now, now, command, str(interaction.guild_id or ""), str(actor.id),
                    str(getattr(actor, "display_name", actor))[:200],
                    "everyone" if everyone else str(target.id) if command == "psuvai" else "",
                    "@everyone" if everyone else str(getattr(target, "display_name", target))[:200]
                    if command == "psuvai" else "",
                    str(interaction.channel_id or ""), str(getattr(channel, "name", ""))[:200],
                    str(getattr(voice, "id", "")), str(getattr(voice, "name", ""))[:200], "running",
                )).lastrowid

    def record(self, event_id, status, detail="", text=None):
        with self.connect() as db:
            if text is None:
                db.execute("UPDATE club_history SET status=?,detail=?,updated_at=? WHERE id=?",
                           (status, detail[:1000], utc_now(), event_id))
            else:
                db.execute("UPDATE club_history SET status=?,detail=?,text=?,updated_at=? WHERE id=?",
                           (status, detail[:1000], text, utc_now(), event_id))

    def history(self, command="", status="", query="", page=1):
        clauses, args = [], []
        for column, value in (("command", command), ("status", status)):
            if value:
                clauses.append(f"{column}=?")
                args.append(value)
        if query:
            clauses.append("(actor_name LIKE ? OR actor_id LIKE ? OR target_name LIKE ? OR text LIKE ?)")
            args.extend([f"%{query}%"] * 4)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as db:
            total = db.execute("SELECT COUNT(*) FROM club_history" + where, args).fetchone()[0]
            rows = db.execute("SELECT * FROM club_history" + where + " ORDER BY id DESC LIMIT 50 OFFSET ?",
                              (*args, (page - 1) * 50)).fetchall()
        return {"items": [dict(row) for row in rows], "total": total, "page": page, "page_size": 50}
