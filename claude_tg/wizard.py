"""Мастер нового напоминания / задания на кнопках (просьба Алексея 02-10-2026).

Одно сообщение, которое меняется по шагам:
  1. Когда — Сегодня / Завтра / другая дата (календарь);
  2. Час, затем минуты с шагом 10 (сегодня — только будущее время);
  3. Повтор — один раз / каждый день / по будням / каждую неделю / каждый месяц;
  4. Текст — только ответом (reply) на сообщение мастера, первым таким ответом. Обычные
     сообщения в теме мастер не перехватывает — они идут как вопросы (просьба Алексея).
На каждом шаге — «← Назад» и «✖ Отмена». Состояние — в памяти процесса, живёт 30 минут.
"""
import calendar
import logging
import time
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup

from . import render, schedule

log = logging.getLogger(__name__)

TTL = 1800
STEP = 10
MONTHS = ["", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь",
          "Октябрь", "Ноябрь", "Декабрь"]
WD = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
WD_FULL = ["понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье"]


class Wizard:
    def __init__(self, bot):
        self.b = bot
        self.tz = ZoneInfo(bot.cfg.tz)
        self.state: dict[str, dict] = {}          # wid -> состояние мастера

    # ---------- общее ----------
    def _now(self):
        return datetime.now(self.tz)

    def _gc(self):
        dead = [w for w, s in self.state.items() if time.time() - s["ts"] > TTL]
        for w in dead:
            self._drop(w)

    def _drop(self, wid):
        self.state.pop(wid, None)

    def _title(self, s):
        what = "🔔 Новое напоминание" if s["kind"] == "remind" else "🤖 Новое задание"
        lines = [f"<b>{what}</b>", f"Куда: {render.esc(s['where'])}"]
        if s.get("dt"):
            lines.append(f"Когда: <b>{render.esc(self._when(s))}</b>")
        if s.get("step") == "text" and s.get("repeat") is not None:
            lines.append(f"Повтор: {render.esc(schedule.describe_repeat(s['repeat']) or 'нет')}")
        return "\n".join(lines)

    def _when(self, s):
        return schedule.fmt(int(s["dt"].timestamp()), self.b.cfg.tz) if s.get("dt") else "—"

    def _nav(self, wid, back=None):
        row = []
        if back:
            row.append(B("← Назад", callback_data=f"rw:{wid}:{back}"))
        row.append(B("✖ Отмена", callback_data=f"rw:{wid}:x"))
        return row

    async def _show(self, wid, text, rows):
        s = self.state[wid]
        s["ts"] = time.time()
        await self.b.out.edit(self._title(s) + "\n\n" + text, chat_id=s["chat"], message_id=s["msg"],
                              markup=InlineKeyboardMarkup(rows))

    # ---------- запуск ----------
    async def start(self, place, kind="remind", note_text=None):
        """place — bot.Place. Тема: в теме-диалоге — она, иначе TGChat."""
        self._gc()
        b = self.b
        topic = place.topic if place.kind == "home" and place.topic and place.topic["kind"] == "dialog" else None
        if kind == "task" and not topic:
            topic = b.store.topic_by_kind(b.cfg.home_chat_id, "tgchat")
        where = f"тема «{topic['title']}»" if topic and topic["kind"] == "dialog" else "TGChat"
        wid = uuid.uuid4().hex[:8]
        s = dict(kind=kind, chat=place.chat_id, thread=place.thread_id, foreign=place.kind == "foreign",
                 topic_id=topic["id"] if topic and topic["kind"] == "dialog" else None,
                 where=where, step="day", ts=time.time(), msg=None)
        self.state[wid] = s
        m = await b.out.send(place.chat_id, "…", thread_id=place.thread_id, silent=True)
        s["msg"] = m.message_id
        prefix = f"⚠ {render.esc(note_text)}\n\n" if note_text else ""
        await self.step_day(wid, prefix)

    # ---------- шаги ----------
    async def step_day(self, wid, prefix=""):
        s = self.state[wid]
        s.update(step="day", dt=None, repeat=None)
        today = self._now().date()
        later_today = bool(self._hours(today))
        first = [B("Завтра", callback_data=f"rw:{wid}:d:{(today + timedelta(1)).isoformat()}")]
        if later_today:
            first.insert(0, B("Сегодня", callback_data=f"rw:{wid}:d:{today.isoformat()}"))
        rows = [first,
                [B("📅 Другая дата", callback_data=f"rw:{wid}:cal:{today.year}-{today.month}")],
                self._nav(wid)]
        await self._show(wid, prefix + "<i>Шаг 1 из 4.</i> Когда напомнить?", rows)

    async def step_calendar(self, wid, year, month):
        s = self.state[wid]
        s["step"] = "cal"
        today = self._now().date()
        rows = [[B(f"{MONTHS[month]} {year}", callback_data="noop")],
                [B(d, callback_data="noop") for d in WD]]
        for week in calendar.Calendar(firstweekday=0).monthdayscalendar(year, month):
            row = []
            for d in week:
                if d == 0 or date(year, month, d) < today:
                    row.append(B(" ", callback_data="noop"))
                else:
                    row.append(B(str(d), callback_data=f"rw:{wid}:d:{date(year, month, d).isoformat()}"))
            rows.append(row)
        prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
        next_y, next_m = (year + 1, 1) if month == 12 else (year, month + 1)
        nav = []
        if (year, month) > (today.year, today.month):
            nav.append(B("‹ раньше", callback_data=f"rw:{wid}:cal:{prev_y}-{prev_m}"))
        if (next_y - today.year) * 12 + next_m - today.month <= 12:
            nav.append(B("позже ›", callback_data=f"rw:{wid}:cal:{next_y}-{next_m}"))
        if nav:
            rows.append(nav)
        rows.append(self._nav(wid, back="day"))
        await self._show(wid, "<i>Шаг 1 из 4.</i> Выберите дату:", rows)

    def _hours(self, d: date):
        now = self._now()
        if d > now.date():
            return list(range(24))
        return [h for h in range(now.hour, 24) if self._minutes(d, h)]

    def _minutes(self, d: date, h: int):
        now = self._now()
        out = []
        for m in range(0, 60, STEP):
            dt = datetime(d.year, d.month, d.day, h, m, tzinfo=self.tz)
            if dt > now + timedelta(minutes=1):
                out.append(m)
        return out

    async def step_hour(self, wid, d: date):
        s = self.state[wid]
        s.update(step="hour", day=d)
        rows, line = [], []
        for h in self._hours(d):
            line.append(B(f"{h:02d}", callback_data=f"rw:{wid}:h:{h}"))
            if len(line) == 6:
                rows.append(line); line = []
        if line:
            rows.append(line)
        rows.append(self._nav(wid, back="day"))
        label = schedule.fmt(int(datetime(d.year, d.month, d.day, 12, tzinfo=self.tz).timestamp()),
                             self.b.cfg.tz).split(" ")[0]
        await self._show(wid, f"<i>Шаг 2 из 4.</i> Дата: <b>{render.esc(label)}</b>. Выберите час:", rows)

    async def step_minute(self, wid, h: int):
        s = self.state[wid]
        d = s["day"]
        s.update(step="minute", hour=h)
        rows, line = [], []
        for m in self._minutes(d, h):
            line.append(B(f"{h:02d}:{m:02d}", callback_data=f"rw:{wid}:m:{m}"))
            if len(line) == 3:
                rows.append(line); line = []
        if line:
            rows.append(line)
        rows.append(self._nav(wid, back=f"d:{d.isoformat()}"))
        await self._show(wid, "<i>Шаг 2 из 4.</i> Выберите минуты:", rows)

    async def step_repeat(self, wid):
        s = self.state[wid]
        s["step"] = "repeat"
        dt = s["dt"]
        rows = [[B("Один раз", callback_data=f"rw:{wid}:r:once")],
                [B("Каждый день", callback_data=f"rw:{wid}:r:daily"),
                 B("По будням", callback_data=f"rw:{wid}:r:weekdays")],
                [B(f"Каждую неделю ({WD[dt.weekday()]})", callback_data=f"rw:{wid}:r:weekly")]]
        if dt.day <= 28:
            rows[-1].append(B(f"Каждый месяц ({dt.day}-го)", callback_data=f"rw:{wid}:r:monthly"))
        back = f"h:{s['hour']}" if s.get("hour") is not None else "day"
        rows.append(self._nav(wid, back=back))
        await self._show(wid, "<i>Шаг 3 из 4.</i> Повторять?", rows)

    async def step_text(self, wid):
        s = self.state[wid]
        s["step"] = "text"
        what = "о чём напомнить" if s["kind"] == "remind" else "что мне сделать в срок"
        how = "<b>ответом (reply) на это сообщение</b> — другие сообщения пойдут как обычные вопросы"
        back = "repeatback" if s.get("hour") is not None else "day"
        await self._show(wid, f"<i>Шаг 4 из 4.</i> Напишите, {what}, {how}.", [self._nav(wid, back=back)])

    # ---------- кнопки ----------
    async def on_button(self, cq, wid, action, arg):
        s = self.state.get(wid)
        if not s:
            await cq.answer("Мастер устарел — откройте заново: /remind", show_alert=True)
            try:
                await self.b.out.edit("Мастер устарел. Откройте заново: /remind", chat_id=cq.message.chat.id,
                                      message_id=cq.message.message_id, markup=None, html_mode=False)
            except Exception:
                pass
            return
        await cq.answer()
        if action == "x":
            self._drop(wid)
            return await self.b.out.edit("✖ Отменено.", chat_id=s["chat"], message_id=s["msg"], markup=None,
                                         html_mode=False)
        if action == "day":
            return await self.step_day(wid)
        if action == "cal":
            y, m = (int(x) for x in arg.split("-"))
            return await self.step_calendar(wid, y, m)
        if action == "d":
            return await self.step_hour(wid, date.fromisoformat(arg))
        if action == "h":
            return await self.step_minute(wid, int(arg))
        if action == "m":
            d, h = s["day"], s["hour"]
            s["dt"] = datetime(d.year, d.month, d.day, h, int(arg), tzinfo=self.tz)
            if s["dt"] <= self._now():
                return await self.step_hour(wid, d)
            return await self.step_repeat(wid)
        if action == "in":
            s.update(dt=self._now() + timedelta(minutes=int(arg)), hour=None, repeat=None)
            return await self.step_text(wid)
        if action == "repeatback":
            return await self.step_repeat(wid)
        if action == "r":
            dt = s["dt"]
            t = f"{dt.hour:02d}:{dt.minute:02d}"
            s["repeat"] = {
                "once": None,
                "daily": {"type": "daily", "time": t},
                "weekdays": {"type": "weekly", "days": [0, 1, 2, 3, 4], "time": t},
                "weekly": {"type": "weekly", "days": [dt.weekday()], "time": t},
                "monthly": {"type": "monthly", "day": dt.day, "time": t},
            }[arg]
            if s["repeat"] and s["repeat"]["type"] == "weekly" and s["repeat"]["days"] == [0, 1, 2, 3, 4] \
                    and dt.weekday() > 4:     # выбрали «по будням», а дата — выходной: первый раз — ближайший будний
                s["dt"] = schedule.next_occurrence(s["repeat"], dt - timedelta(minutes=1))
            return await self.step_text(wid)

    # ---------- текст ----------
    def wants_text(self, msg) -> str | None:
        """Это ответ (reply) на сообщение мастера, ждущего текст? → wid. Иначе None."""
        r = msg.reply_to_message
        if not r:
            return None
        return next((w for w, s in self.state.items() if s["msg"] == r.message_id
                     and s["chat"] == msg.chat.id and s["step"] == "text"), None)

    async def take_text(self, msg, wid, text):
        s = self.state[wid]
        text = (text or "").strip()
        if not text:
            return await self.b.out.send(s["chat"], "Нужен текст — ответьте на сообщение мастера, о чём напомнить.",
                                         thread_id=s["thread"], html_mode=False)
        self._drop(wid)
        due = int(s["dt"].timestamp())
        if due <= time.time():
            due = int(time.time()) + 60
        r = self.b.planner.create(s["kind"], text, due, repeat=s.get("repeat"), topic_id=s["topic_id"],
                                  source="user")
        body, kb = self.b.planner.confirm_view(r)
        await self.b.out.edit(body, chat_id=s["chat"], message_id=s["msg"], markup=kb)
