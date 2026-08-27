#!/usr/bin/env bash
# Smoke a Linux TST Desk bundle on a clean guest (TD-1301 / TD-1302).
#
# This host has no /dev/kvm, so the guest is Docker, not qemu. ubuntu:22.04
# is the sidecar guest (same as package.yml). ubuntu:24.04 is the install
# guest — the .deb needs libwebkit2gtk-4.1, which 22.04 does not ship.
#
# Usage: smoke_linux_bundle.sh [path-to-deb] [path-to-appimage]
# Defaults: the unique files under shell/target/release/bundle/{deb,appimage}/
#
# Works on the host architecture: on aarch64 Linux the Docker guests pull
# arm64 images automatically (TD-4902). On amd64, smoke the x86_64 bundle
# you built locally; CI builds and smokes aarch64 on ubuntu-24.04-arm.

set -euo pipefail

if ! command -v docker >/dev/null; then
  echo "docker is required for the clean guest" >&2
  exit 1
fi

REPO=$(cd "$(dirname "$0")/../.." && pwd)
E2E_PY="$REPO/core/scripts/smoke_linux_e2e.py"
test -f "$E2E_PY"

pick_one() {
  local hint=$1
  shift
  local -a cands=("$@")
  if (( ${#cands[@]} != 1 )); then
    echo "need exactly one $hint (got ${#cands[@]}). pass the path." >&2
    exit 1
  fi
  echo "${cands[0]}"
}

DEB=${1:-}
if [[ -z "$DEB" ]]; then
  shopt -s nullglob
  DEB=$(pick_one ".deb" "$REPO"/shell/target/release/bundle/deb/*.deb)
  shopt -u nullglob
fi
DEB=$(cd "$(dirname "$DEB")" && pwd)/$(basename "$DEB")
test -f "$DEB"

APPIMAGE=${2:-}
if [[ -z "$APPIMAGE" ]]; then
  shopt -s nullglob
  apps=("$REPO"/shell/target/release/bundle/appimage/*.AppImage)
  shopt -u nullglob
  if (( ${#apps[@]} == 1 )); then
    APPIMAGE=${apps[0]}
  fi
fi
if [[ -n "$APPIMAGE" ]]; then
  APPIMAGE=$(cd "$(dirname "$APPIMAGE")" && pwd)/$(basename "$APPIMAGE")
  test -f "$APPIMAGE"
fi

WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT
cp "$DEB" "$WORKDIR/tst-desk.deb"
cp "$E2E_PY" "$WORKDIR/smoke_linux_e2e.py"
if [[ -n "$APPIMAGE" ]]; then
  cp "$APPIMAGE" "$WORKDIR/tst-desk.AppImage"
fi

SIDECAR_IMAGE=${SIDECAR_IMAGE:-ubuntu:22.04}
APP_IMAGE=${APP_IMAGE:-ubuntu:24.04}

echo "== sidecar on ${SIDECAR_IMAGE} (extracted tree, empty PATH) =="
docker run --rm -i \
  -v "$WORKDIR:/bundle:ro" \
  -e DEBIAN_FRONTEND=noninteractive \
  "$SIDECAR_IMAGE" \
  bash -s <<'EOF'
set -euo pipefail
apt-get update -qq
apt-get install -y -qq binutils >/dev/null
cd /tmp
dpkg-deb -x /bundle/tst-desk.deb extracted
BIN=/tmp/extracted/usr/bin/tstd
test -x "$BIN"
if ldd "$BIN" | grep -qi python; then
  echo "sidecar is linked against system Python" >&2
  ldd "$BIN"
  exit 1
fi
NOPATH=$(mktemp -d)
DATA=$(mktemp -d)
env -i PATH="$NOPATH" HOME=/tmp "$BIN" --data-dir "$DATA" --log-level INFO &
PID=$!
for _ in $(seq 1 100); do
  [[ -f "$DATA/port.json" ]] && break
  sleep 0.1
done
test -f "$DATA/port.json"
# Snapshot before SIGTERM: a clean shutdown unlinks the port file.
cp "$DATA/port.json" /tmp/sidecar-port.json
kill "$PID" 2>/dev/null || true
wait "$PID" 2>/dev/null || true
grep -Eq '"port": [1-9][0-9]*' /tmp/sidecar-port.json
grep -q '"token"' /tmp/sidecar-port.json
echo "sidecar ok: $(cat /tmp/sidecar-port.json)"
EOF

echo "== app launch on ${APP_IMAGE} (dpkg -i + xvfb) =="
docker run --rm -i \
  -v "$WORKDIR:/bundle:ro" \
  -e DEBIAN_FRONTEND=noninteractive \
  "$APP_IMAGE" \
  bash -s <<'EOF'
set -euo pipefail
apt-get update -qq
apt-get install -y -qq xvfb ca-certificates >/dev/null
dpkg -i /bundle/tst-desk.deb || true
apt-get install -y -qq -f >/dev/null
test -x /usr/bin/tst-desk
test -x /usr/bin/tstd
export HOME=/tmp/guest
mkdir -p "$HOME"
PORT="$HOME/.local/share/tst-desk/port.json"
rm -f "$PORT"
xvfb-run -a -s "-screen 0 1280x800x24" /usr/bin/tst-desk &
APP=$!
for _ in $(seq 1 150); do
  [[ -f "$PORT" ]] && break
  if ! kill -0 "$APP" 2>/dev/null; then
    echo "tst-desk exited before writing port.json" >&2
    exit 1
  fi
  sleep 0.2
done
test -f "$PORT"
cp "$PORT" /tmp/app-port.json
LISTEN_PORT=$(sed -n 's/.*"port": \([0-9][0-9]*\).*/\1/p' /tmp/app-port.json | head -1)
bash -c "echo >/dev/tcp/127.0.0.1/${LISTEN_PORT}"
grep -Eq '"port": [1-9][0-9]*' /tmp/app-port.json
grep -q '"token"' /tmp/app-port.json
echo "app ok: $(cat /tmp/app-port.json)"
kill "$APP" 2>/dev/null || true
wait "$APP" 2>/dev/null || true
EOF

echo "== protocol E2E on ${APP_IMAGE} (extracted tstd + loopback mock) =="
docker run --rm -i \
  -v "$WORKDIR:/bundle:ro" \
  -e DEBIAN_FRONTEND=noninteractive \
  "$APP_IMAGE" \
  bash -s <<'EOF'
set -euo pipefail
apt-get update -qq
apt-get install -y -qq python3 git >/dev/null
cd /tmp
dpkg-deb -x /bundle/tst-desk.deb extracted
BIN=/tmp/extracted/usr/bin/tstd
test -x "$BIN"
export HOME=/tmp/guest
mkdir -p "$HOME"
WS=/tmp/workspace
mkdir -p "$WS"
git -C "$WS" init -q
git -C "$WS" -c user.email=smoke@tst.desk -c user.name=smoke add -A
git -C "$WS" -c user.email=smoke@tst.desk -c user.name=smoke commit -q --allow-empty -m baseline
python3 /bundle/smoke_linux_e2e.py --serve --workspace "$WS" &
MOCK=$!
for _ in $(seq 1 50); do
  bash -c "echo >/dev/tcp/127.0.0.1/11434" 2>/dev/null && break
  sleep 0.1
done
bash -c "echo >/dev/tcp/127.0.0.1/11434"
DATA="$HOME/.local/share/tst-desk"
mkdir -p "$DATA"
PORT="$DATA/port.json"
rm -f "$PORT"
"$BIN" --data-dir "$DATA" --log-level INFO &
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
python3 /bundle/smoke_linux_e2e.py --client --workspace "$WS" --port-file "$PORT"
python3 /bundle/smoke_linux_e2e.py --probe-keychain --workspace "$WS" --port-file "$PORT"
# Daemon first: graceful quit distills, and the mock must still be up.
kill "$DAEMON" 2>/dev/null || true
wait "$DAEMON" 2>/dev/null || true
kill "$MOCK" 2>/dev/null || true
wait "$MOCK" 2>/dev/null || true
echo "protocol e2e ok"
EOF

if [[ -n "${APPIMAGE:-}" && -f "$WORKDIR/tst-desk.AppImage" ]]; then
  echo "== AppImage sidecar on ${SIDECAR_IMAGE} (extract, empty PATH) =="
  docker run --rm -i \
    -v "$WORKDIR:/bundle:ro" \
    -e DEBIAN_FRONTEND=noninteractive \
    "$SIDECAR_IMAGE" \
    bash -s <<'EOF'
set -euo pipefail
apt-get update -qq
# file + zlib let the AppImage unpack without FUSE.
apt-get install -y -qq file zlib1g >/dev/null
cd /tmp
cp /bundle/tst-desk.AppImage ./tst-desk.AppImage
chmod +x ./tst-desk.AppImage
./tst-desk.AppImage --appimage-extract >/dev/null
BIN=""
for cand in /tmp/squashfs-root/usr/bin/tstd /tmp/squashfs-root/usr/bin/tstd-*; do
  if [[ -x "$cand" ]]; then
    BIN=$cand
    break
  fi
done
test -n "$BIN"
test -x "$BIN"
NOPATH=$(mktemp -d)
DATA=$(mktemp -d)
env -i PATH="$NOPATH" HOME=/tmp "$BIN" --data-dir "$DATA" --log-level INFO &
PID=$!
for _ in $(seq 1 100); do
  [[ -f "$DATA/port.json" ]] && break
  sleep 0.1
done
test -f "$DATA/port.json"
cp "$DATA/port.json" /tmp/appimage-port.json
kill "$PID" 2>/dev/null || true
wait "$PID" 2>/dev/null || true
grep -Eq '"port": [1-9][0-9]*' /tmp/appimage-port.json
grep -q '"token"' /tmp/appimage-port.json
echo "appimage sidecar ok: $(cat /tmp/appimage-port.json)"
EOF
else
  echo "AppImage not provided; skipping AppImage extract smoke"
fi

echo "clean Linux guest: sidecar + windowed host + protocol E2E served"
