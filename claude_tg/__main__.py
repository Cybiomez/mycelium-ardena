"""Точка входа.

  python -m claude_tg                          — бот
  claude-tg unlock                             — снять /panic (только с бокса)
  claude-tg remind "завтра 9:00 текст" [--topic "Название темы"] [--note путь.md]
                                               — напоминание из скрипта/cron (без темы — в TGChat)
  claude-tg reminders                          — активные напоминания и задания
"""
import logging
import sys

from . import config


def _cli_remind(cfg, argv):
    from . import schedule
    from .db import Store
    def take(flag):
        nonlocal argv
        if flag not in argv:
            return None
        i = argv.index(flag)
        val = argv[i + 1] if i + 1 < len(argv) else None
        argv = argv[:i] + argv[i + 2:]
        return val
    topic_name, note = take("--topic"), take("--note")
    phrase = " ".join(argv).strip()
    if not phrase:
        print('Формат: claude-tg remind "завтра 9:00 текст" [--topic "Название темы"]')
        return 2
    try:
        due, repeat, text = schedule.parse(phrase, cfg.tz)
    except schedule.ParseError as e:
        print(f"Время: {e}")
        return 2
    st = Store(cfg.db_path)
    topic_id = None
    if topic_name:
        rows = st.topic_by_title(cfg.home_chat_id, topic_name)
        if not rows:
            print(f"Нет темы «{topic_name}»")
            return 2
        topic_id = rows[0]["id"]
    r = st.rem_add("remind", text, due, repeat=repeat, topic_id=topic_id, note=note, source="cli")
    print(f"#{r['id']} → {schedule.fmt(due, cfg.tz)} МСК {schedule.describe_repeat(repeat)}: {text}")
    return 0


def _cli_list(cfg):
    from . import schedule
    from .db import Store
    rows = Store(cfg.db_path).rem_active()
    for r in rows:
        print(f"#{r['id']} {r['kind']} {schedule.fmt(r['due_at'], cfg.tz)} {r['text']}")
    if not rows:
        print("Активных нет.")
    return 0


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        cfg = config.load()
    except config.ConfigError as e:
        # Неполный конфиг — не стартуем молча, называем причину (ТЗ §12)
        logging.error("конфиг: %s", e)
        sys.exit(2)
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "unlock":
        from .db import Store
        Store(cfg.db_path).set_locked(False)
        print("Блокировка снята — бот снова принимает сообщения.")
        return
    if cmd == "remind":
        sys.exit(_cli_remind(cfg, sys.argv[2:]))
    if cmd == "reminders":
        sys.exit(_cli_list(cfg))
    from .bot import ClaudeTG
    ClaudeTG(cfg).run()


if __name__ == "__main__":
    main()
