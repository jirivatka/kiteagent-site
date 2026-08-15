#!/bin/bash
# Put a Hermes agent and Kite Helper on a Linux server, ready for a phone to pair.
#
# Written after installing this by hand on a 893 MB VPS with no git, no compiler,
# no sudo and a live trading stack already on it. Every check below exists because
# something went wrong without it — see vps-test/README.md in the Kite repo.
#
#   curl -O https://kiteagent.app/downloads/install-hermes-server.sh
#   bash install-hermes-server.sh
#
# Non-interactive:
#   OPENROUTER_API_KEY=sk-or-... bash install-hermes-server.sh
#
# Environment:
#   OPENROUTER_API_KEY   your key; prompted for if unset
#   KITE_HERMES_BASE     install location (default ~/kite-hermes)
#   KITE_GATEWAY_PORT    agent port, loopback only (default 8642)
#   KITE_HELPER_PORT     helper port, exposed to your private network (default 50746)
#   KITE_DEFAULT_MODEL   default model (default a free one)
set -euo pipefail

BASE="${KITE_HERMES_BASE:-$HOME/kite-hermes}"
PORT="${KITE_GATEWAY_PORT:-8642}"
HELPER_PORT="${KITE_HELPER_PORT:-50746}"
MODEL="${KITE_DEFAULT_MODEL:-nvidia/nemotron-3-super-120b-a12b:free}"
HELPER_URL="${KITE_HELPER_URL:-https://kiteagent.app/downloads/kite-helper.py}"
SRC_URL="${KITE_SOURCE_URL:-https://github.com/NousResearch/hermes-agent/archive/refs/heads/main.tar.gz}"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  ✓ %s\n' "$*"; }
warn() { printf '  ⚠ %s\n' "$*"; }
die()  { printf '\n\033[1m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- preflight
#
# Checked up front rather than discovered at the failing step, because the
# failures are otherwise misleading: a missing compiler shows up as a pip
# traceback 200 lines into an install that has already spent two minutes.

say "Checking this machine"

command -v python3 >/dev/null || die "python3 is not installed."
PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "Python 3.11 or newer is required; this is $PYV."
ok "python $PYV"

python3 -c 'import venv' 2>/dev/null \
  || die "The venv module is missing. On Debian/Ubuntu: apt install python3-venv"
ok "venv module present"

command -v curl >/dev/null || die "curl is not installed."
command -v tar  >/dev/null || die "tar is not installed."
ok "curl and tar present"

# Not fatal: only push notifications need these, and push is optional.
command -v openssl >/dev/null && ok "openssl present (needed for push)" \
  || warn "no openssl — everything works except push notifications"
curl -V | grep -q HTTP2 && ok "curl speaks HTTP/2 (needed for push)" \
  || warn "curl has no HTTP/2 — push notifications will not work"

DISK_MB=$(df -Pm "$HOME" | awk 'NR==2 {print $4}')
[ "$DISK_MB" -ge 900 ] || die "Only ${DISK_MB} MB free in $HOME; this needs about 700 MB."
ok "${DISK_MB} MB free on disk"

RAM_MB=$(awk '/^MemTotal:/ {printf "%d", $2/1024}' /proc/meminfo 2>/dev/null || echo 0)
AVAIL_MB=$(awk '/^MemAvailable:/ {printf "%d", $2/1024}' /proc/meminfo 2>/dev/null || echo 0)
if [ "$AVAIL_MB" -gt 0 ] && [ "$AVAIL_MB" -lt 250 ]; then
  warn "${AVAIL_MB} MB of RAM available (of ${RAM_MB} MB). The agent needs about 190 MB"
  warn "while answering. It will work if you have swap, but it will page to disk."
else
  ok "${AVAIL_MB} MB of RAM available"
fi

# A port already in use is worth catching now: systemd would otherwise restart the
# service forever against a port it can never have.
if command -v ss >/dev/null && ss -ltn 2>/dev/null | grep -q ":${PORT}\b"; then
  die "Port ${PORT} is already in use. Set KITE_GATEWAY_PORT to something else."
fi
ok "port ${PORT} is free"

# ⚠️ Raising your own OOM score is unprivileged; lowering it is not. This makes the
# installer, and later the agent, the kernel's first choice under memory pressure
# rather than whatever else the machine is for.
echo 800 > /proc/self/oom_score_adj 2>/dev/null || true

# ------------------------------------------------------------------- the key

if [ -z "${OPENROUTER_API_KEY:-}" ] && [ ! -f "$HOME/.hermes/.env" ]; then
  say "Your OpenRouter API key"
  echo "  The agent needs one to reach a model. Get it at https://openrouter.ai/keys"
  echo "  Leave blank to fill in ~/.hermes/.env yourself later."
  # -s so the key is not echoed and does not linger in the scrollback.
  read -r -s -p "  Key (hidden): " OPENROUTER_API_KEY </dev/tty || true
  echo
fi

# ⚠️ One provider is not enough, and the failure is delayed.
#
# This script used to finish with a single OpenRouter key and say nothing about
# it. That works until the free tier runs out — and then every message fails
# with "rate limit exceeded", the model picker offers nothing that answers, and
# the agent looks broken rather than out of allowance. It cost a whole testing
# session to work out that the machine had only ever had one provider.
#
# Nous Portal is not a key and cannot arrive in an .env: it is an OAuth login.
# `--no-browser` prints a URL to open on any device, so a headless box can do it.
# ⚠️ Always the venv binary, never a bare `hermes`.
#
# This script installs into a virtualenv, so `hermes` is not on anyone's PATH —
# and every hint that said otherwise failed on the machine this script had just
# built. Three round trips were spent discovering that from the outside.
hermes_bin() { printf '%s' "$BASE/hermes-agent/venv/bin/hermes"; }

offer_nous_login() {
  [ -x "$(hermes_bin)" ] || return 0
  if [ -f "$HOME/.hermes/shared/nous_auth.json" ]; then
    ok "Nous Portal is already logged in here"
    return 0
  fi
  say "A second provider (recommended)"
  echo "  OpenRouter's free models have a daily cap. When it runs out the agent"
  echo "  stops answering, which looks like a fault rather than an allowance."
  echo "  Nous Portal is free to log into and gives this machine a fallback."
  echo
  printf "  Log in to Nous Portal now? [Y/n] "
  read -r answer </dev/tty || answer="n"
  case "${answer:-y}" in
    [Nn]*)
      warn "Skipped. Run this later — from root, since this box may have no sudo:"
      warn "  su - $(id -un) -c '$(hermes_bin) auth add nous --type oauth --no-browser'"
      return 0
      ;;
  esac
  echo "  A URL will be printed — open it on your phone or laptop, then come back."
  # Never fatal: a machine with one working provider is still a working machine.
  # ⚠️ `auth add`, not `portal login`. `hermes portal` is the friendly alias but
  # takes NO options, so `portal login --no-browser` fails with "unrecognized
  # arguments" — on a headless box the flag is the whole point.
  "$(hermes_bin)" auth add nous --type oauth --no-browser </dev/tty \
    || warn "Nous login did not complete. Run it later with the command below."
}
offer_nous_login

