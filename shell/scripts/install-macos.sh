#!/usr/bin/env bash
# Build, sign, verify, and install TST Desk.app — the only sanctioned way
# to put a build in /Applications (TD-4823).
#
# Why a script: macOS TCC pins Screen Recording / Accessibility grants to
# the app's code-signing requirement. Hand-copying binaries into an
# installed bundle and ad-hoc signing them changes that requirement on
# every build, so System Settings keeps showing TST Desk ON while the
# host's TCC checks fail. This script signs with the stable self-signed
# identity from ensure-signing-identity.sh, refuses to install a bundle
# whose requirement is cdhash-pinned, and tells you when a one-time
# `tccutil reset` is needed because the previous install was ad-hoc.
#
# Usage: shell/scripts/install-macos.sh [--no-build] [--dest DIR]
#   --no-build   install the bundle already in shell/target/release/bundle
#   --dest DIR   install into DIR instead of /Applications
#
# Env: TST_SIGN_IDENTITY (default tst-desk-dev).

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SHELL_DIR="$REPO/shell"
UI_DIR="$REPO/ui"
BUNDLE_ID="com.thatsimpletech.tstdesk"
IDENTITY="${TST_SIGN_IDENTITY:-tst-desk-dev}"
APP_NAME="TST Desk.app"
BUILT="$SHELL_DIR/target/release/bundle/macos/$APP_NAME"
ENTITLEMENTS="$SHELL_DIR/macos-entitlements.plist"
DEST_DIR="/Applications"
BUILD=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-build) BUILD=0 ;;
    --dest) DEST_DIR="$2"; shift ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

DEST="$DEST_DIR/$APP_NAME"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "install-macos: macOS only" >&2
  exit 1
fi

requirement() { codesign -d -r- "$1" 2>&1 | sed -n 's/^designated => //p'; }
identifier() { codesign -dv "$1" 2>&1 | sed -n 's/^Identifier=//p'; }

# /Applications is root:admin; a user outside the admin group cannot replace
# a bundle there from a shell (no sudo either). Say so before the build.
mkdir -p "$DEST_DIR" 2>/dev/null || true
if [[ ! -w "$DEST_DIR" ]]; then
  echo "install-macos: $DEST_DIR is not writable by $(id -un)." >&2
  echo "Rerun with --dest \"\$HOME/Applications\" — TCC keys grants on the bundle id and" >&2
  echo "signature, not the path — or have an admin copy the built bundle with Finder." >&2
  exit 1
fi
cdhash_of() { codesign -dvvv "$1" 2>&1 | sed -n 's/^CDHash=//p' | head -n 1; }

# 1. Signing identity (created once, then reused forever).
"$SHELL_DIR/scripts/ensure-signing-identity.sh" "$IDENTITY"

# Sign by certificate hash, not by name: a stale twin of the name in the
# keychain (an untrusted earlier attempt) makes name lookup ambiguous, and
# TCC pins grants to one certificate, so exactly one valid one may exist.
HASHES="$(security find-identity -v -p codesigning 2>/dev/null \
  | sed -n 's/^ *[0-9]*) \([0-9A-F]\{40\}\) "'"$IDENTITY"'".*/\1/p' | sort -u)"
case "$(printf '%s\n' "$HASHES" | grep -c .)" in
  1) SIGN="$HASHES" ;;
  0) echo "install-macos: no valid codesigning identity named $IDENTITY" >&2; exit 1 ;;
  *) echo "install-macos: several valid identities are named $IDENTITY:" >&2
     echo "$HASHES" | sed 's/^/  /' >&2
     echo "delete the extras (security delete-certificate -Z <hash>) so TCC pins one certificate" >&2
     exit 1 ;;
esac
echo "signing as $IDENTITY ($SIGN)"

# 2. Build through Tauri so the bundle is signed once, as a whole. The
#    sidecar build (core/scripts/build_sidecar.py) reads TST_SIGN_IDENTITY;
#    Tauri's bundler reads APPLE_SIGNING_IDENTITY. The CLI is the npm one
#    ui/package.json wraps (there is no cargo-tauri on a dev box); its
#    beforeBuildCommand rebuilds the sidecar and the UI.
if [[ $BUILD -eq 1 ]]; then
  echo "building TST Desk (signing as $IDENTITY)…"
  (
    cd "$UI_DIR"
    TST_SIGN_IDENTITY="$SIGN" APPLE_SIGNING_IDENTITY="$SIGN" \
      npm run tauri:build -- --bundles app
  )
