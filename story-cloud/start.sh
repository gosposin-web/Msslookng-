#!/bin/sh
set -eu
install -d -m 700 -o story -g story "$DATA_DIR"
exec gosu story uvicorn app.main:create_app --factory --host 0.0.0.0 --port "${PORT:-8000}" --workers 1 --proxy-headers --forwarded-allow-ips '*' --limit-concurrency 16 --timeout-graceful-shutdown 20
