#!/usr/bin/env bash
# Сменить токен бота: берётся из Vaultwarden через брокер secret, значение нигде не печатается.
#
#   export BW_SESSION=$(bw unlock --raw)          # мастер-пароль вводит только владелец
#   ~/projects/mycelium-ardena/deploy/set-token.sh [имя-записи]
#
# Имя записи по умолчанию — telegram/mycelium-ardena-bot.
# Токен проверяется у Telegram (getMe) до записи в конфиг; бот перезапускается,
# когда в ClaudeDaemon не идёт ход (иначе ответ идущего хода потерялся бы).
set -euo pipefail

NAME="${1:-telegram/mycelium-ardena-bot}"
ENV_FILE="$HOME/.config/claude-tg/env"
export NODE_EXTRA_CA_CERTS="${NODE_EXTRA_CA_CERTS:-$HOME/vaultwarden/data/certs/cert.pem}"

say() { printf '\033[1;36m[claude-tg]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[claude-tg]\033[0m %s\n' "$*" >&2; exit 1; }

[ -n "${BW_SESSION:-}" ] || die "Хранилище заперто. Сначала: export BW_SESSION=\$(bw unlock --raw)"
[ -f "$ENV_FILE" ] || die "Нет конфига $ENV_FILE — сначала deploy/install.sh"

say "Синхронизирую хранилище…"
bw sync --session "$BW_SESSION" >/dev/null || die "bw sync не прошёл"

tpl="$(mktemp)"; trap 'rm -f "$tpl"' EXIT
echo "{{$NAME}}" > "$tpl"
out="$("$HOME/.local/bin/secret" render "$tpl")" || die "Не удалось достать «$NAME» из хранилища"
TOKEN="$(tr -d '[:space:]' < "$out")"; rm -f "$out"
[ -n "$TOKEN" ] || die "Запись «$NAME» пустая"

# Проверка у Telegram — до записи в конфиг. Токен уходит только в адрес запроса к API.
BOTNAME="$(curl -s -m 15 "https://api.telegram.org/bot${TOKEN}/getMe" \
  | python3 -c 'import json,sys; r=json.load(sys.stdin); print(r["result"]["username"] if r.get("ok") else "")')"
[ -n "$BOTNAME" ] || die "Telegram не принял токен из «$NAME»"
say "Токен рабочий: @$BOTNAME"

# Замена строки в конфиге; значение передаётся через окружение, не через аргументы команды
TOKEN="$TOKEN" ENV_FILE="$ENV_FILE" python3 - <<'PY'
import os, re
p, tok = os.environ["ENV_FILE"], os.environ["TOKEN"]
s = open(p).read()
line = f"CLAUDE_TG_BOT_TOKEN={tok}"
s = re.sub(r"^CLAUDE_TG_BOT_TOKEN=.*$", line, s, flags=re.M) if "CLAUDE_TG_BOT_TOKEN=" in s else line + "\n" + s
open(p, "w").write(s)
os.chmod(p, 0o600)
PY
unset TOKEN
say "Конфиг обновлён: $ENV_FILE (600)"

busy() {
  local tok; tok="$(cat "$HOME/.claude-pocket/token" 2>/dev/null)" || return 1
  curl -s -m 5 -H "Authorization: Bearer $tok" http://127.0.0.1:8787/api/queue \
    | python3 -c 'import json,sys; q=json.load(sys.stdin); sys.exit(0 if q.get("running") or q.get("queued") else 1)' 2>/dev/null
}
for i in $(seq 1 36); do busy || break; [ "$i" = 1 ] && say "В демоне идёт ход — жду…"; sleep 5; done
sudo systemctl restart mycelium-claude-tg
say "Готово: бот работает как @$BOTNAME. Шапки тем он перевыпустит сам при старте."
