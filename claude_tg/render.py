"""Markdown ассистента → HTML-разметка Telegram (parse_mode=HTML), ТЗ §7.2.

Telegram понимает только: <b> <i> <u> <s> <tg-spoiler> <code> <pre> <a>
<blockquote> [expandable]. Всё остальное переводим:
  заголовки → жирная строка; таблицы → моноширинный блок; списки → «•»/«◦»;
  горизонтальная линия → пустая строка. Спецсимволы < > & экранируются.
"""
import html
import re

LIMIT = 4096          # лимит Telegram на видимый текст сообщения
CHUNK = 3500          # размер куска markdown при делении длинного ответа (запас на разметку)

_FENCE = re.compile(r"^\s*(```|~~~)\s*([\w+#.-]*)\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_HR = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_NUMBERED = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")


def esc(text: str) -> str:
    return html.escape(text, quote=False)


# ---------- инлайн-разметка ----------
def _inline(text: str) -> str:
    """Инлайн-разметка одной строки. Код и ссылки прячем в заглушки, чтобы
    звёздочки и подчёркивания внутри них не превратились в курсив."""
    stash = []

    def keep(fragment):
        stash.append(fragment)
        return f"\x00{len(stash) - 1}\x00"

    text = re.sub(r"`([^`\n]+)`", lambda m: keep(f"<code>{esc(m.group(1))}</code>"), text)
    text = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+|tg://[^)\s]+)\)",
                  lambda m: keep(f'<a href="{html.escape(m.group(2), quote=True)}">{_inline_simple(m.group(1))}</a>'),
                  text)
    text = re.sub(r"(?<![\w\"'(])(https?://[^\s<>()]+[^\s<>().,;:!?'\"])",
                  lambda m: keep(f'<a href="{html.escape(m.group(1), quote=True)}">{esc(m.group(1))}</a>'), text)
    text = _inline_simple(text)
    return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], text)


def _inline_simple(text: str) -> str:
    text = esc(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\w)__(.+?)__(?!\w)", r"<b>\1</b>", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", text)
    text = re.sub(r"(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])", r"<i>\1</i>", text)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    text = re.sub(r"\|\|(.+?)\|\|", r"<tg-spoiler>\1</tg-spoiler>", text)
    return text


# ---------- таблицы ----------
def _table(rows: list[str]) -> str:
    cells = []
    for r in rows:
        if _TABLE_SEP.match(r):
            continue
        parts = [c.strip() for c in r.strip().strip("|").split("|")]
        # Разметку внутри ячеек снимаем: в моноширинном блоке она не работает
        cells.append([re.sub(r"[*_`]{1,2}", "", c) for c in parts])
    if not cells:
        return ""
    ncol = max(len(r) for r in cells)
    for r in cells:
        r.extend([""] * (ncol - len(r)))
    widths = [min(max(len(r[i]) for r in cells), 40) for i in range(ncol)]
    lines = []
    for n, r in enumerate(cells):
        lines.append("  ".join(c[:40].ljust(widths[i]) for i, c in enumerate(r)).rstrip())
        if n == 0:
            lines.append("  ".join("─" * w for w in widths))
    return "<pre>" + esc("\n".join(lines)) + "</pre>"


# ---------- блоки ----------
def to_html(md: str) -> str:
    out: list[str] = []
    lines = md.replace("\r\n", "\n").split("\n")
    i = 0
    quote: list[str] = []

    def flush_quote():
        if quote:
            out.append("<blockquote>" + "\n".join(quote) + "</blockquote>")
            quote.clear()

    while i < len(lines):
        line = lines[i]
        fence = _FENCE.match(line)
        if fence:
            flush_quote()
            lang, body = fence.group(2), []
            i += 1
            while i < len(lines) and not _FENCE.match(lines[i]):
                body.append(lines[i])
                i += 1
            i += 1   # закрывающий забор (или конец текста — незакрытый блок при стриме)
            cls = f' class="language-{esc(lang)}"' if lang else ""
            out.append(f"<pre><code{cls}>{esc(chr(10).join(body))}</code></pre>")
            continue
        if line.lstrip().startswith("|") and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            flush_quote()
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append(lines[i])
                i += 1
            out.append(_table(rows))
            continue
        q = _QUOTE.match(line)
        if q:
            quote.append(_inline(q.group(1)))
            i += 1
            continue
        flush_quote()
        if _HR.match(line):
            out.append("")
        elif (h := _HEADING.match(line)):
            out.append(f"<b>{_inline(h.group(2))}</b>")
        elif (b := _BULLET.match(line)):
            depth = len(b.group(1).replace("\t", "  ")) // 2
            marker = "•" if depth == 0 else "◦"
            out.append("   " * depth + f"{marker} {_inline(b.group(2))}")
        elif (n := _NUMBERED.match(line)):
            depth = len(n.group(1).replace("\t", "  ")) // 2
            out.append("   " * depth + f"{n.group(2)}. {_inline(n.group(3))}")
        else:
            out.append(_inline(line))
        i += 1
    flush_quote()
    text = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def to_plain(md: str) -> str:
    """Запасной вариант, если Telegram отверг разметку: без тегов вообще."""
    return md.strip()


def visible_len(html_text: str) -> int:
    """Длина видимого текста (лимит Telegram считается после разбора разметки)."""
    return len(html.unescape(re.sub(r"<[^>]+>", "", html_text)))


def split_markdown(md: str, size: int = CHUNK) -> list[str]:
    """Делит длинный markdown по абзацам, не разрывая блоки кода.
    Если блок кода сам длиннее куска — режем его по строкам и переоткрываем забор."""
    blocks, cur, in_code, fence_open = [], [], False, ""
    for line in md.split("\n"):
        f = _FENCE.match(line)
        cur.append(line)
        if f:
            in_code = not in_code
            fence_open = line if in_code else ""
        if not in_code and line.strip() == "":
            blocks.append("\n".join(cur))
            cur = []
    if cur:
        blocks.append("\n".join(cur))

    chunks, buf = [], ""
    for b in blocks:
        if len(buf) + len(b) + 1 <= size:
            buf = f"{buf}\n{b}" if buf else b
            continue
        if buf:
            chunks.append(buf)
            buf = ""
        while len(b) > size:
            cut = b.rfind("\n", 0, size)
            cut = cut if cut > size // 2 else size
            head, b = b[:cut], b[cut:].lstrip("\n")
            # разрезали блок кода — закрыть в этом куске и открыть в следующем
            if head.count("```") % 2 == 1:
                lang = re.search(r"```([\w+#.-]*)", head).group(1)
                head += "\n```"
                b = f"```{lang}\n" + b
            chunks.append(head)
        buf = b
    if buf.strip():
        chunks.append(buf)
    return [c.strip("\n") for c in chunks if c.strip()]


def expandable(title: str, body_html: str) -> str:
    return f"<blockquote expandable><b>{esc(title)}</b>\n{body_html}</blockquote>"
