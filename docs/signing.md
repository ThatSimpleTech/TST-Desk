# Code signing and notarization (TD-4903)

TST Desk v0.1 ships **unsigned** on every platform. First-run warnings are
documented in the README under [Installing an unsigned build](../README.md#installing-an-unsigned-build).
This page records the v0.1 decision, what signing would cost, where release
secrets would live, and how CI would wire signing when we opt in — without
storing any secret in the repo or in config files.

## v0.1 decision (explicit refusal)

| Platform | v0.1 | Rationale |
|---|---|---|
| **macOS** | Unsigned, not notarized | Apple Developer Program (~$99/yr) + notarytool stapling in CI. Right-click → Open is acceptable for named early users. |
| **Windows** | Unsigned (no Authenticode) | EV/OV code-signing certificate (~$200–500/yr) + signtool in CI. SmartScreen “Run anyway” is documented. |
| **Linux** | Unsigned | No cheap, universal Linux codesign path for AppImage/deb that removes all friction. AppImage + dpkg need no signature today. |

**No telemetry** in any signing or notarization step — only Apple/Microsoft APIs
the maintainer invokes at release time.

When signing lands (target: first **external-user** or **v0.2** release,
whichever comes first), TD-4704 (auto-updater) may default on. Until then the
updater stays off.

## Where secrets live (never the repo)

| Secret | Holder | Used for |
|---|---|---|
| Apple Developer `.p12` (base64) | GitHub Actions **encrypted secret** `APPLE_CERTIFICATE` | macOS codesign in CI |
| `.p12` password | `APPLE_CERTIFICATE_PASSWORD` | Unlock the cert |
| Apple ID app password | `APPLE_ID`, `APPLE_PASSWORD`, `APPLE_TEAM_ID` | Notarization + staple |
| Windows Authenticode `.pfx` | `WINDOWS_CERTIFICATE` (+ password secret) | Sign `.msi` / `.exe` |
| User API keys | **OS keychain** only (`core/tstd/keychain.py`) | Runtime — unrelated to bundle signing |

OIDC-based cert acquisition (e.g. Apple notary via App Store Connect API key) is
preferred over long-lived passwords when we wire release; still GitHub encrypted
secrets or cloud KMS — never `config.yaml`, logs, or the audit DB.

Local dev may ad-hoc sign with a personal cert (`shell/tauri.conf.json`
`signingIdentity: "tst-desk-dev"`). That identity is **not** used in CI releases.

## CI wiring (future — not active in v0.1)

When certificates exist, extend `.github/workflows/package.yml` after the Tauri
build step (per matrix leg):

**macOS**

```yaml
# Requires secrets: APPLE_CERTIFICATE, APPLE_CERTIFICATE_PASSWORD,
# APPLE_ID, APPLE_PASSWORD, APPLE_TEAM_ID
env:
  APPLE_CERTIFICATE: ${{ secrets.APPLE_CERTIFICATE }}
  APPLE_CERTIFICATE_PASSWORD: ${{ secrets.APPLE_CERTIFICATE_PASSWORD }}
  APPLE_ID: ${{ secrets.APPLE_ID }}
  APPLE_PASSWORD: ${{ secrets.APPLE_PASSWORD }}
  APPLE_TEAM_ID: ${{ secrets.APPLE_TEAM_ID }}
```

Tauri bundler notarizes when these env vars are set (see Tauri v2 docs).

**Windows**

Sign the MSI with `signtool` (or `tauri signer`) using `WINDOWS_CERTIFICATE`
from secrets — after `npm run tauri:build`, before artifact upload.

**Linux**

No step unless a cheap path appears (e.g. distro-specific packaging later).
AppImage and `.deb` continue unsigned.

## Prime directives

- Signing calls are **maintainer-initiated** at release — no phone-home.
- Failed notarization fails the **release job**, not the user's daemon.
- Unsigned v0.1 builds remain valid; signing is an additive release-lane upgrade.

See also `DECISIONS.md` (2026-08-14 TD-1302, 2026-08-27 TD-4903).
