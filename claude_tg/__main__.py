"""Точка входа: `python -m claude_tg` — бот; `python -m claude_tg unlock` — снять /panic (только с бокса)."""
import logging
import sys

from . import config


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        cfg = config.load()
    except config.ConfigError as e:
        # Неполный конфиг — не стартуем молча, называем причину (ТЗ §12)
        logging.error("конфиг: %s", e)
        sys.exit(2)
    if len(sys.argv) > 1 and sys.argv[1] == "unlock":
        from .db import Store
        Store(cfg.db_path).set_locked(False)
        print("Блокировка снята — бот снова принимает сообщения.")
        return
    from .bot import ClaudeTG
    ClaudeTG(cfg).run()


if __name__ == "__main__":
    main()
