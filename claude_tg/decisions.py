"""Решения человека по ходу работы ассистента (ТЗ §7.4, фаза 3):

  • режим «С подтверждением» — «🔐 Разрешить?»: Да · Да, больше не спрашивать · Нет;
  • режим «План» — «📋 План на утверждение»: Утвердить · Доработать (замечания — ответом
    на сообщение, как в мастере напоминаний).

Куда: в тему хода; для инлайна и чужих чатов — в тему сессии (TGChat): там посторонние,
а кнопки и подробности действий — только для Алексея. Состояние — в памяти процесса;
после перезапуска бота ожидающий запрос снимается /stop.
"""
import logging

from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup

from . import render
from .turn import describe_tool

log = logging.getLogger(__name__)


class Decisions:
    def __init__(self, bot):
        self.b = bot
        self.reqs: dict[str, dict] = {}     # requestId -> {sid, kind, chat, thread, msg, desc}
        self.fix_wait: dict[int, str] = {}  # message_id сообщения «замечания?» -> requestId

    def _where(self, sess, turn):
        if turn and not turn.inline_id and turn.place and turn.place.kind in ("home", "private"):
            return turn.chat_id, turn.thread_id
        for t in self.b.store.topics_for_session(sess):
            return t["chat_id"], t["thread_id"]
        tg = self.b.store.topic_by_kind(self.b.cfg.home_chat_id, "tgchat")
        return (tg["chat_id"], tg["thread_id"]) if tg else (self.b.cfg.owner_id, None)

    async def on_event(self, kind, sess, ev, turn):
        rid = ev.get("requestId")
        if not rid:
            return
        chat, thread = self._where(sess, turn)
        if kind == "permission":
            desc = describe_tool(ev.get("tool", ""), ev.get("input") or {})
            text = f"🔐 <b>Нужно разрешение</b>\n{render.esc(desc)}" + self._details(ev.get("input") or {})
            kb = InlineKeyboardMarkup([[B("✅ Да", callback_data=f"pm:{rid}:a"),
                                        B("✅ Больше не спрашивать", callback_data=f"pm:{rid}:al")],
                                       [B("⛔ Нет", callback_data=f"pm:{rid}:d")]])
            m = await self.b.out.send(chat, text, thread_id=thread, markup=kb)
            self.reqs[rid] = dict(sid=sess, kind=kind, chat=chat, thread=thread, msg=m.message_id, desc=desc)
            return
        # план: может быть длинным — делим, кнопки под последним куском
        plan = (ev.get("plan") or "").strip() or "_(пустой план)_"
        chunks = render.split_markdown(plan)
        kb = InlineKeyboardMarkup([[B("✅ Утвердить", callback_data=f"pl:{rid}:ok"),
                                    B("✏ Доработать", callback_data=f"pl:{rid}:fix")]])
        last = None
        for i, ch in enumerate(chunks):
            head = "📋 <b>План на утверждение</b>\n\n" if i == 0 else ""
            last = await self.b.out.send(chat, head + render.to_html(ch), thread_id=thread,
                                         markup=kb if i == len(chunks) - 1 else None)
        self.reqs[rid] = dict(sid=sess, kind=kind, chat=chat, thread=thread, msg=last.message_id, desc="план")

    @staticmethod
    def _details(inp: dict) -> str:
        """Что именно сделает действие — коротко, моноширинно."""
        lines = []
        if inp.get("command"):
            lines.append(inp["command"])
        if inp.get("file_path") or inp.get("path"):
            lines.append(inp.get("file_path") or inp.get("path"))
        if inp.get("old_string") is not None:
            lines.append("- " + inp["old_string"][:200].replace("\n", "\n- "))
            lines.append("+ " + (inp.get("new_string") or "")[:200].replace("\n", "\n+ "))
        elif inp.get("content"):
            lines.append(inp["content"][:300])
        if inp.get("url"):
            lines.append(inp["url"])
        body = "\n".join(lines)[:900]
        return f"\n<pre>{render.esc(body)}</pre>" if body else ""

    # ---------- кнопки ----------
    async def on_button(self, cq, kind, rid, action):
        r = self.reqs.get(rid)
        if not r:
            await cq.answer("Запрос уже закрыт.")
            try:
                await self.b.out.call(self.b.app.bot.edit_message_reply_markup, chat_id=cq.message.chat.id,
                                      message_id=cq.message.message_id, reply_markup=None)
            except Exception:
                pass
            return
        if kind == "pm":
            decision = {"a": "allow", "al": "always", "d": "deny"}[action]
            ok = (await self.b.daemon.permission(r["sid"], rid, decision)).get("ok")
            self.reqs.pop(rid, None)
            verdict = {"allow": "✅ разрешено", "always": "✅ разрешено, больше не спрашиваю",
                       "deny": "⛔ запрещено"}[decision]
            await self._close(r, f"🔐 {render.esc(r['desc'])} — {verdict}" + ("" if ok else " (ход уже завершён)"))
            return await cq.answer()
        if action == "ok":
            ok = (await self.b.daemon.plan(r["sid"], rid, True)).get("ok")
            self.reqs.pop(rid, None)
            await self.b.out.call(self.b.app.bot.edit_message_reply_markup, chat_id=r["chat"],
                                  message_id=r["msg"], reply_markup=None)
            await self.b.out.send(r["chat"], "✅ План утверждён — выполняю." if ok else "План: ход уже завершён.",
                                  thread_id=r["thread"], html_mode=False, silent=True)
            return await cq.answer()
        # доработать: замечания — ответом на сообщение
        await cq.answer()
        await self.b.out.call(self.b.app.bot.edit_message_reply_markup, chat_id=r["chat"],
                              message_id=r["msg"], reply_markup=None)
        m = await self.b.out.send(r["chat"], "✏ Напишите замечания к плану <b>ответом (reply) на это сообщение</b>.",
                                  thread_id=r["thread"],
                                  markup=InlineKeyboardMarkup([[B("Без замечаний — предложи иначе",
                                                                  callback_data=f"pl:{rid}:nofb")]]))
        self.fix_wait[m.message_id] = rid
        r["fix_msg"] = m.message_id

    async def no_feedback(self, cq, rid):
        await cq.answer()
        await self._send_fix(rid, "")

    async def _close(self, r, text):
        try:
            await self.b.out.edit(text, chat_id=r["chat"], message_id=r["msg"], markup=None)
        except Exception as e:
            log.warning("закрыть запрос: %s", e)

    # ---------- замечания к плану ----------
    def wants_fix(self, msg) -> str | None:
        rep = msg.reply_to_message
        return self.fix_wait.get(rep.message_id) if rep else None

    async def take_fix(self, msg, rid, text):
        await self._send_fix(rid, (text or "").strip())

    async def _send_fix(self, rid, feedback):
        r = self.reqs.pop(rid, None)
        if not r:
            return
        fm = r.get("fix_msg")
        self.fix_wait.pop(fm, None)
        ok = (await self.b.daemon.plan(r["sid"], rid, False, feedback)).get("ok")
        note = "✏ Замечания отправлены — переделываю план." if ok else "План: ход уже завершён."
        if fm:
            try:
                await self.b.out.edit(note, chat_id=r["chat"], message_id=fm, markup=None, html_mode=False)
                return
            except Exception:
                pass
        await self.b.out.send(r["chat"], note, thread_id=r["thread"], html_mode=False, silent=True)
