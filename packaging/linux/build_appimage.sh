#!/usr/bin/env bash
# Package the PyInstaller onedir build (dist/Latkan) as dist/Latkan-<version>-<arch>.AppImage.
#
# Usage: packaging/linux/build_appimage.sh [version]
#        (version defaults to __version__ from src/latkan/__init__.py)
#
# Environment:
#   APPIMAGETOOL            explicit path to appimagetool
#   ALLOW_DOWNLOAD=1        allow downloading appimagetool from the official AppImage/appimagetool
#                           GitHub release when it is not installed (never done silently)
#   APPIMAGE_RUNTIME_FILE   optional pre-downloaded type-2 runtime passed to --runtime-file
#   ARCH                    target architecture (default: uname -m)
#
# Build on the oldest distribution you want to support (release CI uses Ubuntu 22.04): the AppImage
# runs on systems whose glibc is at least as new as the build machine's.
set -euo pipefail

die() { echo "error: $*" >&2; exit 1; }
info() { echo "[appimage] $*" >&2; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DIST="$ROOT/dist"
BUILD="$ROOT/build"
APPDIR="$BUILD/AppDir"
ARCH="${ARCH:-$(uname -m)}"
APP_SRC="$DIST/Latkan"

VERSION="${1:-}"
if [[ -z "$VERSION" ]]; then
    VERSION="$(sed -nE "s/^__version__[[:space:]]*=[[:space:]]*['\"]([^'\"]+)['\"].*/\1/p" \
        "$ROOT/src/latkan/__init__.py" | head -n 1)"
fi
[[ -n "$VERSION" ]] || die "could not determine the version (pass it as the first argument)"
OUT="$DIST/Latkan-$VERSION-$ARCH.AppImage"

[[ -x "$APP_SRC/Latkan" ]] || die "$APP_SRC/Latkan not found - run PyInstaller first (python scripts/build.py)"

ICON=""
for candidate in "$ROOT/assets/icon-256.png" "$ROOT/assets/icon.png"; do
    if [[ -f "$candidate" ]]; then ICON="$candidate"; break; fi
done
[[ -n "$ICON" ]] || die "no icon in assets/ - run: python scripts/make_icons.py"

# ------------------------------------------------------------------------------ locate appimagetool

find_appimagetool() {
    if [[ -n "${APPIMAGETOOL:-}" ]]; then
        [[ -x "$APPIMAGETOOL" ]] || die "APPIMAGETOOL=$APPIMAGETOOL is not executable"
        echo "$APPIMAGETOOL"; return
    fi
    if command -v appimagetool >/dev/null 2>&1; then
        command -v appimagetool; return
    fi
    local cached="$BUILD/tools/appimagetool-$ARCH.AppImage"
    if [[ -x "$cached" ]]; then
        echo "$cached"; return
    fi
    if [[ "${ALLOW_DOWNLOAD:-0}" != "1" ]]; then
        die "appimagetool not found. Install it (https://github.com/AppImage/appimagetool), set APPIMAGETOOL, or re-run with ALLOW_DOWNLOAD=1 to download it."
    fi
    local url="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-$ARCH.AppImage"
    info "downloading $url"
    mkdir -p "$(dirname "$cached")"
    if command -v curl >/dev/null 2>&1; then
        curl -fL --retry 3 --retry-delay 2 -o "$cached.part" "$url" || die "download failed: $url"
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O "$cached.part" "$url" || die "download failed: $url"
    else
        die "neither curl nor wget is available to download appimagetool"
    fi
    chmod +x "$cached.part"
    mv "$cached.part" "$cached"
    echo "$cached"
}

TOOL="$(find_appimagetool)"
info "using $TOOL"

# ------------------------------------------------------------------------------------ build AppDir

info "assembling $APPDIR"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/lib/latkan" "$APPDIR/usr/share/applications" \
    "$APPDIR/usr/share/icons/hicolor/256x256/apps"

cp -a "$APP_SRC/." "$APPDIR/usr/lib/latkan/"
ln -s ../lib/latkan/Latkan "$APPDIR/usr/bin/latkan"

cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
# AppImage entry point: start the bundled PyInstaller build. $APPIMAGE (set by the AppImage runtime)
# is what Latkan writes into its autostart entry, so "Start at login" survives the temporary mount.
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/lib/latkan/Latkan" "$@"
EOF
chmod 0755 "$APPDIR/AppRun"

install -m 0644 "$ROOT/packaging/linux/latkan.desktop" "$APPDIR/latkan.desktop"
install -m 0644 "$ROOT/packaging/linux/latkan.desktop" "$APPDIR/usr/share/applications/latkan.desktop"
install -m 0644 "$ICON" "$APPDIR/latkan.png"
install -m 0644 "$ICON" "$APPDIR/usr/share/icons/hicolor/256x256/apps/latkan.png"
ln -s latkan.png "$APPDIR/.DirIcon"

# ------------------------------------------------------------------------------------- run the tool

# Run AppImage-packaged tools without FUSE (containers, CI runners without /dev/fuse).
export APPIMAGE_EXTRACT_AND_RUN="${APPIMAGE_EXTRACT_AND_RUN:-1}"
export ARCH VERSION

args=(--no-appstream)
if [[ -n "${APPIMAGE_RUNTIME_FILE:-}" ]]; then
    args+=(--runtime-file "$APPIMAGE_RUNTIME_FILE")
fi

mkdir -p "$DIST"
rm -f "$OUT"
"$TOOL" "${args[@]}" "$APPDIR" "$OUT"
[[ -f "$OUT" ]] || die "appimagetool did not produce $OUT"
chmod 0755 "$OUT"
info "created $OUT"
