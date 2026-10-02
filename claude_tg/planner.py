"""Планировщик: напоминания и задания по расписанию (ТЗ §8).

Два вида:
  • remind — в срок присылает готовый текст с кнопками. Модель не вызывается, квота не тратится;
  • task   — в срок отправляет промпт в сессию; ответ приходит в тему обычным ходом.

Куда: напоминание по делу сессии — в тему этой сессии; личное, без привязки — в TGChat
(решение Алексея 30-09-2026). Тихие часы соблюдает единая отправка (tgio.Out).

Надёжность (правило №27): тик раз в 30 с обновляет файл-пульс — Console видит,
что планировщик жив; пропущенное за время простоя досылается с пометкой «опоздало».
"""
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from . import render, schedule

log = logging.getLogger(__name__)

TICK = 30                 # с между проверками
NAG_AFTER = 3 * 3600      # без ответа — напомнить снова через 3 ч
MAX_NAGS = 3              # не больше 3 повторов
LATE_NOTE = 300           # опоздание больше 5 мин — пишем «опоздало»
LOCK_RETRY = 600          # сессия задания открыта в VS Code — повтор через 10 мин
MIN_STEP = 10            # шаг минут в выборе времени «Позже» / «Завтра»


class Planner:
    def __init__(self, bot):
        self.b = bot
        self.tz = ZoneInfo(bot.cfg.tz)
        self.heartbeat = bot.cfg.data_dir / "heartbeat"

    # ---------- цикл ----------
    async def loop(self):
        await asyncio.sleep(3)
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("тик планировщика")
            try:
                self.heartbeat.touch()
            except OSError as e:
                log.warning("пульс: %s", e)
            await asyncio.sleep(TICK)

    async def tick(self):
        if self.b.store.locked():
            return                       # /panic — проактивность тоже молчит
        now = time.time()
        for r in self.b.store.rem_due(now):
            try:
                await self.fire(r, now)
            except Exception:
                log.exception("напоминание #%s", r["id"])
        for r in self.b.store.rem_unanswered(now - NAG_AFTER):
            if r["nags"] < MAX_NAGS and not r["repeat"]:
                try:
                    await self.send_reminder(r, nag=True)
                except Exception:
                    log.exception("повтор напоминания #%s", r["id"])

    # ---------- куда ----------
    def target(self, r):
        """(chat_id, thread_id, topic) для напоминания. Тема пропала — в TGChat, нет и её — в личку."""
        b = self.b
        t = b.store.topic(r["topic_id"]) if r["topic_id"] else None
        if not t or t["state"] == "archived":
            t = b.store.topic_by_kind(b.cfg.home_chat_id, "tgchat")
        if t:
            return t["chat_id"], t["thread_id"], t
        return b.cfg.owner_id, None, None

    def where_name(self, r) -> str:
        _, _, t = self.target(r)
        if not t:
            return "личка"
        return "TGChat" if t["kind"] == "tgchat" else f"«{t['title']}»"

    # ---------- срабатывание ----------
    async def fire(self, r, now):
        if r["kind"] == "task":
            return await self.run_task(r, now)
        await self.send_reminder(r, late=now - r["due_at"])

    def _markup(self, r):
        rid = r["id"]
        b = InlineKeyboardButton
        rows = [[b("✅ Сделано", callback_data=f"rmd:done:{rid}"), b("⏰ +1 ч", callback_data=f"rmd:h1:{rid}")],
                [b("🕐 Позже", callback_data=f"rmd:pick:{rid}:0"), b("📅 Завтра", callback_data=f"rmd:pick:{rid}:1")]]
        last = [b("🗑 Удалить", callback_data=f"rmd:del:{rid}")]
        if r["note"]:
            last.insert(0, b("📄 Показать заметку", callback_data=f"rmd:note:{rid}"))
        rows.append(last)
        return InlineKeyboardMarkup(rows)

    def _text(self, r, late=0, nag=False):
        lines = []
        if nag:
            lines.append("🔁 <i>Напоминаю ещё раз</i>")
        lines.append(f"🔔 <b>{render.esc(r['text'])}</b>")
        if r["note"]:
            lines.append(f"📎 {render.esc(Path(r['note']).stem)}")
        rep = schedule.describe_repeat(json.loads(r["repeat"])) if r["repeat"] else ""
        if rep:
            lines.append(f"🔄 {render.esc(rep)}")
        if late > LATE_NOTE:
            lines.append(f"<i>⌛ опоздало на {int(late // 60)} мин — бот был недоступен</i>")
        return "\n".join(lines)

    async def send_reminder(self, r, late=0, nag=False):
        chat, thread, _ = self.target(r)
        m = await self.b.out.send(chat, self._text(r, late, nag), thread_id=thread, markup=self._markup(r))
        now = int(time.time())
        fields = dict(sent_at=now, msg_chat=chat, msg_id=m.message_id, nags=r["nags"] + 1 if nag else 0)
        if r["repeat"] and not nag:
            # Повторяющееся: следующий раз считаем сразу — оно придёт, даже если на это не ответили
            nxt = schedule.next_occurrence(json.loads(r["repeat"]), datetime.now(self.tz))
            fields["due_at"] = int(nxt.timestamp())
        self.b.store.rem_update(r["id"], **fields)

    async def run_task(self, r, now):
        """Задание: промпт уходит в сессию темы фоновым ходом (приоритет ниже живого разговора)."""
        b = self.b
        chat, thread, topic = self.target(r)
        if not topic:
            self.b.store.rem_update(r["id"], state="cancelled")
            return log.warning("задание #%s: нет ни темы, ни TGChat — отменено", r["id"])
        sess = topic["session_id"]
        if sess and not sess.startswith("new-"):
            try:
                locked = (await b.daemon.lock(sess)).get("locked")
            except Exception:
                locked = False
            if locked:   # открыта в VS Code — два процесса в одном транскрипте разведут историю
                if r["nags"] == 0:
                    await b.out.send(chat, "⏳ Задание отложено: сессия сейчас открыта в VS Code. "
                                     "Повторю через 10 минут.", thread_id=thread, html_mode=False, silent=True)
                b.store.rem_update(r["id"], due_at=int(now) + LOCK_RETRY, nags=r["nags"] + 1)
                return
        when = schedule.fmt(r["due_at"], b.cfg.tz)
        prompt = (f"[Задание по расписанию #{r['id']}, срок {when}. Алексея рядом может не быть: "
                  f"сделай и коротко доложи итог.]\n\n{r['text']}")
        from .bot import Place
        place = Place("home", chat, thread, None, topic)
        await b.submit(place, prompt, question=f"⏰ задание #{r['id']}", priority=0, force=True)
        fields = dict(sent_at=int(now), nags=0)
        if r["repeat"]:
            fields["due_at"] = int(schedule.next_occurrence(json.loads(r["repeat"]), datetime.now(self.tz)).timestamp())
        else:
            fields["state"] = "done"
        b.store.rem_update(r["id"], **fields)

    # ---------- кнопки ----------
    # ---------- выбор времени: сначала час, потом минуты (шаг 10) ----------
    def _slots(self, day: int, hour: int | None = None):
        """day 0 — сегодня, 1 — завтра. Без hour → доступные часы; с hour → минуты.
        Сегодня — только будущее время (не раньше чем через минуту)."""
        now = datetime.now(self.tz)
        if day == 1:
            return list(range(24)) if hour is None else list(range(0, 60, MIN_STEP))
        def ok(h, m):
            return now.replace(hour=h, minute=m, second=0, microsecond=0) > now + timedelta(minutes=1)
        if hour is None:
            return [h for h in range(now.hour, 24) if any(ok(h, m) for m in range(0, 60, MIN_STEP))]
        return [m for m in range(0, 60, MIN_STEP) if ok(hour, m)]

    def _pick_markup(self, rid, day, hour=None):
        b = InlineKeyboardButton
        rows, line = [], []
        if hour is None:
            for h in self._slots(day):
                line.append(b(f"{h:02d}", callback_data=f"rmd:ph:{rid}:{day}:{h}"))
                if len(line) == 6:
                    rows.append(line); line = []
        else:
            for m in self._slots(day, hour):
                line.append(b(f"{hour:02d}:{m:02d}", callback_data=f"rmd:pm:{rid}:{day}:{hour}:{m}"))
                if len(line) == 3:
                    rows.append(line); line = []
        if line:
            rows.append(line)
        back = f"rmd:pick:{rid}:{day}" if hour is not None else f"rmd:back:{rid}"
        rows.append([b("← Назад", callback_data=back)])
        return InlineKeyboardMarkup(rows)

    async def _picker(self, msg, r, day, hour=None):
        when = "сегодня" if day == 0 else "завтра"
        step = "выберите час" if hour is None else f"выберите минуты ({hour:02d}:…)"
        text = self._text(r) + f"\n\n🕐 <i>Перенести на {when}: {step}</i>"
        await self.b.out.edit(text, chat_id=msg.chat.id, message_id=msg.message_id,
                              markup=self._pick_markup(r["id"], day, hour))

    async def _snooze(self, msg, r, new: datetime):
        b = self.b
        ts = int(new.timestamp())
        if r["repeat"]:   # у повторяющегося переносим только этот раз — отдельной разовой копией
            b.store.rem_add("remind", r["text"], ts, topic_id=r["topic_id"], note=r["note"], source="snooze")
        else:
            b.store.rem_update(r["id"], due_at=ts, nags=0)
        await self._close(msg, f"⏰ {render.esc(r['text'])} — перенесено на {schedule.fmt(ts, b.cfg.tz)}")

    async def on_button(self, cq, action, rid, extra=()):
        b = self.b
        r = b.store.rem(rid)
        if not r:
            return await cq.answer("Напоминание не найдено.")
        msg = cq.message
        now = datetime.now(self.tz)
        if action == "note":
            await cq.answer()
            return await self.show_note(msg, r)
        if action == "done":
            if not r["repeat"]:
                b.store.rem_update(rid, state="done")
            await self._close(msg, f"✅ <s>{render.esc(r['text'])}</s>")
            return await cq.answer("Отмечено")
        if action == "del":
            b.store.rem_update(rid, state="cancelled")
            await self._close(msg, f"🗑 <s>{render.esc(r['text'])}</s> — удалено")
            return await cq.answer("Удалено")
        if action == "h1":
            await self._snooze(msg, r, now + timedelta(hours=1))
            return await cq.answer("Перенесено на час")
        if action == "pick":                                  # «Позже» (0) / «Завтра» (1) → часы
            day = int(extra[0])
            if not self._slots(day):
                return await cq.answer("Сегодня уже поздно — выберите «Завтра».", show_alert=True)
            await self._picker(msg, r, day)
            return await cq.answer()
        if action == "ph":                                    # час выбран → минуты
            await self._picker(msg, r, int(extra[0]), int(extra[1]))
            return await cq.answer()
        if action == "pm":                                    # минуты выбраны → переносим
            day, h, m = (int(x) for x in extra[:3])
            new = (now + timedelta(days=day)).replace(hour=h, minute=m, second=0, microsecond=0)
            if new <= now:
                return await cq.answer("Это время уже прошло — выберите другое.", show_alert=True)
            await self._snooze(msg, r, new)
            return await cq.answer("Перенесено")
        if action == "back":                                  # назад к основным кнопкам
            await self.b.out.edit(self._text(r), chat_id=msg.chat.id, message_id=msg.message_id,
                                  markup=self._markup(r))
            return await cq.answer()
        await cq.answer()

    async def _close(self, msg, text):
        try:
            await self.b.out.edit(text, chat_id=msg.chat.id, message_id=msg.message_id, markup=None)
        except Exception as e:
            log.warning("правка напоминания: %s", e)

    async def show_note(self, msg, r):
        """Текст заметки Obsidian в чат (только внутри домашнего каталога, только .md)."""
        p = Path(os.path.expanduser(r["note"])).resolve()
        home = Path.home().resolve()
        thread = msg.message_thread_id if msg.is_topic_message else None
        if home not in p.parents or p.suffix.lower() != ".md" or not p.is_file():
            return await self.b.out.send(msg.chat.id, "Заметка не найдена или вне домашнего каталога.",
                                         thread_id=thread, html_mode=False)
        body = p.read_text(encoding="utf-8", errors="replace")
        if len(body) <= 3500:
            return await self.b.out.send(msg.chat.id, f"📄 <b>{render.esc(p.stem)}</b>\n\n" + render.to_html(body),
                                         thread_id=thread)
        await self.b.out.send_document(msg.chat.id, body.encode(), p.name, thread_id=thread)

    # ---------- создание (общее для команд, MCP и CLI) ----------
    def create(self, kind, text, due, *, repeat=None, topic_id=None, note=None, settings=None, source="user"):
        r = self.b.store.rem_add(kind, text, due, repeat=repeat, topic_id=topic_id, note=note,
                                 settings=settings, source=source)
        return r

    def describe(self, r, num=None, with_id=False) -> str:
        """num — порядковый номер в списке для человека; with_id — внутренний id (для моих
        инструментов и журнала: по нему отменяют). Человеку id не показываем."""
        icon = "🤖 задание" if r["kind"] == "task" else "🔔"
        rep = schedule.describe_repeat(json.loads(r["repeat"])) if r["repeat"] else ""
        when = schedule.fmt(r["due_at"], self.b.cfg.tz)
        head = f"#{r['id']} " if with_id else (f"{num}. " if num else "")
        return f"{head}{icon} {when}{' (' + rep + ')' if rep else ''} → {self.where_name(r)}: {r['text']}"
