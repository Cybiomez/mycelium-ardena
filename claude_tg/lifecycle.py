"""Жизненный цикл сессий темы: удаление в архив, перезапуск, откат, возврат из архива.

Файлы сессии бот не трогает — архив ведёт демон (POST /api/sessions/:id/archive,
/api/archive, /api/archive/:id/restore). Бот только спрашивает подтверждение,
убирает и заводит темы. Правила тем — заметка «AI — Темы и сессии».
"""
import asyncio
import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from . import render
from .daemon import DaemonError

log = logging.getLogger(__name__)

BUSY = ("Сессия занята: идёт или ждёт ход, либо она открыта в VS Code / терминале. "
        "Дождитесь конца хода (или закройте вкладку) и повторите.")


def _date(ms):
    return time.strftime("%d.%m.%Y", time.localtime(ms / 1000))


def _why(e: DaemonError) -> str:
    s = str(e)
    return BUSY if "HTTP 409" in s else f"Не получилось: {s}"


class Lifecycle:
    def __init__(self, bot):
        self.b = bot

    def _prev_key(self, topic):
        return f"prev:{topic['id']}"

    # ---------- команды ----------
    async def cmd(self, cmd, reply, place, topic):
        if place.kind != "home" or not topic or topic["kind"] != "dialog":
            return await reply(f"/{cmd} работает в теме-диалоге домашней группы.", html_mode=False)
        sess = topic["session_id"]
        has = bool(sess) and not sess.startswith("new-")
        tid = topic["id"]
        if cmd == "delete":
            what = "сессия целиком (транскрипт, файлы, чек-лист) уйдёт в архив на 90 дней, " \
                   "потом удалится безвозвратно. Вернуть до срока — /archive в General." if has else \
                   "у темы нет сессии — будет удалена только тема."
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Удалить", callback_data=f"lc:del:{tid}"),
                                        InlineKeyboardButton("Отмена", callback_data=f"lc:no:{tid}")]])
            return await reply(f"Удалить тему «{render.esc(topic['title'])}»? Тема исчезнет, {what}", kb)
        if not has:
            return await reply("У темы ещё нет сессии — она появится с первым сообщением.", html_mode=False)
        if cmd == "fresh":
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Начать заново", callback_data=f"lc:fresh:{tid}"),
                                        InlineKeyboardButton("Отмена", callback_data=f"lc:no:{tid}")]])
            return await reply("Начать тему с новой сессии? Нынешняя уйдёт в архив (90 дней, вернуть — /back), "
                               "новая начнёт с заметки «Контекст» и открытых пунктов чек-листа.", kb)
        if cmd == "back":
            prev = self.b.store.kv_get(self._prev_key(topic))
            if not prev:
                return await reply("Прежней сессии у этой темы нет — откатывать некуда.", html_mode=False)
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("↩ Вернуть прежнюю", callback_data=f"lc:back:{tid}"),
                                        InlineKeyboardButton("Отмена", callback_data=f"lc:no:{tid}")]])
            return await reply(f"Вернуть прежнюю сессию <code>{prev[:8]}</code>? Нынешняя уйдёт в архив.", kb)

    async def cmd_archive(self, reply):
        items = await self.b.daemon.archive_list()
        if not items:
            return await reply("Архив пуст.", html_mode=False)
        lines, rows = [], []
        for m in items[:20]:
            lines.append(f"• {render.esc(m.get('title') or 'Сессия')} — до {_date(m['expiresAt'])}")
            rows.append([InlineKeyboardButton(f"↩ {(m.get('title') or 'Сессия')[:40]}",
                                              callback_data=f"lc:res:{m['id']}")])
        more = f"\n…и ещё {len(items) - 20}" if len(items) > 20 else ""
        await reply("<b>Архив сессий</b> (после срока — удаляются безвозвратно)\n" + "\n".join(lines) + more,
                    InlineKeyboardMarkup(rows))

    # ---------- кнопки lc:<действие>:<аргумент> ----------
    async def on_button(self, cq, action, arg):
        msg = cq.message
        edit = lambda text: self.b.out.edit(text, chat_id=msg.chat.id, message_id=msg.message_id)
        if action == "res":
            return await self._restore(cq, edit, arg)
        topic = self.b.store.topic(int(arg))
        if not topic:
            await edit("Тема уже удалена.")
            return await cq.answer()
        if action == "no":
            await edit("Отменено.")
            return await cq.answer()
        await cq.answer("Выполняю…")
        try:
            if action == "del":
                await self._delete(topic)
            elif action == "fresh":
                await self._fresh(topic, edit)
            elif action == "back":
                await self._back(topic, edit)
        except DaemonError as e:
            await edit(render.esc(_why(e)))

    # ---------- действия ----------
    async def _delete(self, topic):
        b, sess = self.b, topic["session_id"]
        title = topic["title"] or "Тема"
        tail = "сессии не было"
        if sess and not sess.startswith("new-"):
            b.hold.add(sess)
            try:
                m = await b.daemon.archive(sess, "delete", title)
            finally:
                b.hold.discard(sess)
            tail = f"сессия <code>{sess[:8]}</code> в архиве до {_date(m['expiresAt'])} (/archive — вернуть)"
        prev = b.store.kv_get(self._prev_key(topic))
        b.store.kv_set(self._prev_key(topic), None)
        try:
            await b.out.call(b.app.bot.delete_forum_topic, chat_id=topic["chat_id"], message_thread_id=topic["thread_id"])
        except Exception as e:
            log.warning("удалить тему %s: %s", title, e)
        b.store.delete_topic(topic["id"])
        note = f" Прежняя сессия <code>{prev[:8]}</code> остаётся в архиве до своего срока." if prev else ""
        log.info("тема удалена: %s (%s)", title, (sess or "-")[:8])
        try:   # итог в General — сопроводительный: его сбой не отменяет уже сделанное удаление
            await b.out.send(b.cfg.home_chat_id, f"🗑 Тема «{render.esc(title)}» удалена, {tail}.{note}", silent=True)
        except Exception as e:
            log.warning("итог удаления в General: %s", e)

    async def _fresh(self, topic, edit):
        b, old = self.b, topic["session_id"]
        try:
            st = await b.daemon.settings(old)
            settings = {k: st.get(k) for k in ("model", "effort", "permissionMode") if st.get(k)}
        except DaemonError:
            settings = None
        b.hold.add(old)
        try:
            m = await b.daemon.archive(old, "fresh", topic["title"])
            b.store.kv_set(self._prev_key(topic), old)
            topic = b.store.update_topic(topic["id"], session_id=None,
                                         pending_settings=settings or None)
        finally:
            b.hold.discard(old)
        await edit(f"🔄 Новая сессия. Прежняя <code>{old[:8]}</code> в архиве до {_date(m['expiresAt'])}. "
                   "Вернуть — /back.")
        todo = "\n".join(m.get("openTodo") or []) or "(открытых пунктов не было)"
        seed = (f"Это новая сессия темы «{topic['title']}». Прежняя сессия {old[:8]} убрана в архив "
                "(Алексей может вернуть её командой /back).\n\n"
                "Начни так:\n"
                f"1. Найди заметку «<Проект> — Контекст», где строка «Тема в боте» называет эту тему "
                f"(`grep -rl \"Тема в боте:.*{topic['title']}\" ~/ObsidianVault/AI ~/projects/notes`), и прочитай её. "
                "Нет такой заметки — скажи об этом.\n"
                "2. Заведи свой чек-лист сессии с открытыми пунктами прежней:\n"
                f"{todo}\n"
                "3. Коротко доложи, на чём остановились, и жди задач.")
        from .bot import Place   # отложенный импорт: bot.py импортирует этот модуль
        place = Place("home", topic["chat_id"], topic["thread_id"], None, topic)
        await b.submit(place, seed, question="🔄 Перезапуск темы")
        asyncio.create_task(self._keep_title(topic["id"], topic["title"]))

    async def _keep_title(self, topic_id, title, wait=300):
        """Новой сессии — имя темы: иначе синхронизация переименует тему по авто-имени сессии,
        и строка «Тема в боте» в заметке «Контекст» перестанет совпадать."""
        until = time.time() + wait
        while time.time() < until:
            t = self.b.store.topic(topic_id)
            sid = t["session_id"] if t else None
            if sid and not sid.startswith("new-"):
                try:
                    await self.b.daemon.set_title(sid, title)
                    log.info("новая сессия %s получила имя темы «%s»", sid[:8], title)
                except DaemonError as e:
                    log.warning("имя новой сессии: %s", e)
                return
            await asyncio.sleep(2)
        log.warning("новая сессия темы «%s» не получила id за %s с — имя не задано", title, wait)

    async def _back(self, topic, edit):
        b, cur = self.b, topic["session_id"]
        prev = b.store.kv_get(self._prev_key(topic))
        if not prev:
            return await edit("Прежней сессии нет.")
        b.hold.update({cur, prev})
        try:
            await b.daemon.archive(cur, "fresh", topic["title"])
            try:
                await b.daemon.restore(prev)
            except DaemonError:
                await b.daemon.restore(cur)          # прежняя пропала — не оставлять тему пустой
                raise
            b.store.update_topic(topic["id"], session_id=prev, pending_settings=None)
            b.store.kv_set(self._prev_key(topic), cur)
        finally:
            b.hold.difference_update({cur, prev})
        await b.ensure_header(b.store.topic(topic["id"]))
        await edit(f"↩ Вернул прежнюю сессию <code>{prev[:8]}</code>. Та, что была, в архиве "
                   f"(<code>{cur[:8]}</code>, снова переключиться — /back).")

    async def _restore(self, cq, edit, sid):
        b = self.b
        b.hold.add(sid)
        try:
            try:
                r = await b.daemon.restore(sid)
            except DaemonError as e:
                return await cq.answer(_why(e)[:190], show_alert=True)
            name = (r.get("title") or "Сессия")[:128]
            ft = await b.out.call(b.app.bot.create_forum_topic, chat_id=b.cfg.home_chat_id, name=name)
            b.bot_created.add(ft.message_thread_id)
            t = b.store.topic_at(b.cfg.home_chat_id, ft.message_thread_id)
            t = b.store.update_topic(t["id"], session_id=sid, pending_settings=None) if t else \
                b.store.add_topic(b.cfg.home_chat_id, ft.message_thread_id, "dialog", name, session_id=sid)
            b.sync.mark_bound(sid)
        finally:
            b.hold.discard(sid)
        await b.ensure_header(t)
        await edit(f"↩ «{render.esc(name)}» возвращена из архива: {b._topic_link(t)}")
        await cq.answer()
