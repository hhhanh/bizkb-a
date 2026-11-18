#!/usr/bin/env bash
# Khởi động demo sống: giao diện + engine thật (rdflib + pySHACL)
set -e
pip install -q -r requirements.txt
echo "→ http://localhost:${PORT:-8080}"
exec uvicorn bizkb_a.live:app --host 0.0.0.0 --port "${PORT:-8080}"
