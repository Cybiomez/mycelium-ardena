"""Конфигурация бота — только из окружения.

Всё, что привязывает бота к конкретному токену, группе и человеку, приходит
через переменные окружения (systemd EnvironmentFile). В коде нет ни токенов,
ни id, ни имени бота: имя бот узнаёт сам через getMe. Так бот переносится
на другой токен без правки кода (ТЗ §3).
"""
import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    """Конфиг неполный — бот не должен стартовать молча (урок feed-agent v2)."""


@dataclass(frozen=True)
class Config:
    bot_token: str
    owner_id: int
    home_chat_id: int
    data_dir: Path
    daemon_url: str
    daemon_token_file: Path
    tz: str
    quiet_from: int        # час начала тихих часов (по tz)
    quiet_to: int          # час окончания
    min_edit_interval: float

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bot.db"

    def daemon_token(self) -> str:
        return self.daemon_token_file.read_text().strip()


def _int(name: str) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        raise ConfigError(f"не задана переменная {name}")
    try:
        return int(raw)
    except ValueError as e:
        raise ConfigError(f"{name} должна быть числом, получено: {raw!r}") from e


def load() -> Config:
    token = os.environ.get("CLAUDE_TG_BOT_TOKEN", "").strip()
    if not token:
        raise ConfigError("не задана переменная CLAUDE_TG_BOT_TOKEN")
    data_dir = Path(os.path.expanduser(os.environ.get("CLAUDE_TG_DATA_DIR", "~/.local/share/claude-tg")))
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    token_file = Path(os.path.expanduser(os.environ.get("CLAUDE_TG_DAEMON_TOKEN_FILE", "~/.claude-pocket/token")))
    if not token_file.exists():
        raise ConfigError(f"нет файла токена демона: {token_file}")
    return Config(
        bot_token=token,
        owner_id=_int("CLAUDE_TG_OWNER_ID"),
        home_chat_id=_int("CLAUDE_TG_HOME_CHAT_ID"),
        data_dir=data_dir,
        daemon_url=os.environ.get("CLAUDE_TG_DAEMON_URL", "http://127.0.0.1:8787").rstrip("/"),
        daemon_token_file=token_file,
        tz=os.environ.get("CLAUDE_TG_TZ", "Europe/Moscow"),
        quiet_from=int(os.environ.get("CLAUDE_TG_QUIET_FROM", "22")),
        quiet_to=int(os.environ.get("CLAUDE_TG_QUIET_TO", "8")),
        min_edit_interval=float(os.environ.get("CLAUDE_TG_EDIT_INTERVAL", "1.5")),
    )
