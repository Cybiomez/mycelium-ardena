"""Темы = рабочие сессии (решение Алексея 30-09-2026).

• Синхронизация: каждая непустая сессия демона — тема в домашней группе.
  Новая сессия, начатая где угодно (VS Code, Pocket), сама появляется темой;
  переименование сессии переименовывает тему.
• Зеркало: ходы, сделанные в VS Code / терминале, беззвучно дублируются в тему
  сессии — вопрос и итоговый ответ. Ходы через демон (бот, Pocket) помечены в
  транскрипте entrypoint=sdk-ts и не зеркалятся: бот их и так показал.
"""
import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

from . import render

log = logging.getLogger(__name__)

SYNC_EVERY = 60        # с — сверка списка сессий
MIRROR_EVERY = 15      # с — проверка новых ходов из VS Code
CREATE_PAUSE = 6       # с между созданием тем (лимиты Telegram на группу)
_INJECTED = re.compile(r"^\s*<(task-notification|local-command-[a-z]+|command-(?:name|message|args)|system-reminder|channel-context)[\s>]")


def _encode_cwd(cwd: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


def _human_text(rec) -> str | None:
    """Текст, написанный человеком (не служебная вставка и не результат инструмента)."""
    if rec.get("isMeta") or rec.get("isSidechain"):
        return None
    kind = (rec.get("origin") or {}).get("kind")
    if isinstance(kind, str) and kind != "human":
        return None
    c = (rec.get("message") or {}).get("content")
    if isinstance(c, str):
        return None if _INJECTED.match(c) else (c.strip() or None)
    if isinstance(c, list):
        if any(b.get("type") == "tool_result" for b in c):
            return None
        texts = [b.get("text", "") for b in c if b.get("type") == "text" and not _INJECTED.match(b.get("text", ""))]
        t = "\n".join(texts).strip()
        if any(b.get("type") == "image" for b in c):
            t = (t + "\n[изображение]").strip()
        return t or None
    return None


class Sync:
    def __init__(self, bot):
        self.b = bot                 # ClaudeTG
        self.cwd = None
        self.renaming: set[int] = set()   # темы, которые бот сам переименовывает (не писать title обратно)

    def _file(self, sid) -> Path:
        return Path.home() / ".claude" / "projects" / _encode_cwd(self.cwd or str(Path.home())) / f"{sid}.jsonl"

    def mark_bound(self, sid):
        """Зеркало начинается с текущего конца транскрипта — старую историю не выкладываем."""
        try:
            self.b.store.kv_set(f"mir:{sid}", self._file(sid).stat().st_size)
        except OSError:
            self.b.store.kv_set(f"mir:{sid}", 0)

    # ---------- синхронизация списка ----------
    async def loop(self):
        await asyncio.sleep(5)
        last_sync = 0.0
        while True:
            try:
                if time.time() - last_sync >= SYNC_EVERY:
                    last_sync = time.time()
                    await self.sync_topics()
                await self.mirror_all()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("синхронизация тем")
            await asyncio.sleep(MIRROR_EVERY)

    async def sync_topics(self):
        b = self.b
        if not b.store.topic_by_kind(b.cfg.home_chat_id, "tgchat"):
            return   # до /setup группа не настроена — тем не создаём
        data = await b.daemon._req("GET", "/api/sessions")
        self.cwd = data.get("cwd")
        sessions = data.get("sessions") or []
        topics = {t["session_id"]: t for t in b.store.dialog_topics(b.cfg.home_chat_id) if t["session_id"]}
        pending_new = any(s.startswith("new-") for s in topics)
        now = time.time() * 1000
        for s in reversed(sessions):            # от старых к новым: свежие окажутся сверху списка тем
            sid, title = s["id"], (s.get("title") or "").strip()
            if not s.get("messageCount"):
                continue
            t = topics.get(sid)
            if t:
                if t["kind"] == "dialog" and title and title != "(без названия)" and title[:128] != (t["title"] or ""):
                    await self._rename(t, title[:128])
                continue
            # Сессия только что создана ботом и ещё не получила свой id — не дублировать
            if pending_new and now - s.get("mtime", 0) < 120_000:
                continue
            if sid in b.active or any(sid in tr.sessions for tr in b.turns.values()):
                continue
            await self._create(sid, title if title and title != "(без названия)" else f"Сессия {sid[:8]}")
            await asyncio.sleep(CREATE_PAUSE)

    async def _create(self, sid, title):
        b = self.b
        ft = await b.out.call(b.app.bot.create_forum_topic, chat_id=b.cfg.home_chat_id, name=title[:128])
        t = b.store.topic_at(b.cfg.home_chat_id, ft.message_thread_id)
        if t:
            t = b.store.update_topic(t["id"], session_id=sid, title=title[:128], pending_settings=None)
        else:
            t = b.store.add_topic(b.cfg.home_chat_id, ft.message_thread_id, "dialog", title[:128], session_id=sid)
        self.mark_bound(sid)
        await b.ensure_header(t)
        log.info("тема для сессии %s: %s", sid[:8], title)

    async def _rename(self, t, title):
        self.renaming.add(t["thread_id"])
        try:
            await self.b.out.call(self.b.app.bot.edit_forum_topic, chat_id=t["chat_id"], message_thread_id=t["thread_id"], name=title)
        except Exception as e:
            self.renaming.discard(t["thread_id"])
            log.warning("переименовать тему: %s", e)

    # ---------- зеркало ходов из VS Code ----------
    async def mirror_all(self):
        b = self.b
        for t in b.store.dialog_topics(b.cfg.home_chat_id):
            sid = t["session_id"]
            if sid and not sid.startswith("new-"):
                await self.mirror(t, sid)

    async def mirror(self, topic, sid):
        path = self._file(sid)
        key = f"mir:{sid}"
        offset = self.b.store.kv_get(key)
        try:
            size = path.stat().st_size
        except OSError:
            return
        if offset is None:
            return self.b.store.kv_set(key, size)
        if size <= offset:
            if size < offset:        # файл переписан/урезан — начинаем с конца
                self.b.store.kv_set(key, size)
            return
        with open(path, "rb") as f:
            f.seek(offset)
            chunk = f.read(min(size - offset, 8 * 1024 * 1024))
        pos = offset
        turn = None
        for raw in chunk.split(b"\n")[:-1]:     # последняя часть — недописанная строка
            line_end = pos + len(raw) + 1
            pos = line_end
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            ep = rec.get("entrypoint")
            external = bool(ep) and ep != "sdk-ts"
            rtype = rec.get("type")
            if rtype == "user" and external:
                text = _human_text(rec)
                if text:
                    turn = {"q": text, "parts": [], "start": line_end - len(raw) - 1,
                            "ts": rec.get("timestamp"), "ep": ep}
                continue
            if rtype == "assistant" and turn and external:
                msg = rec.get("message") or {}
                for blk in msg.get("content") or []:
                    if blk.get("type") == "tool_use":
                        turn["parts"] = []
                    elif blk.get("type") == "text" and blk.get("text", "").strip():
                        turn["parts"].append(blk["text"])
                if msg.get("stop_reason") == "end_turn":
                    await self._post(topic, turn)
                    turn = None
        # Незавершённый ход — перечитаем с его начала в следующий раз
        self.b.store.kv_set(key, turn["start"] if turn else pos)

    async def _post(self, topic, turn):
        b = self.b
        src = "VS Code" if "vscode" in (turn["ep"] or "") else "терминал"
        when = b.out.now_str()
        try:   # время самого хода из транскрипта (UTC) → в часовой пояс бота
            from datetime import datetime
            when = datetime.fromisoformat(turn["ts"].replace("Z", "+00:00")).astimezone(b.out.tz).strftime("%d.%m %H:%M")
        except (AttributeError, TypeError, ValueError):
            pass
        q = turn["q"] if len(turn["q"]) <= 1500 else turn["q"][:1500] + "…"
        head = f"🖥 <b>{src}</b> · {when}\n❓ {render.esc(q)}"
        answer = "\n\n".join(turn["parts"]).strip() or "_(без текстового ответа)_"
        await b.out.send(topic["chat_id"], head, thread_id=topic["thread_id"], silent=True)
        for ch in render.split_markdown(answer):
            await b.out.send(topic["chat_id"], render.to_html(ch), thread_id=topic["thread_id"], silent=True)
