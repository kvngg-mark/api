#!/usr/bin/env bash
# Installs ffmpeg + redis (if missing) and the npm deps. Safe to re-run.
set -e
need=()
command -v ffmpeg >/dev/null 2>&1 || need+=(ffmpeg)
command -v redis-server >/dev/null 2>&1 || need+=(redis-server)
if [ ${#need[@]} -gt 0 ]; then
  echo "Installing: ${need[*]}"
  sudo apt-get update -y
  sudo apt-get install -y "${need[@]}"
fi
npm install
echo "Setup done. Run: npm start"
