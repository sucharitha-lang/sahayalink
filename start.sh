#!/usr/bin/env bash
# Start command for Render (and any host). The database is created automatically on first start.
set -e
cd "$(dirname "$0")"
exec python3 -m uvicorn app:app --host 0.0.0.0 --port "${PORT:-8000}"
