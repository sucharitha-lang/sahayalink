#!/usr/bin/env bash
# Run SahayaLink on your own computer:  bash run.sh   then open http://127.0.0.1:8000
set -e
cd "$(dirname "$0")"
pip install -q -r requirements.txt
python3 -m uvicorn app:app --host 127.0.0.1 --port 8000
