"""Разбор времени напоминаний и повторов (ТЗ §8.3).

В базе всё хранится в UTC (секунды эпохи), ввод и показ — в часовом поясе
бота (МСК). Перевод — только здесь: бокс живёт в UTC, это частый источник ошибок.

Понимает:
  разовые   — «через 2 часа», «через 30 мин», «сегодня в 18:30», «завтра 9:00»,
              «послезавтра», «в пятницу 10:00», «03.10 10:00», «2026-10-03 10:00»,
              «в 18:30» / «18:30» (сегодня, а если прошло — завтра);
  повторы   — «каждый день 9:00», «по будням 9:00», «по выходным 11:00»,
              «каждый понедельник 10:00», «по пн,чт 18:00», «каждое 5 число 10:00».
Без времени в дате — 09:00.
"""
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

DEFAULT_TIME = (9, 0)
WEEKDAYS = {
    "пн": 0, "понедельник": 0, "понедельникам": 0,
    "вт": 1, "вторник": 1, "вторникам": 1,
    "ср": 2, "среда": 2, "среду": 2, "средам": 2,
    "чт": 3, "четверг": 3, "четвергам": 3,
    "пт": 4, "пятница": 4, "пятницу": 4, "пятницам": 4,
    "сб": 5, "суббота": 5, "субботу": 5, "субботам": 5,
    "вс": 6, "воскресенье": 6, "воскресеньям": 6,
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}
DAY_NAMES = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

_TIME = r"(?:в\s*)?(\d{1,2})(?:[:.](\d{2}))?"
_UNITS = {"мин": 60, "минут": 60, "минуту": 60, "минуты": 60, "м": 60,
          "час": 3600, "часа": 3600, "часов": 3600, "ч": 3600,
          "день": 86400, "дня": 86400, "дней": 86400, "сутки": 86400,
          "неделю": 604800, "недели": 604800, "недель": 604800}


class ParseError(ValueError):
    pass


def _hm(h, m):
    h, m = int(h), int(m or 0)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ParseError(f"нет такого времени: {h}:{m:02d}")
    return h, m


def _at(day: datetime, hm) -> datetime:
    return day.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)


# ---------- повторы ----------
def parse_repeat(text: str):
    """«каждый день 9:00» → ({'type': 'daily', 'time': '09:00'}, остаток) или (None, text)."""
    t = text.strip()
    low = t.lower()
    m = re.match(r"^(каждый день|ежедневно|daily)\s+" + _TIME + r"\b\s*", low)
    if m:
        h, mi = _hm(m.group(2), m.group(3))
        return {"type": "daily", "time": f"{h:02d}:{mi:02d}"}, t[m.end():].strip()
    m = re.match(r"^(по будням|в будни|weekdays)\s+" + _TIME + r"\b\s*", low)
    if m:
        h, mi = _hm(m.group(2), m.group(3))
        return {"type": "weekly", "days": [0, 1, 2, 3, 4], "time": f"{h:02d}:{mi:02d}"}, t[m.end():].strip()
    m = re.match(r"^(по выходным|в выходные|weekends)\s+" + _TIME + r"\b\s*", low)
    if m:
        h, mi = _hm(m.group(2), m.group(3))
        return {"type": "weekly", "days": [5, 6], "time": f"{h:02d}:{mi:02d}"}, t[m.end():].strip()
    m = re.match(r"^(?:кажд(?:ый|ую|ое)|по|weekly)\s+([a-zа-яё ,]+?)\s+" + _TIME + r"\b\s*", low)
    if m:
        names = [w for w in re.split(r"[ ,]+", m.group(1)) if w and w != "и"]
        days = sorted({WEEKDAYS[w] for w in names if w in WEEKDAYS})
        if days and len(days) == len([w for w in names if w in WEEKDAYS]) and all(w in WEEKDAYS for w in names):
            h, mi = _hm(m.group(2), m.group(3))
            return {"type": "weekly", "days": days, "time": f"{h:02d}:{mi:02d}"}, t[m.end():].strip()
    m = re.match(r"^(?:каждое|monthly)\s+(\d{1,2})(?:-?е)?\s*(?:число)?\s+" + _TIME + r"\b\s*", low)
    if m:
        day = int(m.group(1))
        if not 1 <= day <= 28:
            raise ParseError("день месяца — от 1 до 28 (чтобы был в каждом месяце)")
        h, mi = _hm(m.group(2), m.group(3))
        return {"type": "monthly", "day": day, "time": f"{h:02d}:{mi:02d}"}, t[m.end():].strip()
    return None, t


