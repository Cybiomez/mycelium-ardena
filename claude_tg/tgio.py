"""Единая точка отправки в Telegram.

Все исходящие сообщения бота идут только через эти функции — так правила
действуют везде и обойти их нельзя:
  • тихие часы (ТЗ §8.5): в окне 22:00–08:00 МСК всё уходит без уведомления;
  • ошибка разметки → повтор простым текстом, ответ не теряется (ТЗ §7.2);
  • RetryAfter (лимит Telegram) → ждём, сколько сказали, и повторяем.
"""
import asyncio
import html
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import LinkPreviewOptions, ReplyParameters
from telegram.error import BadRequest, RetryAfter

log = logging.getLogger(__name__)
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


class Out:
    def __init__(self, bot, cfg):
        self.bot = bot
        self.cfg = cfg
        self.tz = ZoneInfo(cfg.tz)

    # ---------- тихие часы ----------
    def silent(self) -> bool:
        h = datetime.now(self.tz).hour
        a, b = self.cfg.quiet_from, self.cfg.quiet_to
        return (a <= h or h < b) if a > b else (a <= h < b)

    def now_str(self) -> str:
        return datetime.now(self.tz).strftime("%d.%m.%Y %H:%M")

    # ---------- вызов с повторами ----------
    async def _call(self, fn, **kw):
        for attempt in range(4):
            try:
                return await fn(**kw)
            except RetryAfter as e:
                await asyncio.sleep(float(getattr(e, "retry_after", 3)) + 0.5)
            except BadRequest as e:
                msg = str(e).lower()
                if "not modified" in msg:
                    return None
                if "parse" in msg and kw.get("parse_mode"):
                    log.warning("Telegram отверг разметку (%s) — отправляю простым текстом", e)
                    kw["text"] = strip_tags(kw["text"])
                    kw.pop("parse_mode", None)
                    continue
                if "reply" in msg and "reply_parameters" in kw:
                    kw.pop("reply_parameters")
                    continue
                raise
        raise RuntimeError("Telegram: не удалось после повторов")

    async def call(self, fn, **kw):
        """Любой метод Bot API с ожиданием по RetryAfter (создание тем, закрепление…)."""
        return await self._call(fn, **kw)

    async def send(self, chat_id, text, thread_id=None, reply_to=None, markup=None, html_mode=True, silent=None):
        kw = dict(chat_id=chat_id, text=text, link_preview_options=NO_PREVIEW,
                  disable_notification=self.silent() if silent is None else silent)
        if html_mode:
            kw["parse_mode"] = "HTML"
        if thread_id:
            kw["message_thread_id"] = thread_id
        if reply_to:
            kw["reply_parameters"] = ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)
        if markup is not None:
            kw["reply_markup"] = markup
        return await self._call(self.bot.send_message, **kw)

    async def edit(self, text, chat_id=None, message_id=None, inline_id=None, markup=None, html_mode=True):
        kw = dict(text=text, link_preview_options=NO_PREVIEW)
        if inline_id:
            kw["inline_message_id"] = inline_id
        else:
            kw["chat_id"], kw["message_id"] = chat_id, message_id
        if html_mode:
            kw["parse_mode"] = "HTML"
        kw["reply_markup"] = markup
        return await self._call(self.bot.edit_message_text, **kw)

    async def send_document(self, chat_id, data: bytes, filename, thread_id=None, caption=None):
        kw = dict(chat_id=chat_id, document=data, filename=filename, disable_notification=self.silent())
        if thread_id:
            kw["message_thread_id"] = thread_id
        if caption:
            kw["caption"] = caption
        return await self._call(self.bot.send_document, **kw)

    async def react(self, chat_id, message_id, emoji=None):
        """Реакция на сообщение; None — снять. Допустим только список эмодзи Telegram."""
        try:
            await self.bot.set_message_reaction(chat_id=chat_id, message_id=message_id,
                                                reaction=[emoji] if emoji else [])
        except Exception as e:
            log.debug("реакция не поставилась: %s", e)


def strip_tags(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))
