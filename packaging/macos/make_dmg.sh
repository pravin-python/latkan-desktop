#!/usr/bin/env bash
# Build a compressed, drag-to-install disk image for Latkan.
#
# Usage: packaging/macos/make_dmg.sh <path/to/Latkan.app> <output.dmg> [volume name]
#
# The image (UDZO, zlib level 9) contains Latkan.app and an /Applications symlink so users can drag
# the app across. The .app is copied with ditto so code signatures, symlinks and xattrs survive.
# Called by scripts/build.py after the bundle has been signed; notarisation (release CI) happens on
# the resulting .dmg.
set -euo pipefail

usage() {
    echo "usage: $0 <path/to/Latkan.app> <output.dmg> [volume name]" >&2
    exit 2
}

[[ $# -ge 2 && $# -le 3 ]] || usage
APP="${1%/}"
OUT="$2"
VOLNAME="${3:-Latkan}"

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "error: make_dmg.sh must run on macOS (needs hdiutil)" >&2
    exit 1
fi
if [[ ! -d "$APP" || "$APP" != *.app ]]; then
    echo "error: '$APP' is not an .app bundle" >&2
    exit 1
fi

STAGING="$(mktemp -d "${TMPDIR:-/tmp}/latkan-dmg.XXXXXX")"
cleanup() { rm -rf "$STAGING"; }
trap cleanup EXIT

ditto "$APP" "$STAGING/$(basename "$APP")"
ln -s /Applications "$STAGING/Applications"

mkdir -p "$(dirname "$OUT")"
rm -f "$OUT"

# hdiutil occasionally fails with "Resource busy" on CI runners (Spotlight/XProtect scanning the
# staging folder); retry a few times before giving up.
attempt=1
until hdiutil create \
    -volname "$VOLNAME" \
    -srcfolder "$STAGING" \
    -fs HFS+ \
    -format UDZO \
    -imagekey zlib-level=9 \
    -ov \
    "$OUT"; do
    if (( attempt >= 5 )); then
        echo "error: hdiutil create failed after $attempt attempts" >&2
        exit 1
    fi
    echo "hdiutil create failed (attempt $attempt), retrying in $((attempt * 3)) s..." >&2
    sleep $((attempt * 3))
    attempt=$((attempt + 1))
done

hdiutil verify "$OUT"
echo "Created $OUT"
