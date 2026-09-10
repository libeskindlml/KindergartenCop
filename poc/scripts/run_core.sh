#!/usr/bin/env bash
# מריץ את שירות הליבה (FastAPI). ודאו שיצרתם .env מתוך .env.example
# והתקנתם תלויות: pip install -r requirements.txt --break-system-packages
set -euo pipefail
cd "$(dirname "$0")/.."
uvicorn app.main:app --host "${CORE_SERVICE_HOST:-0.0.0.0}" --port "${CORE_SERVICE_PORT:-8000}" --reload
