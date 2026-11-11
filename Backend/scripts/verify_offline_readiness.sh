#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BACKEND="$ROOT/Backend"
PYTHON="$BACKEND/.venv/bin/python"
GUARD="$BACKEND/scripts/seccomp_exec.py"

if [[ ! -x "$PYTHON" ]]; then
  echo "Missing Backend/.venv/bin/python. Run ./setup.sh first." >&2
  exit 2
fi

run_strict() {
  "$PYTHON" "$GUARD" -- "$@"
}

run_browser_isolated() {
  "$PYTHON" "$GUARD" --allow-unix-sockets -- "$@"
}

echo "[1/10] Verify strict seccomp network isolation"
run_strict "$PYTHON" "$BACKEND/scripts/probe_network_isolation.py"

echo "[2/10] Run complete unittest suite"
(cd "$BACKEND" && run_strict "$PYTHON" -m unittest discover -s tests -v)

echo "[3/10] Run Golden Regression with both hash seeds"
(cd "$BACKEND" && run_strict env PYTHONHASHSEED=1 "$PYTHON" -m unittest discover -s tests -p 'test_golden_contract.py' -v)
(cd "$BACKEND" && run_strict env PYTHONHASHSEED=983 "$PYTHON" -m unittest discover -s tests -p 'test_golden_contract.py' -v)

echo "[4/10] Check installed dependency consistency"
run_strict "$PYTHON" -m pip check

echo "[5/10] Check shell and dashboard JavaScript syntax"
run_strict bash -n "$ROOT/setup.sh" "$ROOT/start.sh" "$BACKEND/scripts/verify_offline_readiness.sh"
while IFS= read -r -d '' script; do
  run_strict node --check "$script"
done < <(find "$ROOT/Frontend/public" -type f -name '*.js' -print0)

echo "[6/10] Check Vercel dashboard bundle against source assets"
ASSET="$BACKEND/app/dashboard_assets.py"
if [[ -e "$ASSET" ]]; then
  echo "Refusing to overwrite existing generated asset: $ASSET" >&2
  exit 2
fi
cleanup_asset() {
  run_strict "$PYTHON" -c 'from pathlib import Path; import sys; Path(sys.argv[1]).unlink(missing_ok=True)' "$ASSET"
}
trap cleanup_asset EXIT
(cd "$BACKEND" && run_strict "$PYTHON" scripts/prepare_vercel.py)
run_strict "$PYTHON" -c 'import runpy,sys; from pathlib import Path; root=Path(sys.argv[1]); bundle=runpy.run_path(str(root/"Backend/app/dashboard_assets.py"))["ASSETS"]; source=root/"Frontend/public"; expected={p.relative_to(source).as_posix():p.read_bytes() for p in source.rglob("*") if p.is_file() and not any(x.startswith(".") for x in p.relative_to(source).parts)}; assert bundle==expected; print(f"Vercel bundle byte parity: {len(bundle)} assets")' "$ROOT"
cleanup_asset
trap - EXIT

echo "[7/10] Run Chrome DOM tests with Internet families blocked and AF_UNIX for Chrome IPC"
(cd "$BACKEND" && run_browser_isolated "$PYTHON" tests/browser_workspace_offline.py)

echo "[8/10] Run Chrome keyboard and mobile viewport tests under the same browser sandbox"
(cd "$BACKEND" && run_browser_isolated "$PYTHON" tests/browser_keyboard_offline.py)

echo "[9/10] Run adapter → API → Chrome dashboard integration with synthetic source responses"
(cd "$BACKEND" && run_browser_isolated "$PYTHON" tests/browser_pipeline_e2e_offline.py)

echo "[10/10] Run bounded synthetic storage/API workload at 1k, 5k, and 10k entities"
(cd "$BACKEND" && run_strict env PYTHONPATH="$BACKEND" "$PYTHON" scripts/benchmark_storage_offline.py)

echo "OFFLINE_READINESS_VERIFICATION=PASS"
