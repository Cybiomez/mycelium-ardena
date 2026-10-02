"""MCP-сервер бота: инструменты проактивности для любой сессии Claude (ТЗ §8.2).

MCP (Model Context Protocol) — открытый стандарт подключения инструментов к модели.
Транспорт — «streamable HTTP»: JSON-RPC 2.0 через POST на /mcp, ответ — обычный JSON
(поток SSE нам не нужен: инструменты отвечают сразу). Официальный SDK `mcp` не берём —
он сам по себе 62 МБ памяти, а нужны три метода: initialize, tools/list, tools/call.

Чья сессия зовёт: Claude Code подставляет в заголовок X-Claude-Session своё
${CLAUDE_CODE_SESSION_ID} (настройка при установке). Нет заголовка — берём сессию,
которая сейчас выполняется в демоне; нет и её — цель TGChat.

Слушает только 127.0.0.1, вход — по токену (файл 600 в каталоге данных бота).
"""
import json
import logging
import secrets

from aiohttp import web

from . import render, schedule

log = logging.getLogger(__name__)

PROTOCOL = "2025-06-18"
VERSION = "0.2.0"

INSTRUCTIONS = (
    "Инструменты Telegram-бота Алексея (ClaudeTG): напоминания, задания по расписанию и сообщения в Telegram. "
    "Договорились о сроке, отложили дело, Алексей попросил напомнить — ставь напоминание и называй в ответе, "
    "когда оно придёт. Время — по Москве. Тихие часы (22–08) бот соблюдает сам: ночью приходит без звука."
)

_TARGET = {
    "type": "string",
    "description": "Куда слать. «this» (по умолчанию) — в тему текущей сессии: для дел, связанных с этой работой. "
                   "«personal» — в TGChat: личные напоминания, не связанные с сессией (купить билет, позвонить). "
                   "Либо точное название темы.",
}
_WHEN = {
    "type": "string",
    "description": "Когда, по Москве: «завтра 9:00», «через 2 часа», «в пятницу 10:00», «03.10 18:30» "
                   "или ISO «2026-10-03T10:00». Можно не указывать, если задан repeat.",
}
_REPEAT = {
    "type": "string",
    "description": "Необязательно, повтор: «каждый день 9:00», «по будням 9:00», «по выходным 11:00», "
                   "«по пн,чт 18:00», «каждое 5 число 10:00».",
}

TOOLS = [
    {
        "name": "reminder_create",
        "description": "Поставить напоминание Алексею в Telegram. В срок придёт сообщение с кнопками "
                       "«Сделано / +1 ч / Вечером / Завтра». Модель не вызывается, квоту не тратит.",
        "inputSchema": {"type": "object", "required": ["text"], "properties": {
            "text": {"type": "string", "description": "Текст напоминания — коротко и по делу."},
            "when": _WHEN, "repeat": _REPEAT, "target": _TARGET,
            "note": {"type": "string", "description": "Необязательно: путь к заметке Obsidian — у напоминания "
                                                      "будет кнопка «Показать заметку»."},
        }},
    },
    {
        "name": "task_schedule",
        "description": "Запланировать задание себе: в срок промпт уйдёт в сессию темы, ты выполнишь его "
                       "без Алексея и коротко доложишь итог в эту тему. Тратит квоту — только когда нужна модель "
                       "(проверить, разобрать, поправить); для простого «напомни» — reminder_create.",
        "inputSchema": {"type": "object", "required": ["prompt"], "properties": {
            "prompt": {"type": "string", "description": "Что сделать — самодостаточно: к моменту запуска "
                                                        "контекст разговора может быть сжат."},
            "when": _WHEN, "repeat": _REPEAT, "target": _TARGET,
        }},
    },
    {
        "name": "reminder_list",
        "description": "Список активных напоминаний и заданий: id, срок, повтор, куда, текст.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "reminder_cancel",
        "description": "Отменить напоминание или задание по id (из reminder_list).",
        "inputSchema": {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}}},
    },
    {
        "name": "message_send",
        "description": "Написать Алексею в Telegram прямо сейчас — например, доложить итог долгой работы, "
                       "не дожидаясь вопроса. Разметка markdown переводится в разметку Telegram.",
        "inputSchema": {"type": "object", "required": ["text"], "properties": {
            "text": {"type": "string"}, "target": _TARGET,
        }},
    },
]


