#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python_bin="$project_dir/Backend/.venv/bin/python"
port="${TREND_SCOUT_DEMO_PORT:-8000}"

if [[ "${1:-}" == "--synthetic" ]]; then
  shift
  if (($#)); then
    printf '%s\n' 'Usage: bash start.sh [--synthetic]' >&2
    exit 2
  fi
  exec bash "$project_dir/demo.sh"
fi
if (($#)); then
  printf '%s\n' 'Usage: bash start.sh [--synthetic]' >&2
  exit 2
fi
if [[ ! "$port" =~ ^[0-9]{1,5}$ ]] || (( 10#$port < 1 || 10#$port > 65535 )); then
  printf '%s\n' 'TREND_SCOUT_DEMO_PORT must be an integer from 1 to 65535.' >&2
  exit 2
fi
if [[ ! -x "$python_bin" ]]; then
  printf '%s\n' 'Backend/.venv is missing. Install the Backend dependencies first; see Backend/README.md.' >&2
  exit 1
fi
if ! "$python_bin" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 12))'; then
  printf '%s\n' 'Backend/.venv must use Python 3.12; see Backend/README.md.' >&2
  exit 1
fi
if ! "$python_bin" -c 'import fastapi, httpx, uvicorn' >/dev/null 2>&1; then
  printf '%s\n' 'FastAPI, httpx, or Uvicorn is missing from Backend/.venv. Install the Backend dependencies first.' >&2
  exit 1
fi
if ! "$python_bin" -c 'import socket,sys; s=socket.socket(); s.bind(("127.0.0.1",int(sys.argv[1]))); s.close()' "$port" >/dev/null 2>&1; then
  printf 'Port 127.0.0.1:%s is already in use. Stop the process or set TREND_SCOUT_DEMO_PORT to a free port.\n' "$port" >&2
  exit 1
fi

printf 'Starting normal Wikimedia LIVE on http://127.0.0.1:%s/dashboard/\n' "$port"
printf '%s\n' 'Local only; one worker. Press Ctrl+C to stop.'
cd "$project_dir/Backend"
exec env TREND_SCOUT_DEMO_PORT="$port" "$python_bin" -m scripts.run_wikimedia_live_local --port "$port"
