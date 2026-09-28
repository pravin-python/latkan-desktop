# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 6 spec for Latkan - one file for Windows, macOS and Linux.

Run from the repository root (scripts/build.py does this for you):

    python -m PyInstaller --clean --noconfirm packaging/latkan.spec

Produces a windowed *onedir* build:

    Windows  dist/Latkan/Latkan.exe            (+ _internal/)
    macOS    dist/Latkan.app                   (and the intermediate dist/Latkan/)
    Linux    dist/Latkan/Latkan                (+ _internal/)

Icons come from assets/ (generate them with `python scripts/make_icons.py`); a missing icon only
produces a warning so the spec still works on a fresh checkout.

UPX is disabled on purpose: it corrupts some Qt plugins and makes antivirus false positives on
Windows far more likely.
"""

import fnmatch
import importlib.util
import logging
import re
import sys
from pathlib import Path

log = logging.getLogger("latkan.spec")

# absolute(), not resolve(): building through a short junction/subst path (to stay under Windows'
# 260-character MAX_PATH with the deep _internal/PySide6/... tree) must keep using that short path.
ROOT = Path(SPECPATH).absolute().parent  # noqa: F821 - SPECPATH is injected by PyInstaller
SRC = ROOT / "src"
ASSETS = ROOT / "assets"
APP_NAME = "Latkan"
BUNDLE_ID = "in.latkan.app"

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"


def _read_version() -> str:
    """Read __version__ from src/latkan/__init__.py without importing the package (no Qt import)."""
    text = (SRC / "latkan" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r"""^__version__\s*=\s*["']([^"']+)["']""", text, re.MULTILINE)
    if not match:
        raise SystemExit("latkan.spec: could not find __version__ in src/latkan/__init__.py")
    return match.group(1)


VERSION = _read_version()


def _icon():
    """Per-OS icon path, or None (PyInstaller default icon) when it has not been generated."""
    name = "icon.ico" if IS_WIN else "icon.icns" if IS_MAC else "icon.png"
    path = ASSETS / name
    if path.is_file():
        return str(path)
    log.warning("latkan.spec: %s not found - run `python scripts/make_icons.py` for the real icon", path)
    return None


ICON = _icon()


def _macos_minimum() -> str:
    """Oldest macOS the bundle runs on: Latkan targets 12.0, but Qt >= 6.10 itself needs 13.0.

    Release CI pins PySide6-Essentials<6.10 on macOS so the published app keeps macOS 12 support;
    a local build against a newer Qt gets an honest LSMinimumSystemVersion instead of a crash.
    """
    import PySide6

    if tuple(PySide6.__version_info__[:2]) >= (6, 10):
        log.warning(
            "latkan.spec: PySide6 %s requires macOS 13+; install 'PySide6-Essentials<6.10' "
            "to build for macOS 12",
            PySide6.__version__,
        )
        return "13.0"
    return "12.0"


# ---------------------------------------------------------------------------------------- modules

hiddenimports = [
    # platform backends are picked at runtime by latkan.platform.get_platform()
    "latkan.platform",
    "latkan.platform.base",
    "latkan.platform.windows",
    "latkan.platform.macos",
    "latkan.platform.linux",
    # QLocalServer / QLocalSocket (single-instance guard)
    "PySide6.QtNetwork",
]
if IS_MAC:
    # PyObjC is imported lazily inside latkan.platform.macos; list it explicitly when installed.
    hiddenimports += [m for m in ("objc", "AppKit", "Foundation") if importlib.util.find_spec(m)]

_UNUSED_QT = [
    # QtWebEngine*
    "QtWebEngine",
    "QtWebEngineCore",
    "QtWebEngineWidgets",
    "QtWebEngineQuick",
    "QtWebChannel",
    "QtWebSockets",
    "QtWebView",
    # Qt3D*
    "Qt3DAnimation",
    "Qt3DCore",
    "Qt3DExtras",
    "Qt3DInput",
    "Qt3DLogic",
    "Qt3DRender",
    # QtQuick* / QML
    "QtQuick",
    "QtQuick3D",
    "QtQuickControls2",
    "QtQuickTest",
    "QtQuickWidgets",
    "QtQml",
    # media, charts, data
    "QtMultimedia",
    "QtMultimediaWidgets",
    "QtSpatialAudio",
    "QtCharts",
    "QtDataVisualization",
    "QtGraphs",
    "QtPdf",
    "QtPdfWidgets",
    "QtSql",
    # devices / misc
    "QtTest",
    "QtBluetooth",
    "QtNfc",
    "QtPositioning",
    "QtLocation",
    "QtSensors",
    "QtSerialPort",
    "QtSerialBus",
    "QtRemoteObjects",
    "QtScxml",
    "QtStateMachine",
    "QtTextToSpeech",
    "QtHttpServer",
    # tooling
    "QtDesigner",
    "QtHelp",
    "QtUiTools",
]
excludes = [f"PySide6.{m}" for m in _UNUSED_QT] + ["tkinter", "_tkinter", "PyQt5", "PyQt6"]

datas = [(str(ROOT / "LICENSE"), ".")]

# --------------------------------------------------------------------------------------- targets

a = Analysis(  # noqa: F821 - PyInstaller spec globals
    [str(SRC / "latkan" / "__main__.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

# ------------------------------------------------------------------------------ prune dead weight

# PyInstaller's Qt hooks collect every plugin of the used modules. Some of them drag in large
# libraries Latkan never uses; with the full PySide6 (Addons) installed, the on-screen virtual
# keyboard input context alone pulls the whole QtQuick/QML/OpenGL stack (~25 MB on Windows).
# Patterns match the lower-case destination path inside the bundle ("/" separators).
_PRUNE = [
    "*/plugins/platforminputcontexts/*virtualkeyboard*",  # -> Qt6VirtualKeyboard -> Qt6Quick -> Qml
    "*qt6virtualkeyboard*",
    "*qtvirtualkeyboard.framework/*",
    "*qt6quick*",
    "*qtquick*.framework/*",
    "*qt6qml*",
    "*qtqml*.framework/*",
    "*/plugins/tls/*",  # QtNetwork TLS backends: Latkan only uses local sockets
    "*/plugins/networkinformation/*",
    "*/plugins/generic/*tuio*",  # TUIO network touch input
    "*/plugins/imageformats/*qpdf*",  # PDF "images" (-> Qt6Pdf); release builds never had it
    "*qt6pdf*",
    "*qtpdf.framework/*",
    "pyside6/translations/*",  # Latkan installs no QTranslator
    "pyside6/qt/translations/*",
]
if IS_WIN:
    _PRUNE += [
        "pyside6/opengl32sw.dll",  # software OpenGL fallback; Latkan paints with the raster engine
        "pyside6/qt6opengl.dll",  # only QtQuick links it (verified with the import check below)
        "pyside6/plugins/platforms/qdirect2d.dll",  # alternative QPA plugin, never selected
        "libcrypto-3-x64.dll",  # PySide6's OpenSSL for the TLS plugins (Python's own is -1_1 / -3)
        "libssl-3-x64.dll",
    ]
elif not IS_MAC:
    # The GTK 3 platform theme links the build machine's GTK/GLib stack (Ubuntu 22.04 in release CI),
    # which ldd resolves and PyInstaller bundles. Qt loads that theme first on GNOME, Unity, Cinnamon,
    # MATE and XFCE, so the old GTK/GLib would then dlopen the user's newer GIO modules (gvfs, dconf),
    # GTK modules and gdk-pixbuf loaders, and the native file chooser ("From your image...") can crash
    # the app. Without the plugin Qt uses its built-in GNOME/generic theme and the Qt file dialog.
    _PRUNE += [
        "*/plugins/platformthemes/*qgtk3*",
        # Libraries only that plugin linked; they stay collected after the plugin is dropped. System
        # libraries go to the top level, so the basename is the destination. libglib-2.0 and
        # libgthread-2.0 stay because QtCore's GLib event dispatcher can link them. libgio, libgobject and
        # libgmodule stay as well: without GTK nothing loads them.
        "libgtk-3.so*",
        "libgdk-3.so*",
        "libgdk_pixbuf-2.0.so*",
        "libatk-1.0.so*",
        "libatk-bridge-2.0.so*",
        "libatspi.so*",
        "libpango*-1.0.so*",
        "libcairo*.so*",
        "libepoxy.so*",
    ]


def _prune(toc, what):
    kept, dropped = [], []
    for entry in toc:
        dest = entry[0].replace("\\", "/").lower()
        (dropped if any(fnmatch.fnmatchcase(dest, pat) for pat in _PRUNE) else kept).append(entry)
    if dropped:
        log.info("latkan.spec: pruned %d unused %s", len(dropped), what)
    return kept, dropped


def _check_no_dangling(binaries, dropped):
    """Fail the build if a kept binary links against one we pruned (keeps _PRUNE honest on every OS)."""
    from PyInstaller.depend import bindepend

    gone = {Path(entry[0]).name.lower() for entry in dropped}
    for dest, src, typecode in binaries:
        if typecode not in ("BINARY", "EXTENSION"):
            continue
        try:
            imports = bindepend.get_imports(src)
        except Exception as exc:  # analysis is best effort; the build itself already succeeded
            log.warning("latkan.spec: could not analyse %s: %s", src, exc)
            continue
        missing = sorted(name for name, _path in imports if Path(name).name.lower() in gone)
        if missing:
            raise SystemExit(f"latkan.spec: {dest} links against pruned {missing}; adjust _PRUNE")


a.binaries, _dropped_binaries = _prune(a.binaries, "binaries")
a.datas, _dropped_datas = _prune(a.datas, "data files")
_check_no_dangling(a.binaries, _dropped_binaries)

pyz = PYZ(a.pure)  # noqa: F821


def _windows_version_info():
    """Explorer 'Details' tab metadata for Latkan.exe (Windows only)."""
    from PyInstaller.utils.win32 import versioninfo as vi

    nums = [int(n) for n in re.findall(r"\d+", VERSION)[:4]]
    nums += [0] * (4 - len(nums))
    strings = [
        vi.StringStruct("CompanyName", "Latkan contributors"),
        vi.StringStruct("FileDescription", "Latkan - swinging desktop charms"),
        vi.StringStruct("FileVersion", VERSION),
        vi.StringStruct("InternalName", APP_NAME),
        vi.StringStruct("LegalCopyright", "Copyright (c) 2026 Latkan contributors. MIT License."),
        vi.StringStruct("OriginalFilename", f"{APP_NAME}.exe"),
        vi.StringStruct("ProductName", APP_NAME),
        vi.StringStruct("ProductVersion", VERSION),
    ]
    return vi.VSVersionInfo(
        ffi=vi.FixedFileInfo(filevers=tuple(nums), prodvers=tuple(nums)),
        kids=[
            vi.StringFileInfo([vi.StringTable("040904B0", strings)]),
            vi.VarFileInfo([vi.VarStruct("Translation", [0x0409, 1200])]),
        ],
    )


exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # windowed: no console on Windows, no Terminal on macOS
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,  # native arch of the build machine (release CI builds arm64 and x86_64 separately)
    codesign_identity=None,  # scripts/build.py signs the finished .app
    entitlements_file=None,
    icon=ICON,
    version=_windows_version_info() if IS_WIN else None,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)

if IS_MAC:
    app = BUNDLE(  # noqa: F821
        coll,
        name=f"{APP_NAME}.app",
        icon=ICON,
        bundle_identifier=BUNDLE_ID,
        version=VERSION,
        info_plist={
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": APP_NAME,
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSUIElement": True,  # agent app: no Dock icon, no app menu bar
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": _macos_minimum(),
            "LSApplicationCategoryType": "public.app-category.entertainment",
            "NSHumanReadableCopyright": "Copyright (c) 2026 Latkan contributors. MIT License.",
        },
    )
