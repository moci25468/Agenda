#!/bin/bash
# 本地启动：./run.sh [port]
cd "$(dirname "$0")"
PORT="${1:-8300}"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
fi
exec .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port "$PORT"