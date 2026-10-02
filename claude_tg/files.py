"""«📎 Прислать файл»: кнопки под ответом для файлов, на которые ассистент сослался (ТЗ §6.2).

Ищем в тексте ответа пути внутри домашнего каталога (абсолютные и с «~»), проверяем, что
это существующий файл не больше 50 МБ (лимит Bot API на отправку), и делаем до 5 кнопок.
Путь хранится в kv бота под коротким ключом — в кнопку влезает только 64 байта.

Секреты в Telegram не уходят никогда (правило: секреты только в Vaultwarden; Telegram хранит
переписку у себя): ключи, токены, .env, конфиги ботов, данные Vaultwarden, ~/.claude.json.
Проверка — и при показе кнопки, и при нажатии.
"""
import hashlib
import os
import re
from pathlib import Path

MAX_BYTES = 50 * 1024 * 1024
MAX_BUTTONS = 5
HOME = Path.home().resolve()

_PATH = re.compile(r"(?<![\w/])((?:~|" + re.escape(str(HOME)) + r")/[^\s`'\"<>()\[\]{}|,;]+)")
_SECRET_PARTS = {".ssh", ".gnupg", ".password-store", "vaultwarden", ".claude-pocket"}
_SECRET_NAME = re.compile(
    r"(^\.?env$|^\.env\.|token|secret|passw|credential|\.pem$|\.key$|\.p12$|\.pfx$|^id_(rsa|ed25519|ecdsa|dsa)"
    r"|\.pass$|^\.claude\.json$|^\.credentials\.json$|^config$|\.kdbx$|bw-)", re.I)
_SECRET_DIRS = (".config/claude-tg", ".config/eco-notify", ".config/feed-agent", ".config/eco-backup",
                ".config/aplogate", ".local/share/claude-tg")


def is_secret(p: Path) -> bool:
    rel = str(p.relative_to(HOME)) if HOME in p.parents else str(p)
    if any(part in _SECRET_PARTS for part in p.parts):
        return True
    if any(rel == d or rel.startswith(d + "/") for d in _SECRET_DIRS):
        return True
    return bool(_SECRET_NAME.search(p.name))


def _resolve(raw: str) -> Path | None:
    raw = raw.rstrip(".:!?»")
    try:
        p = Path(os.path.expanduser(raw)).resolve()
    except (OSError, RuntimeError):
        return None
    if HOME not in p.parents or not p.is_file():
        return None
    return p


def find(answer: str) -> list[Path]:
    out, seen = [], set()
    for m in _PATH.finditer(answer or ""):
        p = _resolve(m.group(1))
        if not p or p in seen or is_secret(p):
            continue
        try:
            if p.stat().st_size > MAX_BYTES:
                continue
        except OSError:
            continue
        seen.add(p)
        out.append(p)
        if len(out) == MAX_BUTTONS:
            break
    return out


def rows(store, answer: str, InlineKeyboardButton) -> list:
    btns = []
    for p in find(answer):
        key = hashlib.sha1(str(p).encode()).hexdigest()[:10]
        store.kv_set(f"file:{key}", str(p))
        name = p.name if len(p.name) <= 28 else p.name[:25] + "…"
        btns.append(InlineKeyboardButton(f"📎 {name}", callback_data=f"fs:{key}"))
    return [btns[i:i + 2] for i in range(0, len(btns), 2)]


def path_for(store, key: str) -> tuple[Path | None, str]:
    """→ (путь, причина отказа)."""
    raw = store.kv_get(f"file:{key}")
    if not raw:
        return None, "Ссылка на файл устарела."
    p = _resolve(raw)
    if not p:
        return None, "Файла больше нет."
    if is_secret(p):
        return None, "Это секретный файл — в Telegram не отправляю."
    if p.stat().st_size > MAX_BYTES:
        return None, "Файл больше 50 МБ — Telegram такой не примет."
    return p, ""
