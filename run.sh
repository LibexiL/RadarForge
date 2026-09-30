#!/usr/bin/env bash
# ---------------------------------------------------------------------------
#  Run RadarForge straight from this folder, without installing it.
#  Usage:  bash run.sh [options]        (e.g.  bash run.sh --site KTLX)
#  The first run creates ./.venv and downloads the dependencies.
# ---------------------------------------------------------------------------
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$HERE/.venv"

if [ ! -x "$VENV/bin/python" ]; then
  PY=""
  for cand in "${PYTHON:-}" python3 python3.14 python3.13 python3.12 python3.11 python3.10; do
    [ -n "$cand" ] || continue
    if command -v "$cand" >/dev/null 2>&1 && "$cand" "$HERE/scripts/check_python.py" >/dev/null 2>&1; then
      PY="$cand"; break
    fi
  done
  [ -n "$PY" ] || { echo "Python 3.10 or newer was not found (see README.md)." >&2; exit 1; }
  echo "First run: setting up $VENV (a few minutes)…"
  "$PY" -m venv "$VENV"
  "$VENV/bin/python" -m pip install --quiet --upgrade pip wheel
  "$VENV/bin/python" -m pip install -r "$HERE/requirements.txt"
fi
cd "$HERE"
exec "$VENV/bin/python" -m radarforge "$@"
