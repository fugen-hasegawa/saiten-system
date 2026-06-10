#!/bin/bash
# 自動採点システム起動スクリプト
# 終了: Ctrl+C

cd "$(dirname "$0")"
source ../.venv/bin/activate
kill $(lsof -ti :8000) 2>/dev/null
uvicorn backend.main:app --host 127.0.0.1 --port 8000
