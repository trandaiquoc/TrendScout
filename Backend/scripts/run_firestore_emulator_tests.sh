#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BACKEND="$ROOT/Backend"
PYTHON="$BACKEND/.venv/bin/python"
SYNTHETIC_PROJECT="demo-trendscout-p3s1"
EMULATOR_HOST="127.0.0.1:8080"
FIREBASE_BIN="$(command -v firebase || true)"

if [[ ! -x "$PYTHON" ]]; then
  echo "Missing Backend/.venv/bin/python; run ./setup.sh first." >&2
  exit 2
fi
if ! rg --files "$BACKEND/emulator_tests" 2>/dev/null | rg -q '/test_.*\.py$'; then
  echo "EMULATOR_NOT_VERIFIED: no Firestore integration test suite is present." >&2
  exit 4
fi
if [[ -z "$FIREBASE_BIN" ]]; then
  echo "EMULATOR_NOT_VERIFIED: Firebase CLI is not installed; no downloads were attempted." >&2
  exit 3
fi

EMULATOR_CACHE="$HOME/.cache/firebase/emulators"
if [[ ! -d "$EMULATOR_CACHE" ]] || ! find "$EMULATOR_CACHE" -maxdepth 1 -type f -name 'cloud-firestore-emulator*.jar' -print -quit | rg -q .; then
  echo "EMULATOR_NOT_VERIFIED: Firestore Emulator JAR is not cached; refusing automatic download." >&2
  exit 3
fi
if ! command -v bwrap >/dev/null 2>&1; then
  echo "Missing bubblewrap; outbound network isolation cannot be guaranteed." >&2
  exit 2
fi
if ! command -v timeout >/dev/null 2>&1; then
  echo "Missing timeout; bounded emulator execution cannot be guaranteed." >&2
  exit 2
fi

TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/trendscout-firestore-emulator.XXXXXX")"
cleanup() { rm -rf -- "$TEMP_DIR"; }
trap cleanup EXIT INT TERM
TEMP_HOME="$TEMP_DIR/home"
mkdir -p "$TEMP_HOME/.cache/firebase"
CONFIG="$TEMP_DIR/firebase.json"
"$PYTHON" - "$CONFIG" <<'PY'
import json
from pathlib import Path
import sys

Path(sys.argv[1]).write_text(json.dumps({
    "emulators": {
        "singleProjectMode": True,
        "firestore": {"host": "127.0.0.1", "port": 8080},
        "ui": {"enabled": False},
    }
}), encoding="utf-8")
PY

TEST_COMMAND="env PYTHONPATH='$BACKEND' '$PYTHON' -m unittest discover -s '$BACKEND/emulator_tests' -p 'test_*.py' -v"

echo "Starting Firestore Emulator and its test process in an isolated network namespace."
echo "Project: $SYNTHETIC_PROJECT; host: $EMULATOR_HOST; no cloud credentials are passed."

cd "$TEMP_DIR"
echo "Proving loopback-only IP sockets and blocked AF_UNIX before emulator startup."
bwrap --unshare-net --die-with-parent --dev-bind / / -- \
  /usr/bin/env -i \
    PATH="$BACKEND/.venv/bin:/usr/bin:/bin" \
    PYTHONPATH="$BACKEND" \
    "$PYTHON" "$BACKEND/scripts/seccomp_exec.py" --allow-ip-sockets -- \
      "$PYTHON" "$BACKEND/scripts/probe_firestore_emulator_network.py"

timeout --signal=INT --kill-after=15s 10m \
  bwrap --unshare-net --die-with-parent --dev-bind / / \
    --ro-bind "$EMULATOR_CACHE" "$TEMP_HOME/.cache/firebase/emulators" -- \
  /usr/bin/env -i \
    PATH="$PATH" HOME="$TEMP_HOME" \
    FIRESTORE_EMULATOR_HOST="$EMULATOR_HOST" \
    TREND_SCOUT_EMULATOR_PROJECT_ID="$SYNTHETIC_PROJECT" \
    GOOGLE_CLOUD_PROJECT="$SYNTHETIC_PROJECT" \
    GCLOUD_PROJECT="$SYNTHETIC_PROJECT" \
    "$PYTHON" "$BACKEND/scripts/seccomp_exec.py" --allow-ip-sockets -- \
    "$FIREBASE_BIN" emulators:exec \
      --config "$CONFIG" \
      --only firestore \
      --project "$SYNTHETIC_PROJECT" \
      --non-interactive \
      "$TEST_COMMAND"

echo "FIRESTORE_EMULATOR_ACCEPTANCE=PASS"
