#!/usr/bin/env bash
# Starts Redis in the background (persisting to ./data/redis) then the server.
set -e
mkdir -p data/redis
if ! redis-cli ping >/dev/null 2>&1; then
  redis-server --daemonize yes --dir "$(pwd)/data/redis" --save "60 1" --appendonly no >/dev/null
  sleep 0.5
fi
if [ "$1" = "--watch" ]; then exec node --watch server.js; fi
exec node server.js
