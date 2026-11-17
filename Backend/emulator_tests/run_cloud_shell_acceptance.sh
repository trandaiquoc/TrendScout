#!/usr/bin/env bash
# Copy-paste runner for guarded Firestore Emulator acceptance in Cloud Shell.
set -Eeuo pipefail
REPOSITORY="https://github.com/trandaiquoc/TrendScout.git"
SYNTHETIC_PROJECT="demo-trendscout-p3s1"
FIREBASE_TOOLS_VERSION="15.34.2"
SOURCE_REF="${TREND_SCOUT_SOURCE_REF:-${GITHUB_REF:-refs/heads/main}}"
EXPECTED_SOURCE_SHA="${TREND_SCOUT_SOURCE_SHA:-${GITHUB_SHA:-}}"
STAGE=initialization

if { [[ "$SOURCE_REF" != refs/heads/* ]] && [[ ! "$SOURCE_REF" =~ ^refs/pull/[0-9]+/merge$ ]]; } \
    || [[ "$SOURCE_REF" == *[!A-Za-z0-9._/-]* || "$SOURCE_REF" == *".."* || "$SOURCE_REF" == *"//"* ]]; then
    printf 'FAIL: source ref must be a valid branch or GitHub pull-request merge ref: %s\n' "$SOURCE_REF" >&2
    exit 2
fi
if [[ -n "$EXPECTED_SOURCE_SHA" && ! "$EXPECTED_SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]]; then
    printf 'FAIL: TREND_SCOUT_SOURCE_SHA/GITHUB_SHA must be a full 40-character commit SHA.\n' >&2
    exit 2
fi

if [[ -z "$HOME" || ! -d "$HOME" ]]; then
    printf 'FAIL: HOME must name the Cloud Shell user directory.\n' >&2
    exit 2
fi
LOG_DIR="${FIRESTORE_ACCEPTANCE_LOG_DIR:-${RUNNER_TEMP:-$HOME}}"
if [[ ! -d "$LOG_DIR" || ! -w "$LOG_DIR" ]]; then
    printf 'FAIL: acceptance log directory must exist and be writable: %s\n' "$LOG_DIR" >&2
    exit 2
fi
LOG_FILE="$LOG_DIR/trendscout-firestore-acceptance-$(date -u +%Y%m%dT%H%M%SZ).log"
touch "$LOG_FILE"
chmod 600 "$LOG_FILE"
exec > >(tee -a "$LOG_FILE") 2>&1
RUN_ROOT="$(mktemp -d /tmp/trendscout-firestore-acceptance.XXXXXXXX)"
SAFE_HOME="$RUN_ROOT/home"
SANDBOX_ETC="$RUN_ROOT/sandbox-etc"
mkdir -p "$SAFE_HOME" "$RUN_ROOT/no-gcloud-config" "$SANDBOX_ETC/ssl/certs"

finish() {
    local result=$?
    trap - EXIT ERR INT TERM HUP
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
trap 'exit 130' INT
trap 'exit 143' TERM HUP

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
NODE_HOST_COMMAND="$(require_command node)"
NPM_HOST_COMMAND="$(require_command npm)"
JAVA_COMMAND="$(require_command java)"
READLINK_COMMAND="$(require_command readlink)"
SHA256_COMMAND="$(require_command sha256sum)"
CP_COMMAND="$(require_command cp)"
IP_COMMAND="$(require_command ip)"
BWRAP_REAL="$(real_path "$BWRAP_COMMAND")"
GIT_REAL="$(real_path "$GIT_COMMAND")"
PYTHON_REAL="$(real_path "$PYTHON_COMMAND")"
NODE_REAL="$(real_path "$NODE_HOST_COMMAND")"
NPM_REAL="$(real_path "$NPM_HOST_COMMAND")"
JAVA_REAL="$(real_path "$JAVA_COMMAND")"
IP_REAL="$(real_path "$IP_COMMAND")"
SHA256_REAL="$(real_path "$SHA256_COMMAND")"
JAVA_HOME="$(dirname "$(dirname "$JAVA_REAL")")"
for p in "$BWRAP_REAL" "$GIT_REAL" "$PYTHON_REAL" "$NODE_REAL" "$NPM_REAL" "$JAVA_REAL" "$IP_REAL" "$SHA256_REAL" "$JAVA_HOME"; do
    if [[ "$p" == "$NODE_REAL" || "$p" == "$NPM_REAL" ]]; then
        continue
    fi
    if ! is_system_path "$p"; then
        printf 'FAIL: refusing to expose runtime outside mounted system paths: %s\n' "$p" >&2
        printf 'Use system-installed Python 3.12, Java 21, Bubblewrap, and iproute2.\n' >&2
        exit 3
    fi
done

NODE_BIND_ARGS=()
if is_system_path "$NODE_REAL"; then
    if ! is_system_path "$NPM_REAL"; then
        printf 'FAIL: system Node requires npm from a mounted system path: %s\n' "$NPM_REAL" >&2
        exit 3
    fi
    NODE_COMMAND="$NODE_REAL"
    NPM_COMMAND="$NPM_REAL"
    NODE_BIN_PATH="$(dirname "$NODE_REAL")"
else
    TOOL_CACHE_REAL=""
    if [[ -n "${RUNNER_TOOL_CACHE:-}" && -d "$RUNNER_TOOL_CACHE" ]]; then
        TOOL_CACHE_REAL="$(real_path "$RUNNER_TOOL_CACHE")"
    fi
    if [[ -z "$TOOL_CACHE_REAL" || "$NODE_REAL" != "$TOOL_CACHE_REAL"/* ]]; then
        printf 'FAIL: refusing Node outside mounted system paths and the GitHub tool cache: %s\n' "$NODE_REAL" >&2
        exit 3
    fi
    TOOL_CACHE_RELATIVE="${NODE_REAL#"$TOOL_CACHE_REAL"/}"
    IFS=/ read -r TOOL_NAME TOOL_VERSION TOOL_ARCH _ <<< "$TOOL_CACHE_RELATIVE"
    if [[ -z "$TOOL_NAME" || -z "$TOOL_VERSION" || -z "$TOOL_ARCH" ]]; then
        printf 'FAIL: cannot identify Node package root in runner tool cache: %s\n' "$NODE_REAL" >&2
        exit 3
    fi
    NODE_PACKAGE_ROOT="$(real_path "$TOOL_CACHE_REAL/$TOOL_NAME/$TOOL_VERSION/$TOOL_ARCH")"
    if [[ "$NODE_REAL" != "$NODE_PACKAGE_ROOT"/* || ! -x "$NODE_PACKAGE_ROOT/bin/node" || ! -x "$NODE_PACKAGE_ROOT/bin/npm" ]]; then
        printf 'FAIL: Node/npm package paths are not confined to one tool-cache package: %s\n' "$NODE_PACKAGE_ROOT" >&2
        exit 3
    fi
    NODE_COMMAND=/runtime/node/bin/node
    NPM_COMMAND=/runtime/node/bin/npm
    NODE_BIN_PATH=/runtime/node/bin
    NODE_BIND_ARGS+=(--dir /runtime --dir /runtime/node --ro-bind "$NODE_PACKAGE_ROOT" /runtime/node)
    printf 'NODE_TOOL_CACHE_PACKAGE=%s\n' "$NODE_PACKAGE_ROOT"
fi
BWRAP_VERSION="$("$BWRAP_REAL" --version)"
printf 'BWRAP_VERSION=%s\n' "$BWRAP_VERSION"
if [[ "$BWRAP_VERSION" != *"0.9.0"* ]]; then
    printf 'FAIL: expected Bubblewrap 0.9.0, found %s\n' "$BWRAP_VERSION" >&2
    exit 3
fi
SAFE_PATH="$(dirname "$PYTHON_REAL"):$NODE_BIN_PATH:$(dirname "$JAVA_REAL"):$(dirname "$IP_REAL"):/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

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
BASE_ARGS+=("${NODE_BIND_ARGS[@]}")
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
mkdir -p /work/repo
"$GIT_COMMAND" -C /work/repo init --quiet
"$GIT_COMMAND" -C /work/repo remote add origin "$REPOSITORY"
if [[ -z "$EXPECTED_SOURCE_SHA" ]]; then
    remote_line="$("$GIT_COMMAND" ls-remote --exit-code --refs "$REPOSITORY" "$SOURCE_REF")"
    read -r EXPECTED_SOURCE_SHA resolved_ref <<< "$remote_line"
    if [[ "$resolved_ref" != "$SOURCE_REF" || ! "$EXPECTED_SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]]; then
        printf 'FAIL: could not resolve one immutable source SHA for %s\n' "$SOURCE_REF" >&2
        exit 13
    fi
fi
printf 'SOURCE_REF=%s\n' "$SOURCE_REF"
printf 'EXPECTED_SOURCE_COMMIT=%s\n' "$EXPECTED_SOURCE_SHA"
"$GIT_COMMAND" -C /work/repo fetch --quiet --depth=1 origin "$SOURCE_REF"
fetched_commit="$("$GIT_COMMAND" -C /work/repo rev-parse FETCH_HEAD)"
if [[ "$fetched_commit" != "$EXPECTED_SOURCE_SHA" ]]; then
    printf 'FAIL: source ref changed or did not match the verified commit; expected=%s fetched=%s\n' \
        "$EXPECTED_SOURCE_SHA" "$fetched_commit" >&2
    exit 13
fi
"$GIT_COMMAND" -C /work/repo checkout --quiet --detach "$fetched_commit"
source_commit="$("$GIT_COMMAND" -C /work/repo rev-parse HEAD)"
if [[ "$source_commit" != "$EXPECTED_SOURCE_SHA" ]]; then
    printf 'FAIL: checked-out source revision mismatch; expected=%s actual=%s\n' \
        "$EXPECTED_SOURCE_SHA" "$source_commit" >&2
    exit 13
fi
printf 'SOURCE_COMMIT=%s\n' "$source_commit"
printf 'SOURCE_COMMIT_VERIFICATION=PASS\n'

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
    --setenv NODE_COMMAND "$NODE_COMMAND" --setenv NPM_COMMAND "$NPM_COMMAND" \
    --setenv GIT_COMMAND "$GIT_REAL" \
    --setenv SHA256_COMMAND "$SHA256_REAL" --setenv REPOSITORY "$REPOSITORY" \
    --setenv SOURCE_REF "$SOURCE_REF" --setenv EXPECTED_SOURCE_SHA "$EXPECTED_SOURCE_SHA" \
    --setenv FIREBASE_TOOLS_VERSION "$FIREBASE_TOOLS_VERSION" \
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
