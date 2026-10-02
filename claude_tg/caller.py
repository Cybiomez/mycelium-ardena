"""Какая сессия Claude вызвала MCP-инструмент.

Протокол MCP id сессии Claude не передаёт, а переменную CLAUDE_CODE_SESSION_ID
сам процесс Claude не имеет (она есть только у его дочерних процессов), поэтому
подставить её в заголовок нельзя — проверено 02-10-2026. Определяем по соединению:

  1. порт клиента → сокет в /proc/net/tcp → процесс-владелец;
     у процесса в аргументах «--resume <id>» → это и есть сессия (точно);
  2. процесс — потомок ClaudeDaemon → сессия, которая сейчас выполняется в демоне
     (одновременно у демона идёт один ход);
  3. иначе (новая сессия VS Code без --resume) → самый свежий транскрипт
     за последние 2 минуты: во время вызова инструмента он как раз дописывается;
  4. ничего — None (цель по умолчанию станет TGChat).

Только Linux (/proc). Процессы того же пользователя — права на чтение есть.
"""
import os
import re
import time
from pathlib import Path

FRESH = 120   # с — «свежий» транскрипт


def _socket_inode(peer_port: int, server_port: int) -> str | None:
    local = f"0100007F:{peer_port:04X}"
    remote_tail = f":{server_port:04X}"
    try:
        with open("/proc/net/tcp") as f:
            next(f)
            for line in f:
                parts = line.split()
                if parts[1] == local and parts[2].endswith(remote_tail):
                    return parts[9]
    except OSError:
        pass
    return None


def _pid_of_inode(inode: str) -> int | None:
    target = f"socket:[{inode}]"
    for e in os.listdir("/proc"):
        if not e.isdigit():
            continue
        fd_dir = f"/proc/{e}/fd"
        try:
            for fd in os.listdir(fd_dir):
                try:
                    if os.readlink(f"{fd_dir}/{fd}") == target:
                        return int(e)
                except OSError:
                    continue
        except OSError:
            continue
    return None


def _cmdline(pid: int) -> list[str]:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace").split("\0")
    except OSError:
        return []


def _resume_id(pid: int) -> str | None:
    args = _cmdline(pid)
    for i, a in enumerate(args):
        if a.startswith("--resume="):
            return a.split("=", 1)[1] or None
        if a == "--resume" and i + 1 < len(args):
            return args[i + 1]
    return None


def _ppid(pid: int) -> int:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return int(stat[stat.rfind(")") + 2:].split()[1])
    except (OSError, ValueError, IndexError):
        return 0


def _is_daemon_child(pid: int) -> bool:
    for _ in range(32):
        pid = _ppid(pid)
        if pid <= 1:
            return False
        if any("mycelium-claude-daemon" in a for a in _cmdline(pid)):
            return True
    return False


def newest_transcript(cwd: str) -> str | None:
    d = Path.home() / ".claude" / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", cwd)
    try:
        files = [(p.stat().st_mtime, p.stem) for p in d.glob("*.jsonl")]
    except OSError:
        return None
    if not files:
        return None
    mtime, stem = max(files)
    return stem if time.time() - mtime <= FRESH else None


async def find_session(peer_port: int | None, server_port: int, daemon, cwd: str) -> tuple[str | None, str]:
    """→ (session_id|None, как определили) — второе для журнала."""
    pid = None
    if peer_port:
        inode = _socket_inode(peer_port, server_port)
        pid = _pid_of_inode(inode) if inode else None
    if pid:
        sid = _resume_id(pid)
        if sid:
            return sid, f"pid {pid} --resume"
        if _is_daemon_child(pid):
            try:
                running = (await daemon.queue()).get("running") or []
                if running:
                    return running[0]["session"], f"pid {pid} демон"
            except Exception:
                pass
    sid = newest_transcript(cwd)
    if sid:
        return sid, "свежий транскрипт"
    return None, "не определена"
