#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
venv_dir="$project_dir/Backend/.venv"

if ! command -v python3.12 >/dev/null 2>&1; then
  printf '%s\n' 'Lỗi: cần Python 3.12. Hãy cài Python 3.12 và python3.12-venv trên Ubuntu.' >&2
  exit 1
fi
if [[ ! -x "$venv_dir/bin/python" ]]; then
  if ! python3.12 -m venv "$venv_dir"; then
    printf '%s\n' 'Không tạo được môi trường ảo. Hãy cài gói python3.12-venv phù hợp và chạy lại ./setup.sh.' >&2
    exit 1
  fi
fi
if ! "$venv_dir/bin/python" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 12))'; then
  printf '%s\n' 'Backend/.venv không dùng Python 3.12. Hãy kiểm tra môi trường này trước khi chạy lại.' >&2
  exit 1
fi
if ! "$venv_dir/bin/python" -m pip --version >/dev/null 2>&1; then
  printf '%s\n' 'Lỗi: Backend/.venv không có pip. Hãy cài python3.12-venv và tạo lại môi trường ảo.' >&2
  exit 1
fi
printf '%s\n' 'Đang cài dependency vào Backend/.venv...'
"$venv_dir/bin/python" -m pip install -r "$project_dir/Backend/requirements.txt"
"$venv_dir/bin/python" -m pip check

# Create a private local token only when no configuration already exists.
if [[ ! -e "$project_dir/.env" && -z "${TREND_SCOUT_OPERATOR_TOKEN:-}" ]]; then
  "$venv_dir/bin/python" - "$project_dir/.env" <<'PY'
import os
import secrets
import sys

path = sys.argv[1]
try:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    pass
else:
    with os.fdopen(fd, "w", encoding="utf-8") as config:
        config.write("GOOGLE_CLOUD_PROJECT=trendscout-192e4\n")
        config.write("TREND_SCOUT_OPERATOR_TOKEN=" + secrets.token_urlsafe(32) + "\n")
    print("Đã tạo .env local với quyền 600 và token vận hành. Mở file local để nhập token vào dashboard khi cần.")
PY
fi
printf '%s\n' 'Thiết lập hoàn tất. Chạy ./start.sh để mở TrendScout.'
