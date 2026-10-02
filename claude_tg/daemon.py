"""Клиент ClaudeDaemon: HTTP API + поток событий SSE.

Бот своих процессов Claude не запускает — всё через демон (ТЗ §2, §3).
"""
import asyncio
import json
import logging
import time

import aiohttp

log = logging.getLogger(__name__)


class DaemonError(Exception):
    pass


class Daemon:
    def __init__(self, base_url: str, token_getter):
        self.base = base_url
        self._token_getter = token_getter
        self._session: aiohttp.ClientSession | None = None

    async def start(self):
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))

    async def close(self):
        if self._session:
            await self._session.close()

    def _headers(self):
        return {"Authorization": "Bearer " + self._token_getter()}

    async def _req(self, method, path, body=None, timeout=60):
        try:
            async with self._session.request(method, self.base + path, json=body, headers=self._headers(),
                                             timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                data = await r.json(content_type=None)
                if r.status >= 400:
                    raise DaemonError(f"{method} {path}: HTTP {r.status} {data}")
                return data
        except aiohttp.ClientError as e:
            raise DaemonError(f"демон недоступен: {e}") from e

    # ---------- API ----------
    async def health(self):
        return await self._req("GET", "/api/health", timeout=5)

    async def submit(self, text, session_id=None, attachments=None, settings=None, channel=None, priority=1):
        body = {"text": text, "sessionId": session_id, "attachments": attachments or [], "priority": priority}
        if settings:
            body["settings"] = settings
        if channel:
            body["channel"] = channel
        return await self._req("POST", "/api/messages", body)   # {jobId, sessionKey, position}

    async def settings(self, session_id):
        return await self._req("GET", f"/api/sessions/{session_id}/settings")

    async def set_settings(self, session_id, **values):
        return await self._req("POST", f"/api/sessions/{session_id}/settings", values)

    async def models(self):
        return (await self._req("GET", "/api/models")).get("models") or []

    async def sessions(self):
        return (await self._req("GET", "/api/sessions")).get("sessions") or []

    async def lock(self, session_id):
        return await self._req("GET", f"/api/sessions/{session_id}/lock")

    async def queue(self):
        return await self._req("GET", "/api/queue")

    async def cancel(self, job_id):
        return (await self._req("DELETE", f"/api/jobs/{job_id}")).get("ok", False)

    async def interrupt(self, session_id):
        return await self._req("POST", f"/api/sessions/{session_id}/interrupt", {})

    async def answer(self, session_id, answers):
        return await self._req("POST", f"/api/sessions/{session_id}/answer", {"answers": answers})

    async def permission(self, session_id, request_id, decision):
        """decision: allow | always (больше не спрашивать этот инструмент) | deny"""
        return await self._req("POST", f"/api/sessions/{session_id}/permission",
                               {"requestId": request_id, "decision": decision})

    async def plan(self, session_id, request_id, approve, feedback=""):
        return await self._req("POST", f"/api/sessions/{session_id}/plan",
                               {"requestId": request_id, "approve": approve, "feedback": feedback})

    async def fork(self, session_id, title=None):
        return (await self._req("POST", f"/api/sessions/{session_id}/fork", {"title": title}))["sessionId"]

    async def usage(self):
        return (await self._req("GET", "/api/usage")).get("usage")

    async def context(self, session_id):
        return (await self._req("GET", f"/api/sessions/{session_id}/context")).get("context")

    async def set_title(self, session_id, title):
        return await self._req("POST", f"/api/sessions/{session_id}/title", {"title": title})

    async def upload(self, name, mime, b64):
        return await self._req("POST", "/api/upload", {"name": name, "mime": mime, "base64": b64}, timeout=120)

    # ---------- поток событий ----------
    async def listen(self, handler, on_connect=None):
        """Бесконечно слушает /api/stream и отдаёт события в handler(event).

        При подключении демон заново присылает свой буфер последних событий.
        Уже виденные отсекаем по паре (ts, seq): seq сбрасывается при перезапуске
        демона, а время — нет. На старте бота берём «сейчас» — старое не нужно.
        """
        last_ts, last_seq = int(time.time() * 1000), 0
        while True:
            try:
                async with self._session.get(self.base + "/api/stream", headers=self._headers(),
                                             timeout=aiohttp.ClientTimeout(total=None, sock_read=90)) as r:
                    if r.status != 200:
                        raise DaemonError(f"stream HTTP {r.status}")
                    log.info("поток событий демона подключён")
                    if on_connect:
                        asyncio.create_task(on_connect())
                    event_type, data = None, None
                    async for raw in r.content:
                        line = raw.decode("utf-8", "replace").rstrip("\r\n")
                        if line.startswith(":"):
                            continue
                        if line.startswith("event:"):
                            event_type = line[6:].strip()
                        elif line.startswith("data:"):
                            data = line[5:].strip()
                        elif line == "" and data:
                            try:
                                ev = json.loads(data)
                            except ValueError:
                                ev = None
                            event_type, data = None, None
                            if not ev:
                                continue
                            ts, seq = ev.get("ts", 0), ev.get("seq", 0)
                            if ts < last_ts or (ts == last_ts and seq <= last_seq):
                                continue
                            last_ts, last_seq = ts, seq
                            try:
                                await handler(ev)
                            except Exception:
                                log.exception("ошибка обработки события %s", ev.get("type"))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("поток событий оборвался: %s — переподключение через 3 с", e)
            await asyncio.sleep(3)