class McpServer:
    def __init__(self, bot, port: int):
        self.b = bot
        self.port = port
        self.token_file = bot.cfg.data_dir / "mcp-token"
        self.runner = None

    def token(self) -> str:
        if not self.token_file.exists():
            self.token_file.write_text(secrets.token_hex(32) + "\n")
            self.token_file.chmod(0o600)
        return self.token_file.read_text().strip()

    async def start(self):
        self.token()        # токен — сразу при старте: его ждёт установщик для регистрации в Claude Code
        app = web.Application(client_max_size=1024 * 1024)
        app.router.add_post("/mcp", self.handle)
        app.router.add_get("/mcp", lambda r: web.Response(status=405, headers={"Allow": "POST"}))
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", self.port).start()
        log.info("MCP-сервер слушает http://127.0.0.1:%s/mcp", self.port)

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()

    # ---------- протокол ----------
    async def handle(self, req: web.Request):
        if req.headers.get("Authorization", "") != f"Bearer {self.token()}":
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await req.json()
        except ValueError:
            return web.json_response(_err(None, -32700, "parse error"), status=400)
        caller = await self._caller(req.headers.get("X-Claude-Session", ""))
        msgs = body if isinstance(body, list) else [body]
        out = [r for r in [await self._one(m, caller) for m in msgs] if r is not None]
        if not out:
            return web.Response(status=202)          # одни уведомления — ответа не нужно
        return web.json_response(out if isinstance(body, list) else out[0])

    async def _one(self, m, caller):
        mid, method, params = m.get("id"), m.get("method", ""), m.get("params") or {}
        if mid is None:                               # уведомление (notifications/initialized и т. п.)
            return None
        try:
            if method == "initialize":
                return _ok(mid, {"protocolVersion": params.get("protocolVersion") or PROTOCOL,
                                 "capabilities": {"tools": {"listChanged": False}},
                                 "serverInfo": {"name": "claude-tg", "version": VERSION},
                                 "instructions": INSTRUCTIONS})
            if method == "ping":
                return _ok(mid, {})
            if method == "tools/list":
                return _ok(mid, {"tools": TOOLS})
            if method == "tools/call":
                text, is_err = await self.call(params.get("name"), params.get("arguments") or {}, caller)
                return _ok(mid, {"content": [{"type": "text", "text": text}], "isError": is_err})
            return _err(mid, -32601, f"нет метода {method}")
        except Exception as e:
            log.exception("MCP %s", method)
            return _err(mid, -32603, str(e))

    async def _caller(self, header: str):
        """Id вызывающей сессии: заголовок → выполняющийся ход демона → None."""
        h = header.strip()
        if h and not h.startswith("${"):
            return h
        try:
            running = (await self.b.daemon.queue()).get("running") or []
            return running[0]["session"] if running else None
        except Exception:
            return None

    def _topic_for(self, target: str | None, caller):
        """(topic_id|None, подпись). None — TGChat."""
        st = self.b.store
        tgt = (target or "this").strip()
        if tgt.lower() in ("personal", "tgchat", "личное"):
            return None, "TGChat"
        if tgt.lower() == "this":
            if caller:
                for t in st.topics_for_session(caller):
                    if t["kind"] == "dialog":
                        return t["id"], f"тема «{t['title']}»"
                    if t["kind"] == "tgchat":
                        return None, "TGChat"
            return None, "TGChat (тема этой сессии не найдена)"
        rows = st.topic_by_title(self.b.cfg.home_chat_id, tgt)
        if not rows:
            raise ValueError(f"нет темы «{tgt}». Используйте target «this» или «personal».")
        return rows[0]["id"], f"тема «{tgt}»"

    async def call(self, name, args, caller):
        """→ (текст результата, это ошибка?)"""
        p = self.b.planner
        tz = self.b.cfg.tz
        try:
            if name in ("reminder_create", "task_schedule"):
                kind = "remind" if name == "reminder_create" else "task"
                text = ((args.get("text") if kind == "remind" else args.get("prompt")) or "").strip()
                if not text:
                    return "Пустой текст.", True
                due, repeat = schedule.parse_parts(args.get("when") or "", args.get("repeat"), tz)
                topic_id, where = self._topic_for(args.get("target"), caller)
                r = p.create(kind, text, due, repeat=repeat, topic_id=topic_id,
                             note=args.get("note") if kind == "remind" else None, source="mcp")
                rep = schedule.describe_repeat(repeat)
                what = "Напоминание" if kind == "remind" else "Задание"
                return (f"{what} #{r['id']} поставлено: {schedule.fmt(due, tz)} МСК"
                        f"{', ' + rep if rep else ''} → {where}."), False
            if name == "reminder_list":
                rows = self.b.store.rem_active()
                return ("\n".join(p.describe(r) for r in rows) or "Активных напоминаний и заданий нет."), False
            if name == "reminder_cancel":
                r = self.b.store.rem(int(args.get("id", 0)))
                if not r or r["state"] != "active":
                    return "Не найдено среди активных.", True
                self.b.store.rem_update(r["id"], state="cancelled")
                return f"Отменено: {p.describe(r)}", False
            if name == "message_send":
                text = (args.get("text") or "").strip()
                if not text:
                    return "Пустой текст.", True
                topic_id, where = self._topic_for(args.get("target"), caller)
                chat, thread, _ = p.target({"topic_id": topic_id})
                for ch in render.split_markdown(text):
                    await self.b.out.send(chat, render.to_html(ch), thread_id=thread)
                return f"Отправлено → {where}.", False
            return f"Нет инструмента {name}.", True
        except schedule.ParseError as e:
            return f"Время: {e}", True
        except ValueError as e:
            return str(e), True


def _ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}
