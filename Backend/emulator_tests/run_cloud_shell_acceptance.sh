#!/usr/bin/env bash
# Copy-paste runner for guarded Firestore Emulator acceptance in Cloud Shell.
set -Eeuo pipefail
EXPECTED_MAIN="281bd7d7610489f778ddb1618d56491c22eeb1f0"
REPOSITORY="https://github.com/trandaiquoc/TrendScout.git"
SYNTHETIC_PROJECT="demo-trendscout-p3s1"
FIREBASE_TOOLS_VERSION="15.34.2"
STAGE=initialization

if [[ -z "$HOME" || ! -d "$HOME" ]]; then
    printf 'FAIL: HOME must name the Cloud Shell user directory.\n' >&2
    exit 2
fi
LOG_FILE="$HOME/trendscout-firestore-acceptance-$(date -u +%Y%m%dT%H%M%SZ).log"
touch "$LOG_FILE"
chmod 600 "$LOG_FILE"
exec > >(tee -a "$LOG_FILE") 2>&1
RUN_ROOT="$(mktemp -d /tmp/trendscout-firestore-acceptance.XXXXXXXX)"
SAFE_HOME="$RUN_ROOT/home"
SANDBOX_ETC="$RUN_ROOT/sandbox-etc"
mkdir -p "$SAFE_HOME" "$RUN_ROOT/no-gcloud-config" "$SANDBOX_ETC/ssl/certs"

finish() {
    local result=$?
    trap - EXIT ERR
    if (( result == 0 )); then printf '\nRESULT=PASS\n'
    else printf '\nRESULT=FAIL stage=%s exit=%s\n' "$STAGE" "$result"; fi
    printf 'LOG_FILE=%s\n' "$LOG_FILE"
    case "$RUN_ROOT" in
        /tmp/trendscout-firestore-acceptance.*) rm -rf -- "$RUN_ROOT" || printf 'CLEANUP=WARNING temp path remains: %s\n' "$RUN_ROOT" ;;
        *) printf 'CLEANUP=WARNING refused unexpected path: %s\n' "$RUN_ROOT" ;;
    esac
    exit "$result"
}
trap finish EXIT
trap 'result=$?; printf "ERROR stage=%s line=%s exit=%s\n" "$STAGE" "$LINENO" "$result"; exit "$result"' ERR

