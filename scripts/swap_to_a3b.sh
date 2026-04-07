#!/usr/bin/env bash
#
# swap_to_a3b.sh — take down the qwopus llama-server and bring up
# Qwen3.5-35B-A3B at 50K context for compaction stress testing.
#
# WARNING: This kills the running llama-server on port 8080. Several
# user services depend on that server (Nyx Telegram Relay, ChetGPT,
# any other tools pointed at localhost:8080). DO NOT RUN THIS WHILE
# anyone is actively chatting with those bots — it will fail their
# in-flight requests.
#
# To restore: run scripts/swap_to_qwopus.sh
#
# What this does:
#   1. Find any running llama-server process
#   2. SIGTERM it (gives 10s for graceful shutdown)
#   3. Spawn the A3B server in the background with -c 50000
#   4. Wait for /health to come back ok
#   5. Print confirmation
#
# The A3B server uses the SAME port (8080) so existing client config
# (chat.json, profiles) doesn't need to change to point at the test
# instance — the model name in the profile changes, but the URL is
# the same.

set -euo pipefail

A3B_MODEL="/home/lycaon/models/Qwen3.5-35B-A3B-UD-Q4_K_XL.gguf"
A3B_MMPROJ="/home/lycaon/models/mmproj-Qwen3.5-35B-A3B-F16.gguf"  # not used here, just noting
PORT=8080
CONTEXT=50000
LOG_FILE="/tmp/llama-server-a3b.log"

if [[ ! -f "$A3B_MODEL" ]]; then
  echo "ERROR: A3B model not found at $A3B_MODEL"
  exit 1
fi

# Find running llama-server
PIDS=$(pgrep -f "llama-server" || true)
if [[ -n "$PIDS" ]]; then
  echo "Killing running llama-server PIDs: $PIDS"
  kill -TERM $PIDS || true
  # Wait up to 10s for graceful shutdown
  for i in $(seq 1 10); do
    if ! pgrep -f "llama-server" >/dev/null; then
      break
    fi
    sleep 1
  done
  # Force-kill if still running
  if pgrep -f "llama-server" >/dev/null; then
    echo "Force-killing remaining llama-server"
    pkill -KILL -f "llama-server" || true
    sleep 1
  fi
fi

echo "Starting A3B server on port $PORT with context $CONTEXT…"
echo "Model: $A3B_MODEL"
echo "Log: $LOG_FILE"

# Use nohup so it survives terminal close. Output to a file we can tail.
nohup llama-server \
  -m "$A3B_MODEL" \
  --host 0.0.0.0 \
  --port "$PORT" \
  -ngl 99 \
  -c "$CONTEXT" \
  -ctk q8_0 \
  -ctv q8_0 \
  -fa on \
  --temp 0.5 \
  > "$LOG_FILE" 2>&1 &

SERVER_PID=$!
echo "Spawned llama-server PID: $SERVER_PID"

# Wait for /health to come back
echo -n "Waiting for /health"
for i in $(seq 1 60); do
  if curl -s --max-time 1 http://localhost:$PORT/health 2>/dev/null | grep -q '"status":"ok"'; then
    echo " ✓"
    echo "A3B server up at http://localhost:$PORT"
    echo "Run 'tail -f $LOG_FILE' to watch server output."
    echo
    echo "To restore qwopus: scripts/swap_to_qwopus.sh"
    exit 0
  fi
  echo -n "."
  sleep 1
done

echo
echo "ERROR: A3B server did not become healthy within 60s"
echo "Check $LOG_FILE for details"
exit 1