# -------------------------------------------------------------------- source

say "Installing the Hermes agent"
mkdir -p "$BASE"; cd "$BASE"

if [ ! -d hermes-agent ]; then
  # A tarball, not a clone: git is not installed on many small servers and cannot
  # always be added. It also skips .git, which is 442 MB of history on a machine
  # that only needs to run the thing.
  curl -fsSL --retry 3 "$SRC_URL" -o hermes.tar.gz || die "Could not download the Hermes source."
  tar xzf hermes.tar.gz || die "The download was not a valid archive."
  mv hermes-agent-main hermes-agent
  rm -f hermes.tar.gz
  ok "source downloaded"
else
  ok "source already present"
fi

cd hermes-agent
[ -d venv ] || python3 -m venv venv
nice -n 19 ./venv/bin/python -m pip install --quiet --upgrade pip 2>/dev/null || true

echo "  installing dependencies (a few minutes on a small box)..."
nice -n 19 ./venv/bin/pip install --quiet --no-cache-dir -e . \
  || die "Dependency install failed. Scroll up for the reason."
# ⚠️ Not a core dependency, and without it the gateway starts, logs "API Server:
# aiohttp not installed", BINDS NOTHING, and keeps running — so it looks healthy
# and nothing can connect to it.
nice -n 19 ./venv/bin/pip install --quiet --no-cache-dir aiohttp \
  || die "Could not install aiohttp, which the API server needs."
./venv/bin/python -c "import hermes_cli" 2>/dev/null || die "The install did not import cleanly."
ok "agent installed ($(du -sh "$BASE" | cut -f1) on disk)"

# --------------------------------------------------------------------- config

mkdir -p "$HOME/.hermes"

if [ ! -f "$HOME/.hermes/.env" ]; then
  umask 077
  KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
  cat > "$HOME/.hermes/.env" <<ENV
OPENROUTER_API_KEY=${OPENROUTER_API_KEY:-PUT_YOUR_KEY_HERE}
API_SERVER_ENABLED=true
# Loopback on purpose. The helper is the only thing that should be reachable, and
# it is the only thing that holds this key.
API_SERVER_HOST=127.0.0.1
API_SERVER_PORT=${PORT}
API_SERVER_KEY=${KEY}
# ⚠️ Not merely the name advertised on /v1/models, despite the setting's own
# description. A conversation with no explicit model lock sends this string
# upstream AS the model id, so the stock value "hermes-agent" reaches the provider
# and returns 400 "hermes-agent is not a valid model ID" — which shows up on the
# phone as a failed first message. It must be a real model slug.
API_SERVER_MODEL_NAME=${MODEL}
ENV
  chmod 600 "$HOME/.hermes/.env"
  ok "wrote ~/.hermes/.env"
