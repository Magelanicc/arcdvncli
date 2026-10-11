#!/usr/bin/env bash
set -e
if ! command -v pipx >/dev/null 2>&1; then
  echo "pipx belum terpasang. Lihat https://pipx.pypa.io"; exit 1
fi
pipx install --force .
echo "Selesai! Buka terminal baru lalu ketik: arcdvncli"