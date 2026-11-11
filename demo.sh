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
printf '%s\n' 'Demo SYNTHETIC, chỉ chạy local; dữ liệu sẽ mất khi dừng.' \
  "Mở http://127.0.0.1:$demo_port/dashboard/ và nhấn Ctrl+C để dừng."
cd "$project_dir/Backend"
exec "$python_bin" -m uvicorn app.demo_local:create_demo_app --factory --host 127.0.0.1 --port "$demo_port" --workers 1
