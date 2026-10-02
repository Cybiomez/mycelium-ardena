#!/usr/bin/env bash
# Установка mycelium-claude-tg — Telegram-бота для работы с Claude через ClaudeDaemon.
# Код → ~/.mycelium-claude-tg/app, окружение Python → ~/.mycelium-claude-tg/venv,
# конфиг → ~/.config/claude-tg/env (600), systemd-сервис mycelium-claude-tg.
#
# Токен бота берётся из Vaultwarden через брокер secret (нужна разблокировка:
#   export BW_SESSION=$(bw unlock --raw)
# и имя записи в CLAUDE_TG_SECRET, по умолчанию telegram/mycelium-altsy-bot).
# Для первичного переезда с Altsy: CLAUDE_TG_TOKEN_FROM=<файл с BOT_TOKEN=…>.
# Остальное: CLAUDE_TG_OWNER_ID, CLAUDE_TG_HOME_CHAT_ID (или OWNER_ID из файла выше).
set -euo pipefail

BASE="$HOME/.mycelium-claude-tg"
APP="$BASE/app"
VENV="$BASE/venv"
ENV_FILE="$HOME/.config/claude-tg/env"
UNIT=/etc/systemd/system/mycelium-claude-tg.service
SRC="$(cd "$(dirname "$0")/.." && pwd)"

say() { printf '\033[1;36m[claude-tg]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[claude-tg]\033[0m %s\n' "$*" >&2; exit 1; }

command -v python3 >/dev/null || die "нужен python3 >= 3.11"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || die "python3 слишком старый, нужен >= 3.11"

# 1. Код и зависимости
say "Ставлю код: $SRC → $APP"
mkdir -p "$APP"
rm -rf "$APP/claude_tg"
cp -r "$SRC/claude_tg" "$SRC/requirements.txt" "$APP/"
[ -d "$VENV" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$APP/requirements.txt"

# 2. Конфиг (создаётся один раз; повторная установка его не трогает)
if [ ! -s "$ENV_FILE" ]; then
  mkdir -p "$(dirname "$ENV_FILE")"; chmod 700 "$(dirname "$ENV_FILE")"
  token=""; owner="${CLAUDE_TG_OWNER_ID:-}"
  if [ -n "${BW_SESSION:-}" ]; then
    tpl="$(mktemp)"; echo "{{${CLAUDE_TG_SECRET:-telegram/mycelium-altsy-bot}}}" > "$tpl"
    out="$("$HOME/.local/bin/secret" render "$tpl")"; token="$(cat "$out")"; rm -f "$tpl" "$out"
  elif [ -n "${CLAUDE_TG_TOKEN_FROM:-}" ]; then
    token="$(grep -E '^BOT_TOKEN=' "$CLAUDE_TG_TOKEN_FROM" | head -1 | cut -d= -f2- | tr -d "\"'")"
    [ -n "$owner" ] || owner="$(grep -E '^OWNER_ID=' "$CLAUDE_TG_TOKEN_FROM" | head -1 | cut -d= -f2- | tr -d "\"'")"
  fi
  [ -n "$token" ] || die "нет токена: разблокируйте Vaultwarden (BW_SESSION) или задайте CLAUDE_TG_TOKEN_FROM"
  [ -n "$owner" ] || die "не задан CLAUDE_TG_OWNER_ID"
  [ -n "${CLAUDE_TG_HOME_CHAT_ID:-}" ] || die "не задан CLAUDE_TG_HOME_CHAT_ID"
  umask 077
  {
    echo "CLAUDE_TG_BOT_TOKEN=$token"
    echo "CLAUDE_TG_OWNER_ID=$owner"
    echo "CLAUDE_TG_HOME_CHAT_ID=$CLAUDE_TG_HOME_CHAT_ID"
    [ -n "${CLAUDE_TG_TGCHAT_SESSION:-}" ] && echo "CLAUDE_TG_TGCHAT_SESSION=$CLAUDE_TG_TGCHAT_SESSION"
  } > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  say "Конфиг записан: $ENV_FILE (600)"
