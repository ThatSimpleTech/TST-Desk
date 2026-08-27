#!/usr/bin/env bash
# Packaged-sidecar smoke on a clean macOS guest (TD-4906).
#
# Run on macOS after `npm run tauri:build` in ui/. Proves the bundled sidecar
# serves with no Python on PATH, then runs the protocol stranger loop
# (mock OpenAI → local preset → fs_write → reply) and a keychain probe.
#
# Usage: smoke_macos_bundle.sh [path-to-.app]

set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "smoke_macos_bundle.sh requires macOS" >&2
  exit 1
fi

REPO=$(cd "$(dirname "$0")/../.." && pwd)
E2E_PY="$REPO/core/scripts/smoke_linux_e2e.py"
test -f "$E2E_PY"

APP=${1:-}
if [[ -z "$APP" ]]; then
  APP=$(ls -d "$REPO"/shell/target/release/bundle/macos/*.app 2>/dev/null | head -1 || true)
fi
if [[ -z "$APP" || ! -d "$APP" ]]; then
  echo "need a built .app (pass path or run tauri:build first)" >&2
  exit 1
fi
APP=$(cd "$(dirname "$APP")" && pwd)/$(basename "$APP")

SIDECAR="$APP/Contents/MacOS/tstd"
test -x "$SIDECAR"

echo "== sidecar with empty PATH =="
DATA=$(mktemp -d)
NOPATH=$(mktemp -d)
trap 'rm -rf "$DATA" "$NOPATH"' EXIT
env -i PATH="$NOPATH" HOME="$DATA" "$SIDECAR" --data-dir "$DATA/d" --log-level INFO &
PID=$!
for _ in $(seq 1 100); do
  [[ -f "$DATA/d/port.json" ]] && break
  sleep 0.1
done
test -f "$DATA/d/port.json"
kill "$PID" 2>/dev/null || true
wait "$PID" 2>/dev/null || true
echo "sidecar ok: $(cat "$DATA/d/port.json")"

echo "== protocol E2E (bundled tstd + loopback mock) =="
WS=$(mktemp -d)
export HOME="$DATA/home"
mkdir -p "$HOME"
git -C "$WS" init -q
git -C "$WS" -c user.email=smoke@tst.desk -c user.name=smoke commit -q --allow-empty -m baseline
PORT="$DATA/daemon/port.json"
mkdir -p "$(dirname "$PORT")"
rm -f "$PORT"
python3 "$E2E_PY" --serve --workspace "$WS" &
MOCK=$!
for _ in $(seq 1 50); do
  bash -c "echo >/dev/tcp/127.0.0.1/11434" 2>/dev/null && break
  sleep 0.1
done
bash -c "echo >/dev/tcp/127.0.0.1/11434"
"$SIDECAR" --data-dir "$DATA/daemon" --log-level INFO &
DAEMON=$!
for _ in $(seq 1 150); do
  [[ -f "$PORT" ]] && break
  if ! kill -0 "$DAEMON" 2>/dev/null; then
    echo "tstd exited before writing port.json" >&2
    exit 1
  fi
  sleep 0.1
done
test -f "$PORT"
python3 "$E2E_PY" --client --workspace "$WS" --port-file "$PORT"
python3 "$E2E_PY" --probe-keychain --workspace "$WS" --port-file "$PORT"
kill "$DAEMON" 2>/dev/null || true
wait "$DAEMON" 2>/dev/null || true
kill "$MOCK" 2>/dev/null || true
wait "$MOCK" 2>/dev/null || true
echo "clean macOS guest: sidecar + protocol E2E + keychain probe ok"