def next_occurrence(repeat: dict, after: datetime) -> datetime:
    """Ближайшее срабатывание повтора строго позже `after` (aware datetime в поясе бота)."""
    hm = tuple(int(x) for x in repeat["time"].split(":"))
    if repeat["type"] == "daily":
        cand = _at(after, hm)
        return cand if cand > after else cand + timedelta(days=1)
    if repeat["type"] == "weekly":
        for add in range(0, 8):
            cand = _at(after + timedelta(days=add), hm)
            if cand.weekday() in repeat["days"] and cand > after:
                return cand
    if repeat["type"] == "monthly":
        y, mo = after.year, after.month
        for _ in range(3):
            cand = _at(after.replace(year=y, month=mo, day=repeat["day"]), hm)
            if cand > after:
                return cand
            y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
    raise ParseError(f"не понимаю повтор: {repeat}")


def describe_repeat(repeat: dict | None) -> str:
    if not repeat:
        return ""
    if repeat["type"] == "daily":
        return f"каждый день в {repeat['time']}"
    if repeat["type"] == "weekly":
        d = repeat["days"]
        if d == [0, 1, 2, 3, 4]:
            return f"по будням в {repeat['time']}"
        if d == [5, 6]:
            return f"по выходным в {repeat['time']}"
        return f"по {', '.join(DAY_NAMES[i] for i in d)} в {repeat['time']}"
    if repeat["type"] == "monthly":
        return f"каждое {repeat['day']} число в {repeat['time']}"
    return str(repeat)