else
  say "Конфиг уже есть: $ENV_FILE — не трогаю"
fi

# 3. Команда claude-tg (например, claude-tg unlock — снять /panic)
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/claude-tg" <<CLI
#!/usr/bin/env bash
set -a; . "$ENV_FILE"; set +a
cd "$APP" && exec "$VENV/bin/python" -m claude_tg "\$@"
CLI
chmod 755 "$HOME/.local/bin/claude-tg"

# 4. systemd (системный сервис, переживает выход из сеанса)
say "Регистрирую systemd-сервис (нужен sudo)…"
sudo tee "$UNIT" >/dev/null <<U
[Unit]
Description=MyCelium ClaudeTG — Telegram-бот для работы с Claude
After=network-online.target mycelium-claude-daemon.service
Wants=network-online.target

[Service]
Type=simple
User=$USER
Environment=HOME=$HOME
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=$ENV_FILE
WorkingDirectory=$APP
ExecStart=$VENV/bin/python -m claude_tg
Restart=always
RestartSec=5
# Бокс — 2 ГБ: потолок памяти бота (замер ~55–70 МБ)
MemoryMax=150M

[Install]
WantedBy=multi-user.target
U
sudo systemctl daemon-reload
sudo systemctl enable mycelium-claude-tg >/dev/null

# Не перезапускать бота посреди хода: бот держит живые сообщения ходов в памяти,
# и ответ идущего хода не дошёл бы до Telegram. Ждём до 3 минут; FORCE=1 — без ожидания.
busy() {
  local tok; tok="$(cat "$HOME/.claude-pocket/token" 2>/dev/null)" || return 1
  curl -s -m 5 -H "Authorization: Bearer $tok" http://127.0.0.1:8787/api/queue \
    | python3 -c 'import json,sys; q=json.load(sys.stdin); sys.exit(0 if q.get("running") or q.get("queued") else 1)' 2>/dev/null
}
if [ "${FORCE:-0}" != 1 ] && systemctl is-active --quiet mycelium-claude-tg; then
  for i in $(seq 1 36); do busy || break; [ "$i" = 1 ] && say "В демоне идёт ход — жду, пока закончится…"; sleep 5; done
  busy && die "Ход всё ещё идёт — перезапуск отложен. Повторите позже или FORCE=1 ./deploy/install.sh"
fi
sudo systemctl restart mycelium-claude-tg

# 5. MCP-сервер бота → в Claude Code (уровень пользователя: виден и VS Code, и сессиям демона).
#    Вызывающую сессию бот определяет по соединению (claude_tg/caller.py). Токен — бот создаёт сам.
TOKEN_FILE="${CLAUDE_TG_DATA_DIR:-$HOME/.local/share/claude-tg}/mcp-token"
for _ in $(seq 1 20); do [ -s "$TOKEN_FILE" ] && break; sleep 1; done
if [ -s "$TOKEN_FILE" ] && command -v claude >/dev/null; then
  claude mcp remove claude-tg -s user >/dev/null 2>&1 || true
  claude mcp add --transport http --scope user claude-tg "http://127.0.0.1:${CLAUDE_TG_MCP_PORT:-8790}/mcp" \
    --header "Authorization: Bearer $(cat "$TOKEN_FILE")" >/dev/null
  say "MCP-сервер claude-tg зарегистрирован в Claude Code (новые сессии увидят инструменты)"
else
  say "MCP не зарегистрирован: нет токена ($TOKEN_FILE) или команды claude"
fi

# 6. Манифест для MyCelium Console
mkdir -p "$HOME/.config/eco/registry"
cp "$SRC/registry.json" "$HOME/.config/eco/registry/09-mycelium-claude-tg.json"

say "Готово. Журнал: journalctl -u mycelium-claude-tg -f"