fi

if [[ ! -d "$BUILT" ]]; then
  echo "install-macos: no bundle at $BUILT" >&2
  exit 1
fi
echo "bundle: $BUILT (built $(stat -f '%Sm' "$BUILT"))"

HOST="$BUILT/Contents/MacOS/tst-desk"
SIDECAR="$BUILT/Contents/MacOS/tstd"

# 3. The sidecar must carry the bundle id, or TCC sees two clients.
if [[ -f "$SIDECAR" && "$(identifier "$SIDECAR")" != "$BUNDLE_ID" ]]; then
  echo "re-signing sidecar with identifier $BUNDLE_ID"
  codesign --force --sign "$SIGN" --identifier "$BUNDLE_ID" --options runtime \
    --entitlements "$ENTITLEMENTS" --timestamp=none "$SIDECAR"
  # Inner first, outer last: the bundle seal covers the sidecar.
  codesign --force --sign "$SIGN" --identifier "$BUNDLE_ID" --options runtime \
    --entitlements "$ENTITLEMENTS" --timestamp=none "$BUILT"
fi

# 4. Verify: the seal holds and both requirements are certificate-based.
codesign -vvv --deep --strict "$BUILT"
for bin in "$HOST" "$SIDECAR"; do
  [[ -f "$bin" ]] || continue
  req="$(requirement "$bin")"
  if [[ "$req" != *"certificate leaf"* ]]; then
    echo "install-macos: $bin is not signed with a certificate (requirement: $req)" >&2
    echo "refusing to install: TCC grants would reset on the next build" >&2
    exit 1
  fi
  if [[ "$(identifier "$bin")" != "$BUNDLE_ID" ]]; then
    echo "install-macos: $bin identifier is $(identifier "$bin"), expected $BUNDLE_ID" >&2
    exit 1
  fi
done

# 5. Never replace a running app.
if pgrep -x tst-desk >/dev/null 2>&1; then
  echo "install-macos: TST Desk is running — quit it (Cmd+Q) and rerun" >&2
  exit 1
fi

# 6. Compare with the previous install to know whether TCC rows go stale.
NEED_RESET=0
if [[ -d "$DEST" ]]; then
  old_req="$(requirement "$DEST" || true)"
  new_req="$(requirement "$BUILT")"
  if [[ "$old_req" != "$new_req" ]]; then
    NEED_RESET=1
  fi
fi

# 7. Stage beside the old install and swap with renames, so a failure part
#    way through never leaves a half-removed app.
STAGE="$DEST_DIR/.$APP_NAME.new"
OLD="$DEST_DIR/.$APP_NAME.old"
rm -rf "$STAGE" "$OLD"
ditto "$BUILT" "$STAGE"
if [[ -d "$DEST" ]]; then
  mv "$DEST" "$OLD"
fi
mv "$STAGE" "$DEST"
rm -rf "$OLD"
echo "installed $DEST"
echo "  requirement: $(requirement "$DEST")"
echo "  cdhash:      $(cdhash_of "$DEST")"

if [[ $NEED_RESET -eq 1 ]]; then
  cat <<EOF

The previous install had a different code-signing requirement, so the
Screen Recording / Accessibility rows in System Settings belong to it.
One-time repair (later installs signed as $IDENTITY keep their grants):

  tccutil reset ScreenCapture $BUNDLE_ID
  tccutil reset Accessibility $BUNDLE_ID

Then in System Settings → Privacy & Security → Screen & System Audio
Recording and → Accessibility, remove any lowercase "tst-desk" rows
(path-keyed rows from unbundled or hand-patched binaries) with "−".
Launch TST Desk, allow both prompts, then Quit (Cmd+Q) and reopen once.
Or use Reset grants in TST Desk → Settings → Computer use.
EOF
fi
