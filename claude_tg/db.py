"""Хранилище бота (SQLite): темы ↔ сессии, служебные темы, буфер контекста, флаги.

Настройки сессий (модель, effort, режим) здесь НЕ хранятся — они живут в демоне
(один факт в одном месте, ТЗ §6). У темы без сессии есть только «отложенные»
настройки: демон получит их вместе с первым ходом.
"""
import json
import sqlite3
import time
from pathlib import Path

# Виды тем: dialog — обычная тема-сессия; tgchat — сессия для чужих чатов и лички;
# general / reminders / journal — служебные, в сессию не идут
SERVICE_KINDS = ("general", "reminders", "journal")


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS topics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                thread_id INTEGER,                 -- NULL = General
                kind TEXT NOT NULL DEFAULT 'dialog',
                title TEXT,
                session_id TEXT,                   -- реальный id или 'new-…' до первого init
                pending_settings TEXT,             -- JSON {model, effort, permissionMode} до первого хода
                header_msg_id INTEGER,             -- закреплённая шапка темы
                state TEXT NOT NULL DEFAULT 'open',-- open | closed | archived
                created_at INTEGER NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_topics_place ON topics(chat_id, IFNULL(thread_id, 0));
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS buffer (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at INTEGER NOT NULL,
                text TEXT,                         -- текст пересланного (с пометкой источника)
                attachment TEXT                    -- JSON вложения демона (картинка/файл) или NULL
            );
            CREATE TABLE IF NOT EXISTS live (
                job_id INTEGER PRIMARY KEY,        -- живое сообщение хода: чтобы после рестарта
                chat_id INTEGER NOT NULL,          -- не осталось висеть «Думаю…»
                message_id INTEGER,
                created_at INTEGER NOT NULL
            );
        """)

    # ---------- kv ----------
    def kv_get(self, key, default=None):
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def kv_set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, json.dumps(value)))

    # ---------- темы ----------
    def topic_at(self, chat_id, thread_id):
        return self.db.execute(
            "SELECT * FROM topics WHERE chat_id = ? AND IFNULL(thread_id, 0) = IFNULL(?, 0) AND state != 'archived'",
            (chat_id, thread_id)).fetchone()

    def topic(self, topic_id):
        return self.db.execute("SELECT * FROM topics WHERE id = ?", (topic_id,)).fetchone()

    def topic_by_kind(self, chat_id, kind):
        return self.db.execute(
            "SELECT * FROM topics WHERE chat_id = ? AND kind = ? AND state != 'archived' ORDER BY id LIMIT 1",
            (chat_id, kind)).fetchone()

    def topics_for_session(self, session_id):
        return self.db.execute(
            "SELECT * FROM topics WHERE session_id = ? AND state != 'archived'", (session_id,)).fetchall()

    def dialog_topics(self, chat_id):
        return self.db.execute(
            "SELECT * FROM topics WHERE chat_id = ? AND kind IN ('dialog', 'tgchat') AND state != 'archived' ORDER BY id",
            (chat_id,)).fetchall()

    def add_topic(self, chat_id, thread_id, kind="dialog", title=None, session_id=None, pending=None):
        # Уже есть живая запись на этом месте (гонка sync ↔ событие forum_topic_created) —
        # не плодим дубль, а до-привязываем: проставляем session_id/title.
        existing = self.topic_at(chat_id, thread_id)
        if existing:
            fields = {}
            if session_id and not existing["session_id"]:
                fields["session_id"] = session_id
            if title and not (existing["title"] or "").strip():
                fields["title"] = title
            return self.update_topic(existing["id"], **fields) if fields else existing
        # Освобождаем место от архивной записи (тему пересоздали)
        self.db.execute(
            "DELETE FROM topics WHERE chat_id = ? AND IFNULL(thread_id, 0) = IFNULL(?, 0) AND state = 'archived'",
            (chat_id, thread_id))
        cur = self.db.execute(
            "INSERT INTO topics (chat_id, thread_id, kind, title, session_id, pending_settings, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_id, thread_id, kind, title, session_id, json.dumps(pending) if pending else None, int(time.time())))
        return self.topic(cur.lastrowid)

    def topic_by_title(self, chat_id, title, kind="dialog"):
        return self.db.execute(
            "SELECT * FROM topics WHERE chat_id = ? AND kind = ? AND title = ? AND state != 'archived' ORDER BY id",
            (chat_id, kind, title)).fetchall()

    def delete_topic(self, topic_id):
        self.db.execute("DELETE FROM topics WHERE id = ?", (topic_id,))

    # ---------- живые сообщения (переживание рестарта) ----------
    def live_add(self, job_id, chat_id, message_id):
        self.db.execute("INSERT OR REPLACE INTO live (job_id, chat_id, message_id, created_at) VALUES (?, ?, ?, ?)",
                        (job_id, chat_id, message_id, int(time.time())))

    def live_del(self, job_id):
        self.db.execute("DELETE FROM live WHERE job_id = ?", (job_id,))

    def live_all(self):
        return self.db.execute("SELECT * FROM live").fetchall()

    def update_topic(self, topic_id, **fields):
        if "pending_settings" in fields and fields["pending_settings"] is not None:
            fields["pending_settings"] = json.dumps(fields["pending_settings"])
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE topics SET {cols} WHERE id = ?", (*fields.values(), topic_id))
        return self.topic(topic_id)

    def remap_session(self, temp_key, session_id):
        self.db.execute("UPDATE topics SET session_id = ? WHERE session_id = ?", (session_id, temp_key))

    # ---------- буфер контекста (ТЗ §4.3) ----------
    def buffer_add(self, text, attachment=None):
        self.db.execute("INSERT INTO buffer (created_at, text, attachment) VALUES (?, ?, ?)",
                        (int(time.time()), text, json.dumps(attachment) if attachment else None))

    def buffer_items(self, max_age_s=600):
        self.db.execute("DELETE FROM buffer WHERE created_at < ?", (int(time.time()) - max_age_s,))
        rows = self.db.execute("SELECT * FROM buffer ORDER BY id").fetchall()
        return [{"text": r["text"], "attachment": json.loads(r["attachment"]) if r["attachment"] else None} for r in rows]

    def buffer_clear(self):
        self.db.execute("DELETE FROM buffer")

    # ---------- аварийная блокировка (/panic) ----------
    def locked(self) -> bool:
        return bool(self.kv_get("panic", False))

    def set_locked(self, value: bool):
        self.kv_set("panic", bool(value))
