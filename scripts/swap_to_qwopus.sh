#!/usr/bin/env bash
#
# swap_to_qwopus.sh — restore the production qwopus llama-server.
#
# Inverse of swap_to_a3b.sh. Kills any running llama-server (presumably
# A3B from the test session) and brings qwopus back up at full
# 262144-token context. After this runs, Nyx Telegram Relay and other
# services pointed at localhost:8080 will work normally again.
#
# Mirrors the original invocation captured at session start:
#   llama-server -m Qwen3.5-27B-Opus-Distilled-v2-Q4_K_M.gguf
#                --host 0.0.0.0 --port 8080
#                -ngl 99 -c 262144 -ctk q8_0 -ctv q8_0 -fa on
#                --temp 0.7

set -euo pipefail

QWOPUS_MODEL="/home/lycaon/models/Qwen3.5-27B-Opus-Distilled-v2-Q4_K_M.gguf"
PORT=8080
CONTEXT=262144
LOG_FILE="/tmp/llama-server-qwopus.log"
# llama-server needs its shared libs from the build dir
LLAMA_LIB_DIR="/home/lycaon/dev/tools/llama.cpp/build/bin"
export LD_LIBRARY_PATH="$LLAMA_LIB_DIR:${LD_LIBRARY_PATH:-}"

if [[ ! -f "$QWOPUS_MODEL" ]]; then
  echo "ERROR: qwopus model not found at $QWOPUS_MODEL"
  exit 1
fi

PIDS=$(pgrep -f "llama-server" || true)
if [[ -n "$PIDS" ]]; then
  echo "Killing running llama-server PIDs: $PIDS"
  kill -TERM $PIDS || true
  for i in $(seq 1 10); do
    if ! pgrep -f "llama-server" >/dev/null; then
      break
    fi
    sleep 1
  done
  if pgrep -f "llama-server" >/dev/null; then
    pkill -KILL -f "llama-server" || true
    sleep 1
  fi
fi

echo "Starting qwopus server on port $PORT with context $CONTEXT…"

nohup llama-server \
  -m "$QWOPUS_MODEL" \
  --host 0.0.0.0 \
  --port "$PORT" \
  -ngl 99 \
  -c "$CONTEXT" \
  -ctk q8_0 \
  -ctv q8_0 \
  -fa on \
  --temp 0.7 \
  > "$LOG_FILE" 2>&1 &

SERVER_PID=$!
echo "Spawned llama-server PID: $SERVER_PID"

echo -n "Waiting for /health"
for i in $(seq 1 60); do
  if curl -s --max-time 1 http://localhost:$PORT/health 2>/dev/null | grep -q '"status":"ok"'; then
    echo " ✓"
    echo "qwopus restored at http://localhost:$PORT (context $CONTEXT)"
    echo "Nyx + ChetGPT can resume normal operation."
    exit 0
  fi
  echo -n "."
  sleep 1
done

echo
echo "ERROR: qwopus did not become healthy within 60s"
echo "Check $LOG_FILE for details"
exit 1
