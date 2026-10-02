"""Один ход = одно живое сообщение (ТЗ §7.1).

Пока идёт ход, сообщение показывает одну строку состояния («Думаю», «Ищу»,
«Читаю X»…), растущий текст ответа и кнопку «Стоп». По окончании оно
становится итоговым: текст ответа + свёрнутая цитата «Размышления» + свёрнутая
цитата «Ход работы». Служебных сообщений вида «Думаю…» отдельно нет.
"""
import asyncio
import logging
import os
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from . import render

log = logging.getLogger(__name__)

THINKING_TAIL = 1500       # символов размышлений в цитате (ТЗ §7.3)
LOG_LINES = 25             # строк «Хода работы» в цитате
INLINE_BUDGET = 3500       # лимит ответа инлайн-сообщения (ТЗ §4.3)
DOC_THRESHOLD = 12_000     # длиннее — дополнительно файлом .md


def _short(s, n=60):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def describe_tool(name: str, inp: dict) -> str:
    """Строка действия для статуса и «Хода работы»."""
    inp = inp if isinstance(inp, dict) else {}
    base = lambda p: os.path.basename(str(p).rstrip("/")) or str(p)
    if name == "Read":
        return f"📖 Читаю {base(inp.get('file_path', ''))}"
    if name in ("Edit", "MultiEdit", "NotebookEdit"):
        return f"✏ Правлю {base(inp.get('file_path') or inp.get('notebook_path', ''))}"
    if name == "Write":
        return f"📝 Пишу {base(inp.get('file_path', ''))}"
    if name == "Bash":
        return f"▶ {_short(inp.get('description') or inp.get('command', ''))}"
    if name in ("Grep", "Glob"):
        return f"🔎 Ищу «{_short(inp.get('pattern', ''), 40)}»"
    if name == "WebSearch":
        return f"🌐 Ищу в сети «{_short(inp.get('query', ''), 40)}»"
    if name == "WebFetch":
        return f"🌐 Открываю {_short(inp.get('url', ''), 50)}"
    if name in ("Agent", "Task"):
        return f"🤖 Субагент: {_short(inp.get('description', ''), 50)}"
    if name == "TodoWrite":
        return "🗒 Обновляю план задач"
    if name == "AskUserQuestion":
        return "❓ Задаю вопрос"
    if name == "Skill":
        return f"🧩 Навык {inp.get('skill', '')}"
    if name.startswith("mcp__"):
        return f"🔌 {name.split('__')[-1]}"
    return f"🔧 {name}"


def _fmt_dur(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 60} мин {sec % 60} с" if sec >= 60 else f"{sec} с"


