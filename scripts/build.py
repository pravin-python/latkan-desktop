#!/usr/bin/env python3
"""Cross-platform build driver for Latkan: icons -> PyInstaller -> native package for this OS.

Usage (from anywhere; paths are resolved relative to the repository)::

    python scripts/build.py [--skip-icons] [--skip-pyinstaller] [--no-package] [--portable-zip]

Artifacts written to ``dist/``:

=========  ==================================================================================
Windows    ``Latkan-<ver>-windows-x64-setup.exe`` when Inno Setup (ISCC) is found, otherwise
           ``Latkan-<ver>-windows-x64.zip`` (``--portable-zip`` produces the zip in both cases)
macOS      ``Latkan-<ver>-macos-<arm64|x86_64>.dmg`` (the .app is code-signed first)
Linux      ``Latkan-<ver>-linux-<arch>.tar.gz`` and, when appimagetool is available or
           ``APPIMAGE=1``, ``Latkan-<ver>-<arch>.AppImage``
=========  ==================================================================================

Environment variables:

``MACOS_SIGN_IDENTITY``  codesign identity (e.g. ``Developer ID Application: Name (TEAMID)``).
                         Unset -> ad-hoc signature (``-``), which is enough to run locally.
``APPIMAGE=1``           force the AppImage step (``ALLOW_DOWNLOAD=1`` lets it fetch appimagetool).
``ISCC``                 explicit path to Inno Setup's ``ISCC.exe``.
"""

from __future__ import annotations

import argparse
import fnmatch
import logging
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from collections.abc import Sequence
from pathlib import Path

# absolute(), not resolve(): a build started through a short junction/subst path (Windows MAX_PATH)
# must keep using it for the deep dist/Latkan/_internal/... tree.
ROOT = Path(__file__).absolute().parent.parent
SRC = ROOT / "src"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
SPEC = ROOT / "packaging" / "latkan.spec"
ISS = ROOT / "packaging" / "windows" / "latkan.iss"
MAKE_DMG = ROOT / "packaging" / "macos" / "make_dmg.sh"
BUILD_APPIMAGE = ROOT / "packaging" / "linux" / "build_appimage.sh"
APP_NAME = "Latkan"

log = logging.getLogger("build")

# Hardened-runtime entitlements for a PyInstaller + PySide6 + PyObjC app (only used with a real identity).
# libffi closures (ctypes / PyObjC) need writable+executable memory, and the bundled Python/Qt
# libraries are loaded from inside the bundle.
MAC_ENTITLEMENTS = {
    "com.apple.security.cs.allow-unsigned-executable-memory": True,
    "com.apple.security.cs.disable-library-validation": True,
}


class BuildError(RuntimeError):
    """A build precondition failed; the message is shown to the user."""


# ------------------------------------------------------------------------------------------ helpers


def read_version() -> str:
    """Return ``__version__`` from src/latkan/__init__.py (parsed, not imported: no Qt needed)."""
    text = (SRC / "latkan" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r"""^__version__\s*=\s*["']([^"']+)["']""", text, re.MULTILINE)
    if not match:
        raise BuildError("could not find __version__ in src/latkan/__init__.py")
    return match.group(1)


def run(cmd: Sequence[str | os.PathLike[str]], env: dict[str, str] | None = None) -> None:
    """Run ``cmd`` from the repository root, streaming output; raise CalledProcessError on failure."""
    args = [os.fspath(c) for c in cmd]
    log.info("$ %s", subprocess.list2cmdline(args) if os.name == "nt" else " ".join(args))
    subprocess.run(args, cwd=ROOT, env=env, check=True)


def _machine() -> str:
    """Normalised CPU architecture of the running interpreter (what PyInstaller builds for)."""
    machine = platform.machine().lower()
    return {"amd64": "x86_64", "x64": "x86_64", "arm64": "arm64", "aarch64": "aarch64"}.get(machine, machine)


def _require(path: Path, hint: str) -> Path:
    if not path.exists():
        raise BuildError(f"{path} not found - {hint}")
    return path