# ---------- разовые ----------
def parse_when(text: str, now: datetime):
    """Время в начале фразы → (aware datetime, остаток текста)."""
    t = text.strip()
    low = t.lower()
    m = re.match(r"^через\s+(полчаса|час|день|неделю)\b\s*", low)      # слово само задаёт срок
    if m:
        secs = 1800 if m.group(1) == "полчаса" else _UNITS[m.group(1)]
        return now + timedelta(seconds=secs), t[m.end():].strip()
    m = re.match(r"^через\s+(\d+)\s*([а-яё]+)\b\s*", low)
    if m:
        unit = m.group(2)
        unit_key = next((u for u in sorted(_UNITS, key=len, reverse=True) if unit.startswith(u)), None)
        if not unit_key:
            raise ParseError(f"не понимаю единицу: «{unit}»")
        return now + timedelta(seconds=int(m.group(1)) * _UNITS[unit_key]), t[m.end():].strip()

    m = re.match(r"^(сегодня|завтра|послезавтра)(?:\s+" + _TIME + r")?\b\s*", low)
    if m:
        shift = {"сегодня": 0, "завтра": 1, "послезавтра": 2}[m.group(1)]
        hm = _hm(m.group(2), m.group(3)) if m.group(2) else DEFAULT_TIME
        return _at(now + timedelta(days=shift), hm), t[m.end():].strip()

    m = re.match(r"^(?:в(?:о)?\s+)?(понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье|пн|вт|ср|чт|пт|сб|вс)"
                 r"(?:\s+" + _TIME + r")?\b\s*", low)
    if m:
        wd = WEEKDAYS[m.group(1)]
        hm = _hm(m.group(2), m.group(3)) if m.group(2) else DEFAULT_TIME
        for add in range(0, 8):
            cand = _at(now + timedelta(days=add), hm)
            if cand.weekday() == wd and cand > now:
                return cand, t[m.end():].strip()

    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?:[ t]+(\d{1,2}):(\d{2}))?\b\s*", low)
    if m:
        hm = _hm(m.group(4), m.group(5)) if m.group(4) else DEFAULT_TIME
        d = now.replace(year=int(m.group(1)), month=int(m.group(2)), day=int(m.group(3)))
        return _at(d, hm), t[m.end():].strip()

    # «03.10» — дата (по-русски точка в дате, двоеточие во времени); «18.30» — время,
    # потому что месяца 30 не бывает
    m = re.match(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?(?:\s+" + _TIME + r")?\b\s*", low)
    if m and 1 <= int(m.group(2)) <= 12 and 1 <= int(m.group(1)) <= 31:
        year = int(m.group(3)) if m.group(3) else now.year
        year = year + 2000 if year < 100 else year
        hm = _hm(m.group(4), m.group(5)) if m.group(4) else DEFAULT_TIME
        d = _at(now.replace(year=year, month=int(m.group(2)), day=int(m.group(1))), hm)
        if not m.group(3) and d < now:
            d = d.replace(year=year + 1)
        return d, t[m.end():].strip()

    m = re.match(r"^" + _TIME + r"\b\s*", low)
    if m and (m.group(2) or low.startswith("в ")):
        hm = _hm(m.group(1), m.group(2))
        cand = _at(now, hm)
        return (cand if cand > now else cand + timedelta(days=1)), t[m.end():].strip()

    raise ParseError("не понял время. Примеры: «завтра 9:00 …», «через 2 часа …», «в пятницу 10:00 …», "
                     "«03.10 18:30 …», «каждый день 9:00 …», «по будням 9:00 …»")


def parse(text: str, tz: str):
    """Полная фраза «<когда> <текст>» → (due UTC-epoch, repeat|None, текст)."""
    zone = ZoneInfo(tz)
    now = datetime.now(zone)
    repeat, rest = parse_repeat(text)
    if repeat:
        due = next_occurrence(repeat, now)
    else:
        due, rest = parse_when(text, now)
    if due <= now:
        raise ParseError("это время уже прошло")
    return int(due.timestamp()), repeat, rest


def parse_parts(when: str, repeat_text: str | None, tz: str):
    """Для MCP: время и повтор отдельными полями → (due, repeat)."""
    zone = ZoneInfo(tz)
    now = datetime.now(zone)
    if repeat_text:
        repeat, rest = parse_repeat(repeat_text)
        if not repeat or rest:
            raise ParseError(f"не понял повтор «{repeat_text}». Примеры: «каждый день 9:00», "
                             "«по будням 9:00», «по пн,чт 18:00», «каждое 5 число 10:00»")
        if not when:
            return int(next_occurrence(repeat, now).timestamp()), repeat
    else:
        repeat = None
    w = (when or "").strip()
    try:   # ISO-время от модели: «2026-10-03T10:00» — в поясе бота
        due = datetime.fromisoformat(w)
        due = due.replace(tzinfo=zone) if due.tzinfo is None else due
    except ValueError:
        due, rest = parse_when(w, now)
        if rest:
            raise ParseError(f"лишнее в поле времени: «{rest}»")
    if due <= now:
        raise ParseError("это время уже прошло")
    return int(due.timestamp()), repeat


def fmt(ts: int, tz: str) -> str:
    """UTC-epoch → «03.10 09:00» (+ «сегодня/завтра»)."""
    zone = ZoneInfo(tz)
    d = datetime.fromtimestamp(ts, zone)
    today = datetime.now(zone).date()
    if d.date() == today:
        return f"сегодня {d:%H:%M}"
    if d.date() == today + timedelta(days=1):
        return f"завтра {d:%H:%M}"
    return f"{d:%d.%m} {d:%H:%M}"
