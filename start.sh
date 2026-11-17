#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python_bin="$project_dir/Backend/.venv/bin/python"
port="${TREND_SCOUT_DEMO_PORT:-8000}"

if [[ "${1:-}" == "--synthetic" ]]; then
  shift
  if (($#)); then
    printf '%s\n' 'Usage: bash start.sh [--synthetic] | bash start.sh [--youtube-owner-manual-live [--youtube-owner-force-fresh-test]]' >&2
    exit 2
  fi
  exec bash "$project_dir/demo.sh"
fi
youtube_manual_live=false
youtube_force_fresh_test=false
while (($#)); do
  case "$1" in
    --youtube-owner-manual-live)
      [[ "$youtube_manual_live" == false ]] || { printf '%s\n' 'Duplicate manual LIVE flag.' >&2; exit 2; }
      youtube_manual_live=true
      ;;
    --youtube-owner-force-fresh-test)
      [[ "$youtube_force_fresh_test" == false ]] || { printf '%s\n' 'Duplicate fresh-test flag.' >&2; exit 2; }
      youtube_force_fresh_test=true
      ;;
    *)
      printf '%s\n' 'Usage: bash start.sh [--synthetic] | bash start.sh [--youtube-owner-manual-live [--youtube-owner-force-fresh-test]]' >&2
      exit 2
      ;;
  esac
  shift
done
if [[ "$youtube_force_fresh_test" == true && "$youtube_manual_live" != true ]]; then
  printf '%s\n' '--youtube-owner-force-fresh-test requires --youtube-owner-manual-live.' >&2
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

printf 'Starting the local multi-source LIVE web application on http://127.0.0.1:%s/dashboard/\n' "$port"
printf '%s\n' 'Local only; one worker. Press Ctrl+C to stop.'
cd "$project_dir/Backend"
if [[ "$youtube_manual_live" == true ]]; then
  if [[ "$youtube_force_fresh_test" == true ]]; then
    exec env TREND_SCOUT_DEMO_PORT="$port" "$python_bin" -m scripts.run_google_live_local \
      --port "$port" --youtube-owner-manual-live --youtube-owner-force-fresh-test
  fi
  exec env TREND_SCOUT_DEMO_PORT="$port" "$python_bin" -m scripts.run_google_live_local \
    --port "$port" --youtube-owner-manual-live
fi
exec env TREND_SCOUT_DEMO_PORT="$port" "$python_bin" -m scripts.run_google_live_local --port "$port"
