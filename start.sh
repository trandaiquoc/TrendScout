#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python_bin="$project_dir/Backend/.venv/bin/python"

if [[ ! -x "$python_bin" ]]; then
  printf '%s\n' 'Lỗi: chưa có Backend/.venv. Hãy chạy ./setup.sh trước.' >&2
  exit 1
fi
if ! "$python_bin" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 12))'; then
  printf '%s\n' 'Lỗi: Backend/.venv phải dùng Python 3.12. Hãy kiểm tra môi trường và chạy lại ./setup.sh.' >&2
  exit 1
fi
if ! "$python_bin" -c 'import fastapi, uvicorn, firebase_admin, google.cloud.firestore, httpx' >/dev/null 2>&1 || ! "$python_bin" -m pip check >/dev/null 2>&1; then
  printf '%s\n' 'Lỗi: dependency thiếu hoặc không tương thích. Hãy chạy ./setup.sh.' >&2
  exit 1
fi

# Read only literal values for the two supported keys. Never execute .env as shell.
# Explicit environment variables take precedence over the local file.
if [[ -f "$project_dir/.env" ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
    if [[ "$line" =~ ^[[:space:]]*(GOOGLE_CLOUD_PROJECT|TREND_SCOUT_OPERATOR_TOKEN)[[:space:]]*=(.*)$ ]]; then
      key="${BASH_REMATCH[1]}"
      value="${BASH_REMATCH[2]}"
      value="${value#"${value%%[![:space:]]*}"}"
      value="${value%"${value##*[![:space:]]}"}"
      if [[ "$value" == \"*\" && ${#value} -ge 2 ]] || [[ "$value" == \'*\' && ${#value} -ge 2 ]]; then
        value="${value:1:${#value}-2}"
      fi
      if [[ "$key" == GOOGLE_CLOUD_PROJECT && -z "${GOOGLE_CLOUD_PROJECT:-}" ]]; then
        export GOOGLE_CLOUD_PROJECT="$value"
      elif [[ "$key" == TREND_SCOUT_OPERATOR_TOKEN && -z "${TREND_SCOUT_OPERATOR_TOKEN:-}" ]]; then
        export TREND_SCOUT_OPERATOR_TOKEN="$value"
      fi
    fi
  done < "$project_dir/.env"
fi
if [[ -z "${GOOGLE_CLOUD_PROJECT:-}" ]]; then
  printf '%s\n' 'Lỗi: cần GOOGLE_CLOUD_PROJECT cho runtime cloud. Để chạy demo synthetic local, dùng bash demo.sh.' >&2
  exit 1
fi
export GOOGLE_CLOUD_PROJECT
operator_token="${TREND_SCOUT_OPERATOR_TOKEN:-}"
if [[ ${#operator_token} -lt 24 ]]; then
  printf '%s\n' 'Lỗi: cần TREND_SCOUT_OPERATOR_TOKEN tối thiểu 24 ký tự trong environment hoặc .env.' >&2
  exit 1
fi
if ! "$python_bin" -c 'import google.auth; google.auth.default()' >/dev/null 2>&1; then
  printf '%s\n' 'Lỗi: chưa tìm thấy Application Default Credentials. Hãy cấu hình ADC trên Ubuntu rồi chạy lại.' >&2
  exit 1
fi
if ! "$python_bin" -c 'import socket,sys; s=socket.socket(); sys.exit(0 if s.connect_ex(("127.0.0.1",8000)) else 1)' >/dev/null 2>&1; then
  printf '%s\n' 'Lỗi: cổng 127.0.0.1:8000 đang được sử dụng.' >&2
  exit 1
fi

cd "$project_dir/Backend"
"$python_bin" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 &
server_pid=$!
cleanup() {
  trap - EXIT
  if kill -0 "$server_pid" 2>/dev/null; then
    kill -TERM "$server_pid" 2>/dev/null || true
  fi
  wait "$server_pid" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

ready=false
for ((attempt=0; attempt<100; attempt++)); do
  if ! kill -0 "$server_pid" 2>/dev/null; then
    printf '%s\n' 'Lỗi: backend dừng trước khi khởi động xong. Kiểm tra thông báo Uvicorn ở trên.' >&2
    exit 1
  fi
  if "$python_bin" -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/api/health", timeout=0.5).read()' >/dev/null 2>&1; then
    ready=true
    break
  fi
  sleep 0.2
done
if [[ "$ready" != true ]]; then
  printf '%s\n' 'Lỗi: backend chưa sẵn sàng trong thời gian cho phép.' >&2
  exit 1
fi
printf '\n%s\n%s\n' 'TrendScout đã khởi động: http://127.0.0.1:8000/dashboard/' 'Nhấn Ctrl+C để dừng.'
wait "$server_pid"
