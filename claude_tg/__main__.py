"""Точка входа.

  python -m claude_tg                          — бот
  claude-tg unlock                             — снять /panic (только с бокса)
  claude-tg remind "завтра 9:00 текст" [--note путь.md] [--topic "Тема" | --personal]
        Куда: по умолчанию — в тему той сессии Claude, из которой вызвана команда
        (её id берётся из CLAUDE_CODE_SESSION_ID — он есть у команд любой сессии, и старой,
        и новой). --personal — в TGChat (личное, не про эту работу). Из cron/скрипта
        вне сессии — тоже в TGChat.
  claude-tg reminders                          — активные напоминания и задания
"""
import logging
import os
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
    personal = "--personal" in argv
    argv = [a for a in argv if a != "--personal"]
    topic_name, note, session = take("--topic"), take("--note"), take("--session")
    phrase = " ".join(argv).strip()
    if not phrase:
        print('Формат: claude-tg remind "завтра 9:00 текст" [--note путь.md] [--topic "Тема" | --personal]')
        return 2
    try:
        due, repeat, text = schedule.parse(phrase, cfg.tz)
    except schedule.ParseError as e:
        print(f"Время: {e}")
        return 2
    st = Store(cfg.db_path)
    topic_id, where = None, "TGChat"
    if topic_name:
        rows = st.topic_by_title(cfg.home_chat_id, topic_name)
        if not rows:
            print(f"Нет темы «{topic_name}»")
            return 2
        topic_id, where = rows[0]["id"], f"тема «{topic_name}»"
    elif not personal:
        sid = session or os.environ.get("CLAUDE_CODE_SESSION_ID")
        for t in (st.topics_for_session(sid) if sid else []):
            if t["kind"] == "dialog":
                topic_id, where = t["id"], f"тема «{t['title']}»"
                break
        else:
            if sid:
                where = "TGChat (у этой сессии нет темы в группе)"
    r = st.rem_add("remind", text, due, repeat=repeat, topic_id=topic_id, note=note, source="cli")
    rep = schedule.describe_repeat(repeat)
    print(f"Напоминание #{r['id']}: {schedule.fmt(due, cfg.tz)} МСК{', ' + rep if rep else ''} → {where}: {text}")
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
