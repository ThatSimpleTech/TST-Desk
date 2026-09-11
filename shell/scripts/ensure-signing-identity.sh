#!/usr/bin/env bash
# Create (once) the self-signed code-signing identity TST Desk is built with.
#
# Why: macOS TCC pins each Screen Recording / Accessibility grant to the
# app's code-signing requirement. An ad-hoc signature's requirement is
# `cdhash H"…"`, unique per build, so every rebuild silently invalidates
# the grants while System Settings still shows them ON (TD-4823). A
# certificate-based signature — even self-signed — yields
# `identifier "com.thatsimpletech.tstdesk" and certificate leaf = H"…"`,
# which is stable across rebuilds. No Apple Developer account is needed.
#
# Usage: shell/scripts/ensure-signing-identity.sh [name]
#   name defaults to $TST_SIGN_IDENTITY, then "tst-desk-dev" — the value
#   tauri.conf.json's bundle.macOS.signingIdentity expects.
#
# The keychain steps may prompt once for the login keychain password.
#
# GUI fallback if this script cannot import into the keychain:
#   Keychain Access → Keychain Access menu → Certificate Assistant →
#   Create a Certificate… → Name: tst-desk-dev, Identity Type: Self Signed
#   Root, Certificate Type: Code Signing → Create. Then double-click the
#   certificate in the login keychain → Trust → Code Signing: Always Trust.

set -euo pipefail

NAME="${1:-${TST_SIGN_IDENTITY:-tst-desk-dev}}"
KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "ensure-signing-identity: macOS only" >&2
  exit 1
fi

# Look at every identity with this name, not only the valid ones: creating
# another when an untrusted twin exists makes `codesign --sign NAME`
# ambiguous. A twin gets trusted or deleted, never duplicated.
by_name() { sed -n 's/^ *[0-9]*) \([0-9A-F]\{40\}\) "'"$NAME"'".*/\1/p' | sort -u; }
valid_ids="$(security find-identity -v -p codesigning 2>/dev/null | by_name)"
all_ids="$(security find-identity -p codesigning 2>/dev/null | by_name)"
if [[ -n "$valid_ids" ]]; then
  echo "signing identity present: $NAME ($(echo "$valid_ids" | tr '\n' ' ' | sed 's/ $//'))"
  exit 0
fi
if [[ -n "$all_ids" ]]; then
  echo "ensure-signing-identity: $NAME exists but is not a valid codesigning identity:" >&2
  echo "$all_ids" | sed 's/^/  /' >&2
  echo "Trust it (Keychain Access → certificate → Trust → Code Signing: Always Trust)" >&2
  echo "or delete it (security delete-certificate -Z <hash>) and rerun. Not creating a twin." >&2
  exit 1
fi

echo "creating self-signed code-signing identity: $NAME"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/tst-sign.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
PASS="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24 || true)"

cat >"$WORK/openssl.cnf" <<EOF
[ req ]
distinguished_name = dn
x509_extensions = ext
prompt = no

[ dn ]
CN = $NAME

[ ext ]
keyUsage = critical, digitalSignature
extendedKeyUsage = critical, codeSigning
basicConstraints = critical, CA:false
subjectKeyIdentifier = hash
EOF

# /usr/bin/openssl (LibreSSL) writes a PKCS#12 the macOS keychain imports
# without the -legacy dance newer Homebrew OpenSSL needs.
/usr/bin/openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days 3650 \
  -config "$WORK/openssl.cnf" \
  -keyout "$WORK/key.pem" -out "$WORK/cert.pem" >/dev/null 2>&1

/usr/bin/openssl pkcs12 -export \
  -inkey "$WORK/key.pem" -in "$WORK/cert.pem" -name "$NAME" \
  -out "$WORK/identity.p12" -passout "pass:$PASS"

# -T lets codesign / security use the key without a per-use dialog.
security import "$WORK/identity.p12" -k "$KEYCHAIN" -P "$PASS" \
  -T /usr/bin/codesign -T /usr/bin/security >/dev/null

# Trust the certificate for code signing; without this codesign refuses
# it and TCC would never evaluate the requirement.
security add-trusted-cert -r trustRoot -p codeSign -k "$KEYCHAIN" "$WORK/cert.pem"

# Let Apple's tools use the key without prompting on every sign. Prompts
# for the login keychain password once; best-effort in non-interactive runs.
if ! security set-key-partition-list -S apple-tool:,apple: -s "$KEYCHAIN" >/dev/null; then
  echo "warning: could not set the key partition list; codesign may prompt per use" >&2
fi

if security find-identity -v -p codesigning | grep -q "\"$NAME\""; then
  echo "signing identity ready: $NAME"
else
  echo "ensure-signing-identity: $NAME was imported but is not a valid codesigning identity;" >&2
  echo "use the Keychain Access fallback in this script's header." >&2
  exit 1
fi