class Turn:
    """Живое сообщение одного хода. Цель — либо чат/тема, либо инлайн-сообщение."""

    def __init__(self, out, cfg, *, job_id, session_key, chat_id=None, thread_id=None,
                 reply_to=None, inline_id=None, on_final=None, on_created=None, on_done=None):
        self.out, self.cfg = out, cfg
        self.job_id = job_id
        self.sessions = {session_key}
        self.chat_id, self.thread_id, self.reply_to = chat_id, thread_id, reply_to
        self.inline_id = inline_id
        self.on_final = on_final            # async fn(turn, answer_md) — копия в TGChat, шапка
        self.on_created = on_created         # fn(turn) — запомнить живое сообщение (переживание рестарта)
        self.on_done = on_done              # fn(turn) — забыть живое сообщение
        self.msg_id = None
        self.status = "⏳ Жду очереди"
        self.started = None
        self.actions: list[str] = []
        self.stream = ""                    # текст текущего блока ответа (превью)
        self.answer_parts: list[str] = []   # текст ассистента после последнего инструмента
        self.thinking = ""
        self.model = None
        self.context_pct = None
        self.dirty = True
        self.done = False
        self.error = None
        self._last_edit = 0.0
        self._lock = asyncio.Lock()
        self._create_lock = asyncio.Lock()
        self._ticker = None

    @property
    def session(self):
        real = [s for s in self.sessions if not str(s).startswith("new-")]
        return real[0] if real else next(iter(self.sessions))

    # ---------- кнопки ----------
    def _stop_markup(self):
        return InlineKeyboardMarkup([[InlineKeyboardButton("⏹ Стоп", callback_data=f"stop:{self.job_id}")]])

    # ---------- отрисовка ----------
    def _status_line(self):
        parts = [self.status]
        if self.started:
            parts.append(_fmt_dur(time.time() - self.started))
        if self.actions:
            parts.append(f"действий: {len(self.actions)}")
        return " · ".join(parts)

    def _live_html(self):
        # Только короткая строка состояния. Текст ответа на ходу НЕ дописываем:
        # растущее сообщение прокручивает страницу, и начало не прочитать (просьба Алексея).
        # Ответ показывается целиком и сразу в finalize.
        return f"<i>{render.esc(self._status_line())}</i>"

    async def ensure_message(self):
        """Создать живое сообщение (для темы/чата). У инлайна оно уже есть."""
        async with self._create_lock:   # его могут создать и submit, и событие job.started
            if self.inline_id or self.msg_id:
                return
            await self._create()

    async def _create(self):
        # Живое сообщение — беззвучное и НЕ ответ на сообщение человека: правка
        # сообщения-ответа во многих клиентах шлёт уведомление на каждое изменение.
        m = await self.out.send(self.chat_id, self._live_html(), thread_id=self.thread_id,
                                markup=self._stop_markup(), silent=True)
        self.msg_id = m.message_id
        self._last_edit = time.time()
        self.dirty = False
        if self.on_created:
            try:
                self.on_created(self)
            except Exception:
                log.exception("on_created хода %s", self.job_id)
        self._ticker = asyncio.create_task(self._tick())

    async def _tick(self):
        """Правки только при реальном изменении, не чаще min_edit_interval (лимиты Telegram).
        Таймер хода сам по себе правку не вызывает — иначе лишний трафик и мигание."""
        interval = self.cfg.min_edit_interval * (2 if self.inline_id else 1)
        while not self.done:
            await asyncio.sleep(interval)
            if not self.done and self.dirty:
                await self.flush()

    async def flush(self):
        async with self._lock:
            if self.done or not (self.msg_id or self.inline_id):
                return
            self.dirty = False
            try:
                await self.out.edit(self._live_html(), chat_id=self.chat_id, message_id=self.msg_id,
                                    inline_id=self.inline_id, markup=self._stop_markup())
                self._last_edit = time.time()
            except Exception as e:
                log.warning("правка живого сообщения: %s", e)

    async def start_inline(self):
        if self.on_created:                  # запомнить инлайн-сообщение: после рестарта бот его погасит
            try:
                self.on_created(self)
            except Exception:
                log.exception("on_created инлайн-хода %s", self.job_id)
        self._ticker = asyncio.create_task(self._tick())

    # ---------- события демона ----------
    async def on_event(self, ev):
        t = ev.get("type")
        if t == "session.created":
            self.sessions.add(ev.get("session"))
        elif t == "job.waiting":
            self.status = f"⏳ Ждёт памяти: свободно {ev.get('freeMb')} МБ из {ev.get('needMb')}"
            await self.ensure_message()
        elif t == "job.started":
            self.started = self.started or time.time()
            self.status = "🧠 Думаю"
            await self.ensure_message()
        elif t == "turn.init":
            self.model = ev.get("model")
        elif t == "thinking":
            self.status = "🧠 Думаю"
        elif t == "delta":
            self.stream += ev.get("text", "")
            self.status = "✍ Пишу ответ"
        elif t == "assistant":
            self._on_assistant(ev.get("blocks") or [])
        elif t == "tool_result":
            for b in ev.get("blocks") or []:
                if b.get("isError"):
                    self.actions.append("   ⚠ инструмент вернул ошибку")
        elif t == "question":
            self.status = "❓ Жду ответа на вопрос"
        elif t == "context":
            self.context_pct = (ev.get("context") or {}).get("percentage")
        elif t == "job.done":
            if not ev.get("ok"):
                self.error = f"Ход завершился с ошибкой: {ev.get('subtype')}"
            await self.finalize()
            return
        elif t == "job.error":
            self.error = f"Ошибка сессии: {ev.get('error') or 'неизвестно'}"
            await self.finalize()
            return
        elif t == "job.interrupted":
            self.error = "⏹ Остановлено"
            await self.finalize()
            return
        self.dirty = True

    def _on_assistant(self, blocks):
        texts = [b["text"] for b in blocks if b.get("type") == "text" and b.get("text", "").strip()]
        tools = [b for b in blocks if b.get("type") == "tool_use"]
        for b in blocks:
            if b.get("type") == "thinking" and b.get("text"):
                self.thinking += ("\n\n" if self.thinking else "") + b["text"]
        if tools:
            # Текст перед инструментом — промежуточный комментарий, уходит в «Ход работы»
            for tx in self.answer_parts + texts:
                self.actions.append("💬 " + _short(tx, 120))
            self.answer_parts = []
            for tb in tools:
                d = describe_tool(tb.get("name", ""), tb.get("input") or {})
                self.actions.append(d)
                self.status = d
            self.stream = ""
        elif texts:
            self.answer_parts.extend(texts)

    # ---------- итог ----------
    def _extras(self):
        """Свёрнутые цитаты: размышления и ход работы (ТЗ §7.1, §7.3)."""
        parts = []
        if self.thinking.strip():
            th = self.thinking.strip()
            tail = ("…" + th[-THINKING_TAIL:]) if len(th) > THINKING_TAIL else th
            parts.append(render.expandable("💭 Размышления", render.esc(tail)))
        lines = self.actions[-LOG_LINES:]
        if len(self.actions) > LOG_LINES:
            lines = [f"… и ещё {len(self.actions) - LOG_LINES} раньше"] + lines
        meta = [_fmt_dur(time.time() - (self.started or time.time()))]
        if self.model:
            meta.append(self.model)
        if self.context_pct is not None:
            meta.append(f"контекст {round(self.context_pct)}%")
        body = "\n".join(render.esc(l) for l in lines)
        body = (body + "\n" if body else "") + "⏱ " + render.esc(" · ".join(meta))
        parts.append(render.expandable(f"🛠 Ход работы · действий: {len(self.actions)}", body))
        return "\n".join(parts)

    def answer_md(self):
        ans = "\n\n".join(self.answer_parts).strip() or self.stream.strip()
        if self.error:
            ans = (ans + "\n\n" if ans else "") + f"**{self.error}**"
        return ans or "_(ассистент не дал текстового ответа)_"

    def thinking_truncated(self):
        return len(self.thinking.strip()) > THINKING_TAIL

    async def finalize(self):
        if self.done:
            return
        self.done = True
        if self._ticker:
            self._ticker.cancel()
        if self.on_done:
            try:
                self.on_done(self)
            except Exception:
                log.exception("on_done хода %s", self.job_id)
        answer = self.answer_md()
        async with self._lock:
            try:
                if self.inline_id:
                    await self._final_inline(answer)
                else:
                    await self._final_chat(answer)
            except Exception:
                log.exception("не удалось оформить итог хода %s", self.job_id)
        if self.on_final:
            try:
                await self.on_final(self, answer)
            except Exception:
                log.exception("on_final хода %s", self.job_id)

    async def _final_inline(self, answer):
        body = answer
        if len(body) > INLINE_BUDGET:
            cut = body.rfind("\n\n", 0, INLINE_BUDGET)
            body = body[: cut if cut > INLINE_BUDGET // 2 else INLINE_BUDGET] + "\n\n_…полностью — в TGChat_"
        await self.out.edit(render.to_html(body), inline_id=self.inline_id, markup=None)

    async def _final_chat(self, answer):
        chunks = [render.to_html(c) for c in render.split_markdown(answer)] or [render.esc(answer)]
        extras = self._extras()
        markup = None
        if self.thinking_truncated():
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("💭 Полностью", callback_data=f"think:{self.job_id}")]])
        last = chunks[-1] + "\n\n" + extras
        if render.visible_len(last) > render.LIMIT:      # цитаты не влезли — отдельным последним сообщением
            chunks.append(extras)
        else:
            chunks[-1] = last
        await self.ensure_message()
        for i, ch in enumerate(chunks):
            mk = markup if i == len(chunks) - 1 else None
            if i == 0:
                await self.out.edit(ch, chat_id=self.chat_id, message_id=self.msg_id, markup=mk)
            else:
                await self.out.send(self.chat_id, ch, thread_id=self.thread_id, markup=mk)
        if len(answer) > DOC_THRESHOLD:
            await self.out.send_document(self.chat_id, answer.encode(), "ответ.md", thread_id=self.thread_id,
                                         caption="Полный ответ файлом")