else
  ok "~/.hermes/.env already exists, left alone"
fi

if [ ! -f "$HOME/.hermes/config.yaml" ]; then
  umask 077
  # ⚠️ Without a provider pinned, Hermes falls back to Nous Portal, which a fresh
  # server has never logged into — every new conversation then fails with "hermes
  # is not logged into Nous Portal" before it reaches a model. `hermes model` sets
  # this interactively, which is no use in a script.
  cat > "$HOME/.hermes/config.yaml" <<YAML
model:
  provider: "openrouter"
  default: "${MODEL}"
  base_url: "https://openrouter.ai/api/v1"
YAML
  chmod 600 "$HOME/.hermes/config.yaml"
  ok "wrote ~/.hermes/config.yaml"
else
  ok "~/.hermes/config.yaml already exists, left alone"
fi

# --------------------------------------------------------------------- helper

say "Installing Kite Helper"
if [ ! -f "$BASE/kite-helper.py" ]; then
  curl -fsSL --retry 3 "$HELPER_URL" -o "$BASE/kite-helper.py" \
    || die "Could not download kite-helper.py from $HELPER_URL"
fi
python3 -c "import ast,sys; ast.parse(open('$BASE/kite-helper.py').read())" \
  || die "kite-helper.py did not download cleanly."
ok "helper installed"

cat > "$BASE/run-agent.sh" <<'RUN'
#!/bin/bash
# ⚠️ `hermes gateway`, NOT `hermes serve`. Both are HTTP servers and both have an
# /api/sessions, which is exactly what makes the wrong one so costly: serve is the
# desktop dashboard's backend, its /api/sessions is GET-only, and every attempt to
# start a conversation comes back 405 from something that looks perfectly healthy.
set -a; . "$HOME/.hermes/.env"; set +a
echo 800 > /proc/self/oom_score_adj 2>/dev/null || true
cd "$(dirname "$0")/hermes-agent"
exec nice -n 5 ./venv/bin/hermes gateway
RUN
chmod +x "$BASE/run-agent.sh"

cat > "$BASE/run-helper.sh" <<RUN
#!/bin/bash
echo 700 > /proc/self/oom_score_adj 2>/dev/null || true
cd "$BASE"
exec python3 kite-helper.py --gateway 127.0.0.1:${PORT} --port ${HELPER_PORT}
RUN
chmod +x "$BASE/run-helper.sh"
ok "start scripts written"

# ------------------------------------------------------------------- systemd

mkdir -p "$BASE/systemd"
cat > "$BASE/systemd/kite-hermes.service" <<UNIT
[Unit]
Description=Hermes agent (gateway) for Kite
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${USER}
WorkingDirectory=${BASE}/hermes-agent
ExecStart=${BASE}/run-agent.sh
Restart=always
RestartSec=10
OOMScoreAdjust=800
MemoryMax=320M
Nice=5

[Install]
WantedBy=multi-user.target
UNIT

cat > "$BASE/systemd/kite-helper.service" <<UNIT
[Unit]
Description=Kite Helper (pairing proxy in front of the Hermes gateway)
After=kite-hermes.service
Wants=kite-hermes.service

[Service]
Type=simple
User=${USER}
WorkingDirectory=${BASE}
ExecStart=${BASE}/run-helper.sh
Restart=always
RestartSec=10
OOMScoreAdjust=700
MemoryMax=96M
# The helper prints its pairing link at startup and then waits. Under systemd
# stdout is a pipe and therefore block-buffered, so without this the log stays
# empty — which reads exactly like a service that never started.
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
UNIT
ok "systemd units written to $BASE/systemd/"

# --------------------------------------------------------------------- start

say "Starting"

if grep -q "PUT_YOUR_KEY_HERE" "$HOME/.hermes/.env"; then
  warn "No API key set. Put one in ~/.hermes/.env, then run this script again."
  warn "Everything else is installed and ready."
  exit 0
fi

# Say what this machine can actually reach, by name. "Installed successfully"
# while reachable by exactly one rate-limited provider is a half-truth.
say "Providers this machine can use"
PROVIDER_COUNT=0
if ! grep -q "PUT_YOUR_KEY_HERE" "$HOME/.hermes/.env" && grep -q "^OPENROUTER_API_KEY=." "$HOME/.hermes/.env"; then
  echo "  • OpenRouter        (free models are capped daily)"
  PROVIDER_COUNT=$((PROVIDER_COUNT + 1))