require_command() {
    local name="$1" path
    path="$(type -P "$name" || true)"
    if [[ -z "$path" ]]; then printf 'FAIL: missing command: %s\n' "$name" >&2; return 1; fi
    printf 'COMMAND_%s=%s\n' "$name" "$path" >&2
    printf '%s\n' "$path"
}
is_system_path() {
    case "$1" in
        /usr|/usr/*|/bin|/bin/*|/sbin|/sbin/*|/lib|/lib/*|/lib64|/lib64/*) return 0 ;;
        *) return 1 ;;
    esac
}
real_path() { "$READLINK_COMMAND" -f -- "$1"; }

STAGE=checking-tools
BWRAP_COMMAND="$(require_command bwrap)"
GIT_COMMAND="$(require_command git)"
PYTHON_COMMAND="$(require_command python3.12)"
NODE_COMMAND="$(require_command node)"
NPM_COMMAND="$(require_command npm)"
JAVA_COMMAND="$(require_command java)"
READLINK_COMMAND="$(require_command readlink)"
SHA256_COMMAND="$(require_command sha256sum)"
CP_COMMAND="$(require_command cp)"
IP_COMMAND="$(require_command ip)"
BWRAP_REAL="$(real_path "$BWRAP_COMMAND")"
GIT_REAL="$(real_path "$GIT_COMMAND")"
PYTHON_REAL="$(real_path "$PYTHON_COMMAND")"
NODE_REAL="$(real_path "$NODE_COMMAND")"
NPM_REAL="$(real_path "$NPM_COMMAND")"
JAVA_REAL="$(real_path "$JAVA_COMMAND")"
IP_REAL="$(real_path "$IP_COMMAND")"
SHA256_REAL="$(real_path "$SHA256_COMMAND")"
JAVA_HOME="$(dirname "$(dirname "$JAVA_REAL")")"
for p in "$BWRAP_REAL" "$GIT_REAL" "$PYTHON_REAL" "$NODE_REAL" "$NPM_REAL" "$JAVA_REAL" "$IP_REAL" "$SHA256_REAL" "$JAVA_HOME"; do
    if ! is_system_path "$p"; then
        printf 'FAIL: refusing to expose runtime outside mounted system paths: %s\n' "$p" >&2
        printf 'Use system-installed Python 3.12, Node/npm, Java 21, Bubblewrap, and iproute2.\n' >&2
        exit 3
    fi
done
BWRAP_VERSION="$("$BWRAP_REAL" --version)"
printf 'BWRAP_VERSION=%s\n' "$BWRAP_VERSION"
if [[ "$BWRAP_VERSION" != *"0.9.0"* ]]; then
    printf 'FAIL: expected Bubblewrap 0.9.0, found %s\n' "$BWRAP_VERSION" >&2
    exit 3
fi
SAFE_PATH="$(dirname "$PYTHON_REAL"):$(dirname "$NODE_REAL"):$(dirname "$NPM_REAL"):$(dirname "$JAVA_REAL"):$(dirname "$IP_REAL"):/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# Minimal /etc: no host home, /root, /run, gcloud config, or ADC is mounted.
"$CP_COMMAND" --dereference /etc/resolv.conf "$SANDBOX_ETC/resolv.conf"
"$CP_COMMAND" --dereference /etc/ssl/certs/ca-certificates.crt "$SANDBOX_ETC/ssl/certs/ca-certificates.crt"
if [[ -r /etc/ssl/openssl.cnf ]]; then "$CP_COMMAND" --dereference /etc/ssl/openssl.cnf "$SANDBOX_ETC/ssl/openssl.cnf"; fi
ln -sfn certs/ca-certificates.crt "$SANDBOX_ETC/ssl/cert.pem"
cat > "$SANDBOX_ETC/hosts" <<'EOF'
127.0.0.1 localhost
::1 localhost ip6-localhost
EOF
cat > "$SANDBOX_ETC/nsswitch.conf" <<'EOF'
passwd: files
group: files
hosts: files dns
networks: files
services: files
protocols: files
EOF
cat > "$SANDBOX_ETC/passwd" <<EOF
root:x:0:0:root:/work/home:/bin/bash
sandbox:x:$(id -u):$(id -g):sandbox:/work/home:/bin/bash
EOF
cat > "$SANDBOX_ETC/group" <<EOF
root:x:0:
sandbox:x:$(id -g):
EOF
printf 'NAME="TrendScout isolated acceptance"\nID=linux\n' > "$SANDBOX_ETC/os-release"

BASE_ARGS=(
    --die-with-parent --new-session --unshare-pid
    --ro-bind /usr /usr
    --ro-bind "$SANDBOX_ETC" /etc
    --proc /proc --dev /dev --tmpfs /tmp
    --dir /work --bind "$RUN_ROOT" /work
)
for p in /bin /sbin /lib /lib64; do
    if [[ -L "$p" ]]; then BASE_ARGS+=(--symlink "$(readlink "$p")" "$p")
    elif [[ -d "$p" ]]; then BASE_ARGS+=(--ro-bind "$p" "$p"); fi
done
COMMON_ENV_ARGS=(
    --clearenv --setenv PATH "$SAFE_PATH"
    --setenv HOME /work/home --setenv TMPDIR /tmp
    --setenv XDG_CONFIG_HOME /work/home/.config --setenv XDG_CACHE_HOME /work/home/.cache
    --setenv CLOUDSDK_CONFIG /work/no-gcloud-config --setenv CI 1
    --setenv PYTHONNOUSERSITE 1 --setenv PYTHONDONTWRITEBYTECODE 1
    --setenv JAVA_HOME "$JAVA_HOME" --setenv FIREBASE_CLI_DISABLE_TELEMETRY 1
    --setenv NPM_CONFIG_USERCONFIG /dev/null --setenv NPM_CONFIG_GLOBALCONFIG /dev/null
    --setenv NPM_CONFIG_UPDATE_NOTIFIER false --setenv PIP_CONFIG_FILE /dev/null
    --setenv GIT_CONFIG_NOSYSTEM 1 --setenv GIT_CONFIG_GLOBAL /dev/null
    --setenv GIT_TERMINAL_PROMPT 0
)

cat > "$RUN_ROOT/prepare.sh" <<'PREPARE'
#!/usr/bin/env bash
set -Eeuo pipefail
python_version="$("$PYTHON_COMMAND" -S -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
printf 'PYTHON_VERSION=%s\n' "$python_version"
[[ "$python_version" == 3.12 ]] || { printf 'FAIL: Python 3.12 required\n' >&2; exit 10; }
java_info="$("$JAVA_COMMAND" -version 2>&1 | head -n 1)"
printf 'JAVA_VERSION=%s\n' "$java_info"
[[ "$java_info" == *'21.'* || "$java_info" == *'version "21"'* ]] || { printf 'FAIL: Java 21 required\n' >&2; exit 11; }
node_version="$(node -p 'process.versions.node')"
printf 'NODE_VERSION=%s\n' "$node_version"
if ! node -e 'const [major, minor] = process.versions.node.split(".").map(Number); process.exit((major === 20 && minor >= 19) || (major === 22 && minor >= 12) || major > 22 ? 0 : 1);'; then
    printf 'FAIL: Firebase CLI requires Node.js 20.19+ or 22.12+ (or a later major release)\n' >&2
    exit 12
fi

printf 'SOURCE_FETCH=START\n'
"$GIT_COMMAND" clone --depth 1 --branch main --single-branch "$REPOSITORY" /work/repo
source_commit="$("$GIT_COMMAND" -C /work/repo rev-parse HEAD)"
printf 'SOURCE_COMMIT=%s\n' "$source_commit"
if [[ "$source_commit" != "$EXPECTED_MAIN" ]]; then
    printf 'FAIL: origin/main changed; expected=%s actual=%s\n' "$EXPECTED_MAIN" "$source_commit" >&2
    exit 13
fi

cd /work/repo/Backend
"$PYTHON_COMMAND" -m venv /work/venv
/work/venv/bin/python -m pip --isolated install --disable-pip-version-check --no-cache-dir --index-url https://pypi.org/simple -r requirements.txt
"$NPM_COMMAND" --userconfig=/dev/null --globalconfig=/dev/null --prefix=/work/node install --no-audit --no-fund --registry=https://registry.npmjs.org "firebase-tools@$FIREBASE_TOOLS_VERSION"
FIREBASE_BIN=/work/node/node_modules/.bin/firebase
cli_version="$("$FIREBASE_BIN" --version)"
printf 'FIREBASE_CLI_VERSION=%s\n' "$cli_version"
[[ "$cli_version" == "$FIREBASE_TOOLS_VERSION" ]] || { printf 'FAIL: Firebase CLI version mismatch\n' >&2; exit 14; }

cd /work/repo/Backend/emulator_tests
"$FIREBASE_BIN" --project "$SYNTHETIC_PROJECT" setup:emulators:firestore
jar_path="$(find /work/home/.cache/firebase/emulators -maxdepth 1 -type f -name 'cloud-firestore-emulator*.jar' -print -quit)"
[[ -n "$jar_path" && -f "$jar_path" ]] || { printf 'FAIL: Firestore Emulator JAR missing\n' >&2; exit 15; }
printf 'FIRESTORE_EMULATOR_JAR=%s\n' "$jar_path"
"$SHA256_COMMAND" "$jar_path"
printf 'PREPARATION=PASS\n'
PREPARE
chmod 700 "$RUN_ROOT/prepare.sh"

cat > "$RUN_ROOT/run_emulator_tests.sh" <<'TESTS'
#!/usr/bin/env bash
set -Eeuo pipefail
export FIRESTORE_EMULATOR_HOST=127.0.0.1:8080
export TREND_SCOUT_EMULATOR_PROJECT_ID=demo-trendscout-p3s1
export GOOGLE_CLOUD_PROJECT=demo-trendscout-p3s1
unset GOOGLE_APPLICATION_CREDENTIALS GCLOUD_PROJECT FIREBASE_TOKEN CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE
cd /work/repo/Backend/emulator_tests
printf 'EMULATOR_PROJECT=%s\n' "$GOOGLE_CLOUD_PROJECT"
printf 'EMULATOR_HOST=%s\n' "$FIRESTORE_EMULATOR_HOST"
printf 'SUITES=test_firestore_transactional,test_firestore_shared_runtime\n'
exec /work/venv/bin/python -m unittest -v test_firestore_transactional test_firestore_shared_runtime
TESTS
chmod 700 "$RUN_ROOT/run_emulator_tests.sh"

cat > "$RUN_ROOT/run_isolated.sh" <<'ISOLATED'
#!/usr/bin/env bash
set -Eeuo pipefail
cd /work/repo/Backend/emulator_tests
ip link set lo up
/work/venv/bin/python - <<'PY'
import errno
import socket
from app.storage.firestore_emulator import _assert_network_namespace_isolated
_assert_network_namespace_isolated()
with open("/proc/net/dev", encoding="ascii") as stream:
    interfaces = sorted(line.split(":", 1)[0].strip() for line in stream.read().splitlines()[2:] if ":" in line and line.split(":", 1)[0].strip())
with open("/proc/net/route", encoding="ascii") as stream:
    ipv4_routes = [line for line in stream.read().splitlines()[1:] if line.strip()]
with open("/proc/net/ipv6_route", encoding="ascii") as stream:
    ipv6_routes = [line for line in stream.read().splitlines() if line.strip() and line.split()[-1] != "lo"]
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.settimeout(1)
external_result = sock.connect_ex(("1.1.1.1", 443))
sock.close()
if external_result not in (errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EPERM):
    raise SystemExit(f"external route was not blocked: connect_ex={external_result}")
print(f"NETWORK_INTERFACES={interfaces}")
print(f"IPV4_EXTERNAL_ROUTES={len(ipv4_routes)} IPV6_EXTERNAL_ROUTES={len(ipv6_routes)}")
print(f"EXTERNAL_CONNECT_RESULT={external_result}")
print("NETWORK_ISOLATION=PASS")
PY
printf 'SECCOMP_PROBE=START\n'
/work/venv/bin/python /work/repo/Backend/scripts/seccomp_exec.py --allow-ip-sockets \
    /work/venv/bin/python /work/repo/Backend/scripts/probe_firestore_emulator_network.py
printf 'SECCOMP_PROBE=PASS\n'
printf 'EMULATOR_TESTS=START project=demo-trendscout-p3s1 database=(default)\n'
/work/node/node_modules/.bin/firebase \
    --project demo-trendscout-p3s1 \
    --config /work/repo/Backend/emulator_tests/firebase.json \
    emulators:exec --only firestore --log-verbosity INFO \
    'bash /work/run_emulator_tests.sh'
printf 'EMULATOR_TESTS=PASS\n'
ISOLATED
chmod 700 "$RUN_ROOT/run_isolated.sh"

STAGE="isolated preparation; network for Git, PyPI, npm, and emulator download only"
printf '\nSTAGE=%s\n' "$STAGE"
if ! "$BWRAP_REAL" "${BASE_ARGS[@]}" "${COMMON_ENV_ARGS[@]}" \
    --setenv PYTHON_COMMAND "$PYTHON_REAL" --setenv JAVA_COMMAND "$JAVA_REAL" \
    --setenv NPM_COMMAND "$NPM_REAL" --setenv GIT_COMMAND "$GIT_REAL" \
    --setenv SHA256_COMMAND "$SHA256_REAL" --setenv REPOSITORY "$REPOSITORY" \
    --setenv EXPECTED_MAIN "$EXPECTED_MAIN" --setenv FIREBASE_TOOLS_VERSION "$FIREBASE_TOOLS_VERSION" \
    --setenv SYNTHETIC_PROJECT "$SYNTHETIC_PROJECT" \
    --chdir /work /bin/bash /work/prepare.sh; then
    printf 'Preparation failed; see the log for the failing command.\n' >&2
    exit 20
fi

STAGE="loopback-only emulator and integration tests"
printf '\nSTAGE=%s\n' "$STAGE"
if ! "$BWRAP_REAL" "${BASE_ARGS[@]}" --unshare-net "${COMMON_ENV_ARGS[@]}" \
    --chdir /work /bin/bash /work/run_isolated.sh; then
    printf 'Isolated emulator acceptance failed; see the test trace in the log.\n' >&2
    exit 21
fi
STAGE=complete
