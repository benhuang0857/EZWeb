#!/bin/sh
cd "$(dirname "$0")"

PORT="${EZWEB_PORT:-3000}"

if [ -x .venv/bin/python ]; then
    exec .venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port "$PORT"
elif command -v py >/dev/null 2>&1; then
    exec py -3 -m uvicorn main:app --host 0.0.0.0 --port "$PORT"
else
    exec python3 -m uvicorn main:app --host 0.0.0.0 --port "$PORT"
fi