def _mb(path: Path) -> str:
    if path.is_dir():
        size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    else:
        size = path.stat().st_size
    return f"{size / 1_048_576:.1f} MB"


# -------------------------------------------------------------------------------------------- steps


def make_icons() -> None:
    env = dict(os.environ)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    run([sys.executable, ROOT / "scripts" / "make_icons.py"], env=env)


def pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401 - availability check only
    except ImportError as exc:
        raise BuildError('PyInstaller is not installed: python -m pip install -e ".[dev]"') from exc
    run(
        [
            sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm",
            "--distpath", DIST, "--workpath", BUILD / "pyinstaller", SPEC,
        ]
    )  # fmt: skip


# ------------------------------------------------------------------------------------------ Windows


def find_iscc() -> str | None:
    """Locate Inno Setup's command-line compiler (env ISCC, PATH, or the default install folders)."""
    explicit = os.environ.get("ISCC")
    if explicit and Path(explicit).is_file():
        return explicit
    for name in ("iscc", "ISCC.exe"):
        found = shutil.which(name)
        if found:
            return found
    bases = [os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        bases.append(str(Path(local) / "Programs"))
    for base in filter(None, bases):
        # newest major version first ("Inno Setup 6", a future "Inno Setup 7", ...)
        for candidate in sorted(Path(base).glob("Inno Setup */ISCC.exe"), reverse=True):
            if candidate.is_file():
                return str(candidate)
    return None


def _zip_dir(folder: Path, archive: Path) -> None:
    """Zip ``folder`` (as a top-level ``folder.name/`` directory) in a deterministic file order."""
    archive.unlink(missing_ok=True)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(folder.rglob("*")):
            zf.write(path, Path(folder.name) / path.relative_to(folder))


def package_windows(version: str, portable_zip: bool) -> list[Path]:
    _require(DIST / APP_NAME / f"{APP_NAME}.exe", "PyInstaller step did not produce the app")
    arch = {"x86_64": "x64", "arm64": "arm64"}.get(_machine(), _machine())
    artifacts: list[Path] = []

    iscc = find_iscc()
    if iscc:
        run([iscc, "/Qp", f"/DMyAppVersion={version}", f"/DMyAppArch={arch}", ISS])
        artifacts.append(
            _require(DIST / f"{APP_NAME}-{version}-windows-{arch}-setup.exe", "ISCC output missing")
        )
    else:
        log.warning(
            "Inno Setup (ISCC.exe) not found - building a portable zip instead. "
            "Install Inno Setup 6.3+ (e.g. `choco install innosetup`) for a setup.exe."
        )

    if portable_zip or not iscc:
        archive = DIST / f"{APP_NAME}-{version}-windows-{arch}.zip"
        _zip_dir(DIST / APP_NAME, archive)
        artifacts.append(archive)
    return artifacts


# -------------------------------------------------------------------------------------------- macOS


def package_macos(version: str) -> list[Path]:
    app = _require(DIST / f"{APP_NAME}.app", "PyInstaller step did not produce the .app bundle")
    identity = os.environ.get("MACOS_SIGN_IDENTITY", "").strip()

    if identity:
        entitlements = BUILD / "entitlements.plist"
        entitlements.parent.mkdir(parents=True, exist_ok=True)
        entitlements.write_bytes(plistlib.dumps(MAC_ENTITLEMENTS))
        log.info("Signing with identity %r (hardened runtime)", identity)
        run(
            [
                "codesign", "--force", "--deep", "--options", "runtime", "--timestamp",
                "--entitlements", entitlements, "--sign", identity, app,
            ]
        )  # fmt: skip
    else:
        log.info("MACOS_SIGN_IDENTITY not set - ad-hoc signing (users must right-click > Open once)")
        run(["codesign", "--force", "--deep", "--sign", "-", app])
    run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", app])

    dmg = DIST / f"{APP_NAME}-{version}-macos-{_machine()}.dmg"
    run(["bash", MAKE_DMG, app, dmg])
    if identity:
        run(["codesign", "--force", "--timestamp", "--sign", identity, dmg])
    return [_require(dmg, "make_dmg.sh did not produce the disk image")]


# -------------------------------------------------------------------------------------------- Linux


def _tar_filter(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """Strip build-machine ownership from archive members."""
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


# Must never ship in the Linux bundle: the GTK 3 platform theme would run the build machine's GTK/GLib
# next to the user's newer GIO/GTK/gdk-pixbuf modules (see _PRUNE in packaging/latkan.spec).
LINUX_FORBIDDEN = ("libqgtk3*", "libgtk-3.so*", "libgdk-3.so*")


def check_linux_bundle(folder: Path) -> None:
    """Fail when the onedir build still contains the GTK 3 theme plugin or GTK 3 itself."""
    found = sorted(
        path.relative_to(folder).as_posix()
        for path in folder.rglob("*")
        if any(fnmatch.fnmatchcase(path.name.lower(), pattern) for pattern in LINUX_FORBIDDEN)
    )
    if found:
        raise BuildError(
            f"{folder} bundles GTK 3 ({', '.join(found)}) - adjust _PRUNE in packaging/latkan.spec"
        )


def package_linux(version: str) -> list[Path]:
    folder = DIST / APP_NAME
    _require(folder / APP_NAME, "PyInstaller step did not produce the app")
    check_linux_bundle(folder)
    arch = _machine()

    tarball = DIST / f"{APP_NAME}-{version}-linux-{arch}.tar.gz"
    with tarfile.open(tarball, "w:gz") as tar:  # symlinks and file modes are preserved
        tar.add(folder, arcname=APP_NAME, filter=_tar_filter)
    artifacts = [tarball]

    # NB: an AppImage runtime also sets $APPIMAGE (to a path) - only explicit truthy values count here.
    forced = os.environ.get("APPIMAGE", "").strip().lower() in {"1", "true", "yes", "on"}
    if forced or shutil.which("appimagetool"):
        env = dict(os.environ)
        env.setdefault("ARCH", arch)
        run(["bash", BUILD_APPIMAGE, version], env=env)
        artifacts.append(_require(DIST / f"{APP_NAME}-{version}-{arch}.AppImage", "AppImage missing"))
    else:
        log.info("appimagetool not found and APPIMAGE!=1 - skipping the AppImage")
    return artifacts


# --------------------------------------------------------------------------------------------- main


def package(version: str, portable_zip: bool) -> list[Path]:
    if sys.platform == "win32":
        return package_windows(version, portable_zip)
    if sys.platform == "darwin":
        return package_macos(version)
    if sys.platform.startswith("linux"):
        return package_linux(version)
    raise BuildError(f"unsupported platform {sys.platform!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build Latkan for the current OS.")
    parser.add_argument("--skip-icons", action="store_true", help="reuse the icons already in assets/")
    parser.add_argument("--skip-pyinstaller", action="store_true", help="package an existing dist/ build")
    parser.add_argument("--no-package", action="store_true", help="stop after PyInstaller")
    parser.add_argument("--portable-zip", action="store_true", help="Windows: also create the zip")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="[build] %(message)s", force=True)

    try:
        version = read_version()
        log.info(
            "Latkan %s | %s %s | Python %s", version, sys.platform, _machine(), platform.python_version()
        )
        if not args.skip_icons:
            make_icons()
        if not args.skip_pyinstaller:
            pyinstaller()
        artifacts = [] if args.no_package else package(version, args.portable_zip)
    except subprocess.CalledProcessError as exc:
        log.error("command failed with exit code %s: %s", exc.returncode, exc.cmd)
        return exc.returncode or 1
    except (BuildError, OSError) as exc:
        log.error("%s", exc)
        return 1

    log.info("Done.")
    for artifact in artifacts:
        log.info("  %s  (%s)", artifact.relative_to(ROOT), _mb(artifact))
    return 0


if __name__ == "__main__":
    sys.exit(main())
