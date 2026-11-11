#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python_bin="$project_dir/Backend/.venv/bin/python"
if [[ ! -x "$python_bin" ]]; then
  printf '%s\n' 'Backend/.venv chưa sẵn sàng. Hãy cài dependencies theo Backend/README.md.' >&2
  exit 1
fi
printf '%s\n' 'Demo SYNTHETIC, chỉ chạy local; dữ liệu sẽ mất khi dừng.' \
  'Mở http://127.0.0.1:8000/dashboard/ và nhấn Ctrl+C để dừng.'
cd "$project_dir/Backend"
exec "$python_bin" -m uvicorn app.demo_local:create_demo_app --factory --host 127.0.0.1 --port 8000 --workers 1