fi
if [ -f "$HOME/.hermes/shared/nous_auth.json" ]; then
  echo "  • Nous Portal"
  PROVIDER_COUNT=$((PROVIDER_COUNT + 1))
fi
for pair in "ANTHROPIC_API_KEY:Anthropic" "DEEPSEEK_API_KEY:DeepSeek" "OPENAI_API_KEY:OpenAI"; do
  var="${pair%%:*}"; label="${pair##*:}"
  if grep -q "^${var}=." "$HOME/.hermes/.env" 2>/dev/null; then
    echo "  • ${label}"
    PROVIDER_COUNT=$((PROVIDER_COUNT + 1))
  fi
done
if [ "$PROVIDER_COUNT" -le 1 ]; then
  warn "Only one provider. When its allowance runs out this agent stops"
  warn "answering, and the app will show rate-limit errors rather than a fault."
  warn "Add another — from root, since this box may have no sudo:"
  warn "  su - $(id -un) -c '$(hermes_bin) auth add nous --type oauth --no-browser'"
fi

pkill -f "hermes gateway" 2>/dev/null || true
pkill -f "kite-helper.py" 2>/dev/null || true
sleep 2

setsid nohup "$BASE/run-agent.sh"  > "$BASE/agent.log"  2>&1 < /dev/null &
GATEWAY_KEY=$(grep '^API_SERVER_KEY=' "$HOME/.hermes/.env" | cut -d= -f2-)

for i in $(seq 1 40); do
  sleep 2
  if curl -fsS --max-time 5 -H "Authorization: Bearer $GATEWAY_KEY" \
       "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    ok "agent answering on port ${PORT} (after $((i*2))s)"
    break
  fi
  [ "$i" = 40 ] && {
    echo; tail -20 "$BASE/agent.log" | sed 's/^/    /'
    die "The agent did not come up. Its log is above, and in $BASE/agent.log"
  }
done

setsid nohup "$BASE/run-helper.sh" > "$BASE/helper.log" 2>&1 < /dev/null &
for i in $(seq 1 20); do
  sleep 1
  grep -q "listening" "$BASE/helper.log" 2>/dev/null && break
  [ "$i" = 20 ] && {
    echo; tail -20 "$BASE/helper.log" | sed 's/^/    /'
    die "The helper did not start. Its log is above."
  }
done

# End to end, through the helper, exactly as the phone will: if this passes, the
# proxy, the token swap and the agent are all working together.
PAIR_TOKEN=$(cat "$HOME/.kite-helper/pairing-token" 2>/dev/null || true)
ACTUAL_PORT=$(cat "$HOME/.kite-helper/port" 2>/dev/null || echo "$HELPER_PORT")
CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 \
  -H "Authorization: Bearer ${PAIR_TOKEN}" \
  "http://127.0.0.1:${ACTUAL_PORT}/api/sessions" || echo 000)
[ "$CODE" = "200" ] || {
  echo; tail -10 "$BASE/helper.log" | sed 's/^/    /'
  die "The helper is running but could not reach the agent (HTTP $CODE)."
}
ok "helper reaches the agent end to end"

# --------------------------------------------------------------------- pairing

say "About the model this is set to use"
cat <<'EOF'
  It defaults to a free model so it works as soon as you have a key.

  ⚠️ Every step your agent takes is a separate request — one question that reads a
  few files can be twenty — and a free OpenRouter key has a small daily allowance.
  A day of ordinary use will exhaust it, and you will get:

    429 Rate limit exceeded: free-models-per-day

  Adding 10 credits raises the free allowance to 1000 requests a day; you keep
  using free models. The counter resets at 00:00 UTC.

  Set KITE_DEFAULT_MODEL before running this to choose something else.
EOF

say "Pair your phone"
LINK=$(grep -o 'kite-pair:[^ ]*' "$BASE/helper.log" | head -1)
REACH=$(grep -o 'Reachable at [^ ]*' "$BASE/helper.log" | head -1 | awk '{print $3}')

if grep -q "not a tailnet address" "$BASE/helper.log"; then
  warn "No Tailscale on this machine. Your phone can only reach ${REACH} if it is"
  warn "on the same network — which it never is for a server. Install Tailscale"
  warn "(https://tailscale.com/download) on this box and your phone, then re-run."
fi

echo
echo "  $LINK"
echo
command -v qrencode >/dev/null && qrencode -t ANSIUTF8 "$LINK" \
  || echo "  (install qrencode to print a scannable code here)"

say "To keep it running after a reboot"
cat <<EOF
  These need root, once:

    sudo cp ${BASE}/systemd/kite-hermes.service ${BASE}/systemd/kite-helper.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now kite-hermes kite-helper

  Until then both are running, but only until this machine restarts.
EOF
