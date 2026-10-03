#!/usr/bin/env bash
# ---------------------------------------------------------------------------
#  Run RadarForge straight from the downloaded folder, without installing it.
#  Usage:  bash run.sh [options]        (e.g.  bash run.sh --site KTLX)
#  The first run creates a .venv folder here and downloads the dependencies.
# ---------------------------------------------------------------------------
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"          # the program files live one folder up from this Linux folder
VENV="$HERE/.venv"

if [ ! -f "$ROOT/radarforge/__init__.py" ]; then
  echo "The RadarForge program files were not found next to this Linux folder – extract the whole download first." >&2
  exit 1
fi

if [ ! -x "$VENV/bin/python" ]; then
  PY=""
  for cand in "${PYTHON:-}" python3 python3.14 python3.13 python3.12 python3.11 python3.10; do
    [ -n "$cand" ] || continue
    if command -v "$cand" >/dev/null 2>&1 && "$cand" "$ROOT/scripts/check_python.py" >/dev/null 2>&1; then
      PY="$cand"; break
    fi
  done
  if [ -z "$PY" ]; then
    echo "Python 3.10 or newer was not found." >&2
    echo "  Fedora / Nobara: sudo dnf install python3    Ubuntu / Debian: sudo apt install python3 python3-venv" >&2
    exit 1
  fi
  echo "First run: setting up $VENV (a few minutes)…"
  "$PY" -m venv "$VENV"
  "$VENV/bin/python" -m pip install --quiet --upgrade pip wheel
  "$VENV/bin/python" -m pip install -r "$ROOT/requirements.txt"
fi
# a newer version may need packages the existing environment doesn't have yet
"$VENV/bin/python" -c "import h5py, imageio_ffmpeg" 2>/dev/null || \
  "$VENV/bin/python" -m pip install --quiet -r "$ROOT/requirements.txt"
# run the program files from the download without installing them
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
exec "$VENV/bin/python" -m radarforge "$@"
