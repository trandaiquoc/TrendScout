#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python_bin="$project_dir/Backend/.venv/bin/python"
demo_port="${TREND_SCOUT_DEMO_PORT:-8000}"
if [[ ! "$demo_port" =~ ^[0-9]{1,5}$ ]] || (( 10#$demo_port < 1 || 10#$demo_port > 65535 )); then
  printf '%s\n' 'Cổng demo không hợp lệ.' >&2
  exit 1
fi
if [[ ! -x "$python_bin" ]]; then
  printf '%s\n' 'Backend/.venv chưa sẵn sàng. Hãy cài dependencies theo Backend/README.md.' >&2
  exit 1
fi
if ! "$python_bin" -c 'import socket,sys; s=socket.socket(); s.bind(("127.0.0.1",int(sys.argv[1]))); s.close()' "$demo_port" >/dev/null 2>&1; then
  printf 'Port 127.0.0.1:%s is already in use. Stop the process or set TREND_SCOUT_DEMO_PORT to a free port.\n' "$demo_port" >&2
  exit 1
fi
printf 'Starting local SYNTHETIC demo at http://127.0.0.1:%s/dashboard/\n' "$demo_port"
printf '%s\n' 'The demo uses offline fixtures. Uvicorn reports when startup is complete; press Ctrl+C to stop.'
cd "$project_dir/Backend"
exec "$python_bin" -m uvicorn app.demo_local:create_demo_app --factory --host 127.0.0.1 --port "$demo_port" --workers 1
