#!/bin/bash
# Install Hermes headless on a small server and run it for Kite.
#
# Written after doing it by hand on a 893 MB VPS that was already running a
# trading stack. See README.md for what each line is defending against.
set -euo pipefail
BASE="${KITE_HERMES_BASE:-$HOME/kite-hermes}"
PORT="${KITE_GATEWAY_PORT:-8642}"

# ⚠️ Make this tree the OOM killer's first choice. Raising your own score is
# unprivileged; lowering it is not. Anything else on the box keeps its default,
# so under memory pressure the agent dies rather than what you actually run.
echo 800 > /proc/self/oom_score_adj 2>/dev/null || true

mkdir -p "$BASE"; cd "$BASE"
if [ ! -d hermes-agent ]; then
  echo "== fetching source =="
  # A tarball, not a clone: git may not be installed and cannot always be, and
  # this skips .git — 442 MB of history nobody needs to run the thing.
  curl -fsSL --retry 3 \
    https://github.com/NousResearch/hermes-agent/archive/refs/heads/main.tar.gz -o h.tar.gz
  tar xzf h.tar.gz && mv hermes-agent-main hermes-agent && rm -f h.tar.gz
fi

cd hermes-agent
[ -d venv ] || python3 -m venv venv
echo "== installing =="
nice -n 19 ./venv/bin/pip install --quiet --no-cache-dir -e .
# ⚠️ Not a core dependency, and without it the gateway starts, logs "API Server:
# aiohttp not installed", binds NOTHING, and does not exit — so it looks healthy
# and nothing can connect.
nice -n 19 ./venv/bin/pip install --quiet --no-cache-dir aiohttp

if [ ! -f "$HOME/.hermes/.env" ]; then
  mkdir -p "$HOME/.hermes"; umask 077
  KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
  cat > "$HOME/.hermes/.env" <<ENV
OPENROUTER_API_KEY=PUT_YOUR_KEY_HERE
API_SERVER_ENABLED=true
API_SERVER_HOST=127.0.0.1
API_SERVER_PORT=${PORT}
API_SERVER_KEY=${KEY}
ENV
  chmod 600 "$HOME/.hermes/.env"
  echo "  wrote ~/.hermes/.env — put your OpenRouter key in it before starting"
fi

cat > "$BASE/run-agent.sh" <<'RUN'
#!/bin/bash
# ⚠️ `hermes gateway`, NOT `hermes_cli.main serve`. Both are HTTP servers and
# both have an /api/sessions, which is what makes picking the wrong one so
# confusing: serve's is GET-only and has no chat endpoints, so Kite gets 405 on
# every attempt to start a conversation.
set -a; . "$HOME/.hermes/.env"; set +a
echo 800 > /proc/self/oom_score_adj 2>/dev/null || true
cd "$(dirname "$0")/hermes-agent"
exec nice -n 5 ./venv/bin/hermes gateway
RUN
chmod +x "$BASE/run-agent.sh"

echo
echo "Done. Next:"
echo "  1. put your OpenRouter key in ~/.hermes/.env"
echo "  2. $BASE/run-agent.sh          (the agent)"
echo "  3. python3 kite-helper.py --gateway 127.0.0.1:${PORT}   (what the phone talks to)"
