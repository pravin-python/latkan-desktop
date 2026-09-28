# Latkan — Architecture & Module Contracts

Latkan hangs small "charms" from the top edge of the screen on ropes simulated with
real physics. It runs on **Windows 10+, macOS 12+ (Intel & Apple Silicon) and Linux
(Ubuntu 22.04+; X11, and Wayland via XWayland)** from one Python codebase.

This document is the contract every module is written against. Names, signatures and
behaviours here are binding; if you must deviate, update this file.

## 1. Technology decisions

| Concern | Decision | Why |
|---|---|---|
| Language | Python ≥ 3.10 | one codebase, fast iteration |
| GUI | **PySide6 ≥ 6.5** (Qt 6, LGPL) | native transparent always-on-top windows, tray, HiDPI and multi-monitor on all 3 OSes; official wheels for Win x64, macOS universal2, Linux x64/arm64 |
| macOS native tweaks | `pyobjc-framework-Cocoa` (darwin only), optional at runtime | window level, all Spaces, no Dock icon, non-activating clicks, explicit click-through + hover via NSEvent monitors |
| Windows native tweaks | `ctypes` + `winreg` (stdlib) | no extra deps |
| Linux native tweaks | force `xcb` on Wayland; `ctypes` + system `libX11`/`libXfixes`, optional at runtime (compositor detection) | Wayland clients cannot position windows or stay on top |
| Packaging | PyInstaller (onedir) per OS | `.exe` + Inno Setup installer, `.app` + `.dmg`, AppImage + `.tar.gz` |
| CI | GitHub Actions matrix (ubuntu/windows/macos) | lint + tests on each OS; tagged release builds |
| Tests | pytest; Qt tests use `QT_QPA_PLATFORM=offscreen` | headless everywhere |
| Lint | ruff | |

Import rule: **`physics.py` and `config.py` must not import Qt.** They are pure Python so
they are trivially testable and reusable.

## 2. Layout

```
latkan/
  pyproject.toml            package metadata, deps, ruff+pytest config, entry point `latkan`
  README.md  LICENSE  .gitignore  .gitattributes
  docs/ARCHITECTURE.md
  src/latkan/
    __init__.py             __version__ = "1.0.0", APP_NAME = "Latkan", APP_ID = "in.latkan.app"
    __main__.py             `python -m latkan` -> app.main()
    app.py                  entry point: env prep, QApplication, single instance, logging, wiring
    physics.py              pure-Python Verlet rope simulation
    config.py               settings dataclasses, paths, atomic JSON load/save, migration
    charms.py               built-in charm artwork (QPainter), pixmap cache, custom image import
    rendering.py            rope styles + charm body drawing, hit geometry helpers
    overlay.py              OverlayWindow (one per screen) + OverlayManager (owns sim + windows)
    menu.py                 tray menu and per-charm context menu builders
    singleinstance.py       QLocalServer/QLocalSocket single-instance guard
    platform/
      __init__.py           get_platform() -> PlatformIntegration for the running OS
      base.py               PlatformIntegration base class (safe no-op defaults)
      windows.py  macos.py  linux.py
  tests/
    test_physics.py  test_config.py  test_autostart.py  test_overlay_smoke.py  test_app.py
    test_rendering.py  test_menu.py  test_singleinstance.py  test_packaging.py
  scripts/
    make_icons.py           renders the nazar charm to assets/icon.png/.ico/.icns
    build.py                cross-platform build driver (icons -> PyInstaller -> OS package)
  assets/                   generated icons (icon.png, icon.ico, icon.icns)
  packaging/
    latkan.spec             PyInstaller spec (all OSes)
    windows/latkan.iss      Inno Setup installer script
    macos/                  dmg helper (hdiutil), entitlements note
    linux/latkan.desktop  linux/build_appimage.sh  linux/install-from-source.sh
  .github/workflows/ci.yml  .github/workflows/release.yml
```

## 3. Module contracts

### 3.1 `physics.py` (no Qt)

```python
@dataclass
class PhysicsParams:
    gravity: float = 1800.0        # px/s^2, +y is down
    damping: float = 0.993         # per fixed step velocity retention
    segments: int = 12
    iterations: int = 20           # constraint passes per step
    end_inv_mass: float = 0.25     # charm end is 4x heavier than a rope node
    dt: float = 1 / 120            # fixed timestep (s)
    max_substeps: int = 8          # cap per advance() to avoid spiral of death

class Rope:
    def __init__(self, anchor_x: float, anchor_y: float, length: float,
                 params: PhysicsParams | None = None, initial_offset: float = 0.0): ...
    # initial_offset: horizontal skew of the initial straight line so a new rope swings in.
    anchor_x: float; anchor_y: float          # pinned first node (writable; next step honours it)
    length: float                              # property; setter clamps to >= 10 and rescales segment length
    points: list[tuple[float, float]]          # read-only snapshot property, segments+1 nodes
    def step(self) -> None                     # exactly one fixed dt step
    def grab(self, x: float, y: float) -> None # end node pinned to target (clamped to <= length from anchor)
    def move_grab(self, x: float, y: float) -> None
    def release(self) -> None                  # end keeps its velocity (Verlet prev); leftover drag stretch is
                                               # projected out with prev shifted along (no node's velocity changes)
    is_grabbed: bool                           # property
    def nudge(self, vx: float, vy: float = 0.0) -> None  # add velocity (px/s) to all non-pinned nodes,
                                                          # scaled linearly 0..1 from anchor to end
    def apply_force(self, fx: float, fy: float) -> None  # extra acceleration px/s^2 for next step only (breeze)
    def end(self) -> tuple[float, float]
    def end_angle(self) -> float               # radians; 0 = hanging straight down; + when end is to +x of the node above
    def speed(self) -> float                   # max node speed in px/s (0 when at rest)
    def settled(self, threshold: float = 1.5) -> bool  # not grabbed and speed() < threshold, and hanging
                                                       # (near) straight down (a swing's turning point is
                                                       # also slow; speed alone would freeze a tilted charm)
    def relocate(self, anchor_x: float, anchor_y: float) -> None  # move anchor + whole rope, keep shape/velocity
    segment_length: float                      # read-only; MIN_LENGTH = 10.0 module constant

class Simulation:
    def __init__(self, params: PhysicsParams | None = None)
    params: PhysicsParams
    def add(self, rope: Rope) -> None; def remove(self, rope: Rope) -> None
    ropes: list[Rope]
    def advance(self, elapsed: float) -> int   # accumulate real time, run fixed steps (<= max_substeps), return steps run;
                                               # leftover time > max_substeps*dt is dropped
    def settled(self) -> bool                  # all ropes settled
    def reset_clock(self) -> None              # clear accumulator (call when resuming from idle)
```
Constraint solve: distance constraints with inverse masses; anchor node inverse mass 0;
grabbed end inverse mass 0. Numerically stable for lengths 40–1500 px. Constraints are unilateral
(a thread resists stretching, goes slack under compression) plus a final follow-the-leader pass for
free ropes; a grabbed rope (both ends pinned) gets 6 alternating unilateral follow-the-leader passes
instead, from the anchor and from the grabbed end, moving only the inner nodes (a 3000 px/s drag
stretches it by a few % instead of 15-25 %). `initial_offset` is in px (clamped to 95 % of the
length). An anchor that jumps farther than max(length, 250 px) in one step translates the whole rope. Non-finite input is ignored and
logged; a rope whose state goes non-finite resets to hanging at rest. A rope that has just settled
may still sway by a fraction of a pixel and cross the threshold again while other ropes keep the
loop running; `settled()` is not sticky.

### 3.2 `config.py` (no Qt)

```python
SCHEMA_VERSION = 1
ROPE_STYLES = ("gold", "silver", "neon", "kalava")
SCALES = (0.6, 0.8, 1.0, 1.3, 1.6)
BUILTIN_NAMES = ("nazar", "heart", "star", "moon", "clover", "dreamcatcher")

@dataclass
class CharmSpec:
    id: str                       # uuid4 hex, stable identity
    kind: str                     # "builtin" | "image"
    name: str | None = None       # builtin name (kind == builtin)
    image: str | None = None      # file name inside charms_dir() (kind == image), never an absolute path
    x_frac: float = 0.5           # anchor x as fraction of its screen width (0..1) -> resolution independent
    length: float = 150.0         # rope length in logical px
    screen: str | None = None     # QScreen.name() it hangs on; None/unknown -> primary
    @staticmethod
    def new_builtin(name, x_frac, length, screen=None) -> "CharmSpec"
    @staticmethod
    def new_image(image, x_frac, length, screen=None) -> "CharmSpec"

@dataclass
class Settings:
    version: int = SCHEMA_VERSION
    rope_style: str = "gold"
    scale: float = 1.0
    breeze: bool = False          # gentle idle sway (keeps animation running)
    hover_swing: bool = True      # cursor sweeping through a charm pushes it
    hidden: bool = False
    charms: list[CharmSpec] = field(default_factory=list)
    @staticmethod
    def default() -> "Settings"   # nazar at x_frac .72 len 170, star at .80 len 110

def config_dir() -> Path    # Windows: %APPDATA%\Latkan ; macOS: ~/Library/Application Support/Latkan ;
                            # Linux: $XDG_CONFIG_HOME/latkan or ~/.config/latkan. Env LATKAN_HOME overrides all (tests).
def charms_dir() -> Path    # config_dir()/"charms"
def log_dir() -> Path       # config_dir()/"logs"
def settings_path() -> Path # config_dir()/"settings.json"
def load_settings(path: Path | None = None) -> Settings
    # missing -> Settings.default(); corrupt JSON/invalid -> rename to settings.json.bak-<n>, return default;
    # validates & clamps every field (unknown rope -> gold, scale snapped into range 0.3..3, x_frac 0..1,
    # length 40..3000, unknown builtin dropped, image charm whose file is missing dropped); ignores unknown keys;
    # migrates legacy v0 format {"rope","scale","charms":[{"kind","name"|"path","x","len"}]} (x in px ->
    # x_frac assuming 1920 width; legacy "path" images are copied into charms_dir()).
    # unreadable (OSError other than missing: permissions, a directory, a lock) -> defaults, file left
    # alone and remembered; on Windows a PermissionError (a sharing violation looks like that; not for
    # a directory) is retried first for 50..250 ms (~0.75 s in all), like the write's os.replace.
def save_settings(settings: Settings, path: Path | None = None) -> None
    # atomic: write temp file in same dir, fsync, os.replace; creates dirs. Raises OSError (callers catch).
    # A file load_settings() could not read (nor move aside when corrupt) is first copied to .bak-<n>;
    # if that copy fails too, OSError and the file is not replaced (a transient lock keeps the user's
    # settings as the backup; a permanent EACCES, e.g. root-owned 0600, is never overwritten).
def backup_settings(path: Path | None = None) -> Path | None   # move aside to .bak-<n> (used by --reset)
# also: KIND_BUILTIN, KIND_IMAGE, SCALE_MIN/MAX (0.3/3.0), LENGTH_MIN/MAX (40/3000)
```

### 3.3 `platform/` (Qt allowed, imported lazily inside methods where native)

```python
class PlatformIntegration:          # base.py — every method safe no-op by default
    name: str = "generic"
    def prepare_environment(self) -> None            # BEFORE QApplication is created
    def configure_application(self, app) -> None     # after QApplication
    def overlay_window_flags(self) -> Qt.WindowType  # base: Frameless|StaysOnTop|Tool|WindowDoesNotAcceptFocus
    def configure_overlay(self, widget) -> None      # after widget is shown (winId valid)
    def overlay_geometry(self, screen) -> QRect      # base: screen.geometry()
    def anchor_top_inset(self, screen) -> int        # base 0: px at the overlay top where another program
                                                     # takes the clicks; the rope-top drag zone gets this deeper
    def uses_window_mask(self) -> bool               # base True; False = the window system hit-tests the overlay
                                                     # per pixel by alpha (Windows layered windows), so the overlay
                                                     # sets no mask and its 1/255 hit pads are the input area
    def compositing_active(self) -> bool             # base True; False = transparent pixels would show black
                                                     # (X11 without a compositor), overlay shapes its mask instead
    def watch_compositing(self, callback | None)     # base no-op; calls back (GUI thread) when the answer may change
    def dialogs_stay_on_top(self) -> bool            # base False; menu.message_box() then adds WindowStaysOnTopHint
    def tray_icon_is_mask(self) -> bool              # base False
    def activate_app(self) -> None                   # before a dialog; base no-op, macOS activates the
                                                     # accessory app so dialogs are not hidden behind others;
                                                     # Linux/X11 refreshes Qt's X server time (focus stealing)
    def foreground(self) -> ContextManager[None]     # `with platform.foreground(): dialog()` - activate_app()
                                                     # (errors logged), then the block; used by every dialog
                                                     # and the per-charm menu
    in_foreground: bool                              # property: True while such a block runs (nests), so macOS
                                                     # can tell Latkan's own activations from a relaunch
    # autostart ("Start at login")
    def autostart_supported(self) -> bool
    def autostart_enabled(self) -> bool
    def set_autostart(self, enabled: bool) -> None   # raises OSError on failure
    def launch_command(self) -> list[str]            # frozen: [sys.executable]; source: [python, "-m", "latkan"]
                                                     # (on macOS frozen: path inside .app is fine for LaunchAgent)

def get_platform() -> PlatformIntegration          # platform/__init__.py, cached singleton by sys.platform
def clear_cache() -> None                           # tests
# base.py helpers: qpa_platform_name(), windowless_python(), atomic_write_bytes(), one_pixel_short(screen);
# AUTOSTART_FLAG = "--autostart" (what the macOS login item passes, see 3.9)
```
Native window tweaks (`configure_overlay`) run only on the OS's real Qt plugin (`windows`, `cocoa`):
headless plugins (`offscreen`, `minimal`) hand out fake window ids that must never reach user32/AppKit.
Likewise Linux's X11 workarounds (`compositing_active`, `watch_compositing`, `activate_app`,
`dialogs_stay_on_top`) act only on `xcb`.
Per-OS behaviour:
- **Windows** (`windows.py`): `overlay_geometry` = screen geometry with height − 1 (`one_pixel_short`;
  prevents Windows treating a topmost full-screen window as a full-screen app → taskbar/notification
  side effects).
  `configure_overlay` adds `WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW` via ctypes `GetWindowLongPtrW/SetWindowLongPtrW`
  (guarded, never raises). `uses_window_mask` is False on the real `windows` plugin: Qt makes the frameless
  translucent overlay `WS_EX_LAYERED` and flushes it with `UpdateLayeredWindowIndirect(ULW_ALPHA)`, and
  Windows hit-tests such a window per pixel (alpha 0 passes clicks through), so a mask (`SetWindowRgn`
  with a redraw on every changed frame, 5-18 % of a core while animating) adds nothing.
  Autostart: `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` value `Latkan`
  = command line (`subprocess.list2cmdline`: quoted when it contains spaces). `autostart_enabled` also
  honours the Task Manager "disabled" flag (`Explorer\StartupApproved\Run`), which `set_autostart`
  clears. Registry access through a small injectable backend (`get/set/delete(subkey, name)`).
- **macOS** (`macos.py`): `configure_application` sets activation policy Accessory (no Dock icon) via
  PyObjC if available. `overlay_geometry` = the screen **below the menu bar**: x, width and bottom from
  `screen.geometry()`, only the top from `availableGeometry()` (which also excludes the Dock); ropes hang
  from the menu bar's bottom edge, so no rope, charm or anchor-drag zone covers menu titles, status items
  or the notch (auto-hiding menu bar: the tops are equal, the whole screen is used). `overlay_window_flags`
  = base flags; `configure_overlay` also sets widget attribute `Qt.WA_MacAlwaysShowToolWindow`, and via
  PyObjC (from `int(widget.winId())` → NSView → window): `setLevel_(NSStatusWindowLevel)`,
  `setCollectionBehavior_(CanJoinAllSpaces | Stationary | IgnoresCycle | FullScreenAuxiliary)`,
  `setHidesOnDeactivate_(False)`, `setHasShadow_(False)`, and makes the panel **non-activating**
  (`styleMask | NSWindowStyleMaskNonactivatingPanel`, plus the private `_setPreventsActivation:` when
  the window responds to it; NSPanel only; Qt keeps that bit when it recomputes the mask), so clicking or
  dragging a charm never takes keyboard focus from the frontmost app (`WindowDoesNotAcceptFocus` only
  refuses key status). **Input is explicit** on macOS: a Qt mask does not create click-through on Cocoa
  (points outside it go to Latkan's own responder chain) and WindowServer alpha hit-testing is
  undocumented, so `configure_overlay` hands each overlay to a pointer tracker that sets
  `ignoresMouseEvents_(True)` and flips it to NO only while `OverlayWindow.track_pointer(QCursor.pos())`
  says a click there is Latkan's (a `charm_at` target, or any time during a drag). The cursor comes from
  one NSEvent global monitor (`MouseMoved`, moves over other apps) plus one local monitor (`MouseMoved |
  LeftMouseDragged`, returns the event) shared by all overlays (mouse monitors need no Accessibility
  permission); the handlers only start a Qt timer (≤ 60 Hz, the last move always evaluated; a 50 ms
  re-check runs while an overlay takes input), so no Qt code runs inside AppKit dispatch and errors never
  reach it. Each update compares the real NSWindow value (Qt rewrites it only in `setWindowFlags`, which
  the overlay never triggers, but a rewrite heals on the next move), re-applies the window settings when
  the NSWindow number changes (re-created window), re-checks right after every show, and sets YES on any
  error (charms unclickable beats a window swallowing the screen). The same updates drive hover swing,
  because Qt drops button-less mouse moves while the app is inactive (which it nearly always is). The
  monitors are removed with the last overlay widget. If they cannot be installed, `ignoresMouseEvents`
  is never touched and the old alpha hit-testing (1/255 pads) applies. The overlay's Qt window flags must
  never change after creation. Missing PyObjC → log a warning, continue. `tray_icon_is_mask` True.
  Autostart: LaunchAgent plist `~/Library/LaunchAgents/in.latkan.app.plist` (`Label`, `ProgramArguments`
  = `launch_command() + ["--autostart"]`, `RunAtLoad`=true, `ProcessType`=Interactive,
  `LimitLoadToSessionType`=Aqua) written with `plistlib`; home dir injectable. Enabling it for a frozen
  app that runs translocated (`/AppTranslocation/` in the path) or from a read-only volume (a mounted
  DMG: `statvfs().f_flag & ST_RDONLY`) raises `OSError("Move Latkan to the Applications folder first, …")`
  (external drives are fine); `autostart_enabled` is False when `ProgramArguments[0]` no longer exists
  (moved, renamed, ejected), since launchd would fail silently at login.
  `WA_MacAlwaysShowToolWindow` is also set by the overlay before its first `show()` (Qt reads it when the
  NSWindow is created). `activate_app` = `NSApp.activateIgnoringOtherApps_(True)`.
- **Linux** (`linux.py`): `prepare_environment`: if `WAYLAND_DISPLAY` set and `QT_QPA_PLATFORM` unset →
  set `QT_QPA_PLATFORM="xcb;wayland"` (only if `DISPLAY` is also set, i.e. XWayland exists; else log a
  warning). The `;wayland` fallback keeps Latkan running if the xcb plugin cannot load.
  Flags: base | `Qt.X11BypassWindowManagerHint` (override-redirect: stays above panels, no WM decoration).
  `overlay_geometry` = screen geometry with height − 1 (`one_pixel_short`): Mutter 50 (GNOME 50) counts
  the topmost override-redirect window as full-screen when its frame rect equals the monitor rect, and
  then hides the top bar (clock, tray) and the dock on that monitor while it is mapped; elsewhere it
  only costs one unused row (ropes are capped at 0.85 of the height).
  `anchor_top_inset` = `availableGeometry().top() − geometry().top()` (capped at height/4) only for
  GNOME Shell on Xorg (`XDG_SESSION_TYPE=x11` and `GNOME` in `XDG_CURRENT_DESKTOP`, not Flashback): the
  Shell sets its input region, top bar included, on the Composite overlay window, which lies above
  override-redirect windows too, so presses in that strip reach the Shell although the charms are
  drawn over it; the drag zone keeps its full 36 px below the bar. 0 on Wayland (Clutter picks by
  stacking, the overlay is above the panel there) and on other desktops.
  `compositing_active` (xcb only; Wayland sessions/XWayland always composite): whether some client
  owns the EWMH `_NET_WM_CM_S<screen>` selection, asked over a private Xlib connection (ctypes
  `libX11.so.6`, never Qt's; any failure = assume composited). A warning is logged once when there
  is none. `watch_compositing` selects XFixes selection-owner events on that connection (ctypes
  `libXfixes.so.3`) and watches its socket with a `QSocketNotifier`, so a compositor that starts or
  stops (KWin suspends compositing for full-screen games) is noticed while the charms rest; without
  XFixes the overlay still re-checks on every show and animation start.
  `activate_app` (xcb only): tray clicks reach Latkan over D-Bus (StatusNotifierItem/DBusMenu), not
  as X events, so Qt's last X server time - which it writes into a new window's `_NET_WM_USER_TIME`
  and `_NET_ACTIVE_WINDOW` request - may be minutes old and Mutter/KWin focus-stealing prevention
  would stack the dialog below the active window. It retitles a never-shown helper `QWindow` (an X
  `ChangeProperty` → `PropertyNotify`, whose timestamp Qt adopts) and runs `QGuiApplication.sync()`
  twice (Qt reads X events on a separate thread). Nothing is raised. `dialogs_stay_on_top` = True on
  xcb, in case a dialog is refused focus anyway.
  Autostart: `$XDG_CONFIG_HOME/autostart/latkan.desktop` (Exec = launch_command quoted per the Desktop
  Entry spec; for a frozen AppImage build use `$APPIMAGE` as the executable). `Hidden=true` /
  `X-GNOME-Autostart-enabled=false` count as off. Config home injectable for tests.

### 3.4 `charms.py` (Qt)

```python
BUILTIN_LABELS: dict[str, str]            # name -> human label, keys == config.BUILTIN_NAMES
def builtin_pixmap(name: str, size: int = 256) -> QPixmap   # cached per (name,size); transparent bg
def charm_pixmap(spec: CharmSpec) -> QPixmap                 # builtin or loaded image; fallback nazar; cached by spec.id
def forget(spec_id: str) -> None                             # drop cache entry
def import_image(src: Path) -> str
    # validate with QImageReader (max 40 MB file, max 8000x8000), convert to ARGB32, downscale so the long
    # edge <= 512 (smooth), save PNG into charms_dir() as "<uuid>.png"; return the file name. Raises ValueError
    # with a user-readable message on failure.
def delete_image(image: str) -> None                         # remove file from charms_dir() if present
def app_icon() -> QIcon                                      # nazar at 16..256 sizes
def tray_icon(mask: bool = False) -> QIcon                   # app_icon(), or a monochrome template glyph
                                                             # (setIsMask) for the macOS menu bar
# CHARM_PIXMAP_PX = 384: resolution built-ins are rendered at for hanging charms
```
Artwork is original vector drawing (nazar, heart, star, moon, clover, dreamcatcher) — no third-party IP.

### 3.5 `rendering.py` (Qt)

```python
CHARM_BASE_PX = 96          # charm width at scale 1.0 (logical px)
HOOK_PX = 7                 # distance from rope end to top of charm image (a small ring is drawn there)
def rope_path(points) -> QPainterPath                    # smooth quadratic curve through midpoints
def draw_rope(p: QPainter, points, style: str) -> None   # gold | silver (chain links) | neon (glow) | kalava (red+gold)
def charm_size(pix: QPixmap, scale: float) -> tuple[float, float]   # width = CHARM_BASE_PX*scale, keep aspect
def charm_transform(end: tuple[float,float], angle: float) -> QTransform  # translate(end) then rotate(-deg(angle))
def draw_charm(p, pix, end, angle, scale, style) -> None # ring at hook + pixmap below, rotated with the rope
def charm_local_rect(pix, scale) -> QRectF               # QRectF(-w/2, 0, w, h + HOOK_PX) in charm-local coords
def charm_hit(pos: QPointF, pix, end, angle, scale) -> bool
def rope_bounds(points, pad: float) -> list[QRect]       # one padded rect per segment (for masks)
def charm_bounds(pix, end, angle, scale, pad: float) -> QRect  # bounding rect of rotated charm
```

### 3.6 `overlay.py` (Qt) — the core runtime

```python
class HangingCharm:                 # runtime pairing of spec + rope + pixmap
    spec: CharmSpec; rope: Rope; pix: QPixmap; window: "OverlayWindow"

class OverlayWindow(QWidget):       # one per QScreen, covers platform.overlay_geometry(screen)
    def __init__(self, manager, screen): ...
    # WA_TranslucentBackground, WA_ShowWithoutActivating, WA_NoSystemBackground, setMouseTracking(True)
    # flags from platform.overlay_window_flags(); platform.configure_overlay(self) after first show
    screen_name: str
    charms: list[HangingCharm]      # rope coordinates are window-local logical px, anchor_y = 0
    def update_mask(self, exact=True) -> bool  # input region = union of rope_bounds(pad 9) + charm_bounds(pad 6)
                                    # + each rope's top drag zone (±14 px, anchor_depth deep) as QRegion of rects.
                                    # Applied as the window mask only where platform.uses_window_mask() (or
                                    # there is no compositor); else no mask is set and the region only decides
                                    # hide-when-empty and the repaint area. exact=False (animation frames): the
                                    # mask pushed last is kept while it contains the region, else one 24 px
                                    # roomier (MASK_SLACK_PX) is pushed - X11 (XShape -> ShapeNotify for the
                                    # compositor) and macOS (a new CAShapeLayer) pay for every new mask. At rest
                                    # (a window's last animated frame, the loop stopping, any mutation) the exact
                                    # region is pushed.
                                    # No compositor (platform.compositing_active() False, X11): the X server
                                    # would show every transparent pixel in that mask as black, so the mask is
                                    # the painted pixels instead (each charm's rope + body rendered at logical
                                    # resolution into an ARGB32 QImage, createAlphaMask(): alpha >= 128,
                                    # cached per charm while it rests), like a classic shaped window.
                                    # Always exact without a compositor (slack pixels would show black).
                                    # If no charms: hide() (an empty mask means "no mask" = would block the screen).
                                    # Repaints cover the previous + current padded area (without a mask nothing
                                    # else clears stale pixels).
    anchor_depth: float             # 36 px + platform.anchor_top_inset(screen); refreshed on
                                    # geometryChanged / availableGeometryChanged
    composited: bool                # set_composited(b): re-queried when the window is about to show and on
                                    # every animation start, and on platform.watch_compositing() callbacks
    # paintEvent first paints every click target with alpha 1/255: Windows layered windows let clicks
    # through fully transparent pixels regardless of the mask; this makes all OSes hit-test alike, and on
    # Windows (no mask) the pads are the input area (macOS decides input explicitly via track_pointer; the
    # pads only matter there without event monitors).
    # Not without a compositor: the pads would show as black boxes; the shaped mask decides input there.
    # paintEvent: clear, draw every rope then its charm (antialias + smooth pixmap)
    # mouse: left press on charm -> grab (ClosedHand cursor); on rope top (<= anchor_depth from y=0 and within
    #   14 px of anchor_x) -> drag anchor (x clamped, persisted as x_frac on release); double-click -> nudge;
    #   right-click -> menu.charm_menu (exec'd inside platform.foreground()); hover move (hover_swing)
    #   through a charm -> nudge with cursor velocity (clamped to 1500 px/s, scaled 0.35)
    def dispose(self) -> None       # retire + drop charms + deleteLater(); the manager's only way to delete
                                    # a window. While in_menu (show_charm_menu()'s exec(), and any dialog
                                    # opened from that menu, run nested event loops inside mousePressEvent,
                                    # and Qt runs a deleteLater() posted in a nested loop within that loop)
                                    # the delete waits until show_charm_menu() returns
    in_menu: bool                   # property: a show_charm_menu() call is on the stack (counted, nests)
    def hover(self, pos: QPointF, now: float) -> str | None  # returns charm_at's mode ("charm"/"anchor"/None)
    def track_pointer(self, global_pos: QPoint, now: float) -> bool
                                    # cursor move reported outside Qt (macOS event monitors): runs hover and
                                    # returns True where the overlay must take clicks (charm_at target, or
                                    # during a drag); False when hidden/retired or outside the region's bounds

class OverlayManager(QObject):
    def __init__(self, settings: Settings, platform: PlatformIntegration)
    settings: Settings; sim: Simulation
    windows: dict[str, OverlayWindow]   # by screen name; rebuilt on screenAdded/screenRemoved/geometryChanged
    def start(self) -> None             # build windows, hang charms, start animation
    def add_builtin(self, name) / add_image_from_dialog(parent=None) / remove(charm) / clear_all()
    def set_rope_style(s) / set_scale(s) / set_breeze(b) / set_hover_swing(b) / set_hidden(b)
    def nudge_all() / change_length(charm, factor) / move_to_screen(charm, screen_name)
    def wake(self) -> None              # (re)start the animation timer
    def save(self) -> None              # debounced 400 ms via single-shot QTimer; save_now() writes at once;
                                        # flush_save() (quit, shutdown) writes only a change not on disk
                                        # yet (pending, or its save failed): an unchanged session never
                                        # rewrites the file
    changed = Signal()                  # emitted when settings change (menus refresh)
    # additive: tick(elapsed), nudge(charm), charms, animating, primary_window(), shutdown()
```
Screens are keyed by `QScreen.name()` (empty → `screen-N`, duplicates → `name #2`); `spec.screen` and
`move_to_screen()` use these keys. Grabbing keeps the grab offset (no jump); a mouse move without a
button ends a drag whose release was lost; hover swing pushes only on entering a charm.
Animation loop: a `QTimer` (PreciseTimer, 16 ms) drives `Simulation.advance(elapsed)` from a
`QElapsedTimer`; the frame time is clamped to `max_substeps * dt` first (advance() would drop the
rest anyway, and the breeze impulse scales with it: after a stall or sleep - QElapsedTimer counts
through standby on Windows - the raw time would fling every charm). After every advance: windows
with moving ropes `update_mask(exact=False)` + `update()` of the old + new area. **When
`sim.settled()` and nothing is grabbed and breeze is off, the timer stops** (0 % CPU idle). Any interaction / change calls `wake()` which also `sim.reset_clock()`. Breeze applies a
smooth pseudo-random horizontal force (sum of two sines per rope, phase from spec.id hash, ~±60 px/s²) and
keeps the timer running at ~30 fps: with no interaction the loop drops to 33 ms once every rope is
slower than 60 px/s (`BREEZE_IDLE_SPEED`) and returns to 16 ms only on `wake()` or when one exceeds
150 px/s (`BREEZE_WAKE_SPEED`); a breeze sway stays below that at every rope length.
Anchor x on screen = `x_frac * window.width()`; on screen geometry change, ropes are re-anchored.
Charms whose `screen` is not connected hang on the primary screen (spec.screen unchanged so they return
when the monitor comes back).

### 3.7 `menu.py`

```python
def build_main_menu(menu: QMenu, mgr: OverlayManager, platform) -> None
    # Add charm ▸ (6 built-ins with icons, separator, "From your image…"), Rope style ▸ (radio), Charm size ▸
    # (radio 60..160 %), [x] Gentle breeze, [x] Swing on hover, Nudge all, Show/Hide charms,
    # [x] Start at login (only if platform.autostart_supported(); errors -> message_box warning),
    # Remove all charms… (confirm), About Latkan, Quit Latkan
def charm_menu(mgr, charm, platform) -> QMenu
    # Nudge, Longer rope, Shorter rope, Move to screen ▸ (only when >1 screen), Remove this charm, separator,
    # then the main menu items
```
The tray menu is rebuilt on `aboutToShow` so check marks are always current (and, deferred, after every
`changed`, for Linux trays that show an exported menu without asking). Also: `rebuild_main_menu(menu, mgr,
platform)`, `ROPE_STYLE_LABELS`. Every action slot runs through a counting wrapper:
`actions_running()` is True while one is on the stack (a dialog it opened runs a nested loop, with the
triggering QMenu / native menu item still under it), and `call_after_actions(cb)` calls `cb` once none
runs (at once when none does; `cb` may only schedule work). The tray skips any rebuild meanwhile and
re-arms its deferred refresh through `call_after_actions`, so no (sub)menu is deleted under its own handler. Dialogs run inside `with platform.foreground():` (Remove all… includes
`clear_all()` in the block), as do the manager's file dialog and the per-charm menu. Every message box
(also the manager's image-import error) goes through `message_box(platform, kind, title, text, *,
parent=None, buttons=Ok, default=NoButton)` (`kind` = "warning" | "question" | "about"): Qt's static
helper of that name, or, when `platform.dialogs_stay_on_top()`, the same box built as an instance with
`WindowStaysOnTopHint` (the about box then shows the application icon like `QMessageBox.about`).

### 3.8 `singleinstance.py`

```python
class SingleInstance(QObject):
    def __init__(self, key: str | None = None, parent=None)  # default key includes the user name;
                                            # keys sanitized and capped at 40 chars (macOS socket path limit)
    def is_primary(self) -> bool            # try connect: success -> send b"show", return False;
                                            # else removeServer(key) (stale socket) and listen
    activated = Signal()                    # emitted in the primary when another instance starts
```

### 3.9 `app.py`

`main(argv=None) -> int`: `platform = get_platform()`; `platform.prepare_environment()`; configure logging
(rotating file in `log_dir()`, 1 MB × 3, plus stderr); `QApplication` with `setQuitOnLastWindowClosed(False)`,
app name / org / desktop file name; `platform.configure_application(app)`; single instance check (secondary
exits 0); load settings; `OverlayManager(...).start()`; `QSystemTrayIcon` with `charms.app_icon()`
(`setIsMask` on macOS), menu from `menu.build_main_menu`, rebuilt on `aboutToShow`; left-click on tray
(Trigger) on Windows/Linux → nudge all; if no tray available, log and rely on per-charm right-click;
`activated` from single instance → set_hidden(False) + nudge_all; `aboutToQuit` → `flush_save()`;
`sys.excepthook` logs uncaught exceptions instead of crashing silently. CLI flags: `--reset` (back up and
reset settings), `--version`, `--debug`, and the unadvertised `--autostart` that the macOS LaunchAgent
passes. No tray at start-up → re-check every 2 s for about a minute (a panel that starts after Latkan at
login) and un-hide the charms meanwhile. After `exec()` returns, the manager, tray and single-instance
server are torn down explicitly while `QApplication` still exists.
**macOS** (`_startup_unhide_reason`, `_ReopenWatcher`): `QSystemTrayIcon.isSystemTrayAvailable()` is
always true there, yet the status item may not be drawn at all (no room beside the notch, hidden by the
user or a menu-bar manager). So a start *without* `--autostart` always un-hides the charms (adding a nazar
when there are none), and opening the running Latkan.app again - which starts no second process, so
`SingleInstance` never hears of it; Qt reports the reopen only as `applicationStateChanged(Active)`,
forced even when already active - is watched: an Active change while the charms are hidden or gone calls
the same handler as `SingleInstance.activated`, unless `platform.in_foreground` (Latkan's own dialog or
menu activated the app). The status item's native menu does not activate the app, and nothing hidden can
be clicked, so no other activation exists. For a login start, Qt's own launch activation(s) (one, or two
on macOS 14+) must not un-hide: until the settings change or a dialog opens, only a forced Active while
already Active (unique to a reopen) counts.

### 3.10 Packaging (`packaging/latkan.spec`)

Windowed onedir build on every OS. The spec prunes Qt parts Latkan never loads (virtual keyboard input
context and the QtQuick/QML stack it pulls in, TLS/network-information plugins, QtPdf, translations; on
Windows also `opengl32sw.dll`, `Qt6OpenGL.dll`, `qdirect2d.dll`, PySide6's OpenSSL 3) and then fails the
build if any kept binary still links against a pruned one. `ROOT` uses `absolute()` (not `resolve()`) so a
build started through a short junction stays under Windows' 260-character `MAX_PATH`.

Linux: the `gtk3` platform-theme plugin (`platformthemes/libqgtk3.so`) is pruned, together with the
GTK 3 / GDK / gdk-pixbuf / ATK / AT-SPI / Pango / Cairo / epoxy libraries that only it links. PyInstaller
would otherwise bundle the build machine's GTK and GLib (Ubuntu 22.04), and on GNOME-family desktops Qt
loads that theme first. The old GTK/GLib would then dlopen the host's newer GIO modules (gvfs, dconf),
GTK modules and gdk-pixbuf loaders, and the native file chooser (*From your image…*) could kill the
process. Without the plugin Qt uses its built-in `gnome`/generic theme and the Qt Widgets file dialog.
`libglib-2.0`/`libgthread-2.0` are kept because QtCore's GLib event dispatcher can link them.
`scripts/build.py` (`check_linux_bundle`) refuses to package a `dist/Latkan` that still contains
`libqgtk3*`, `libgtk-3.so*` or `libgdk-3.so*`.

The three shell scripts (`packaging/linux/*.sh`, `packaging/macos/make_dmg.sh`) are stored in git with
mode `100755`, so a clone on Linux or macOS can run them directly. A Windows checkout has
`core.filemode=false` and records new files as `100644`, so add them with `git add --chmod=+x`.
`tests/test_packaging.py` checks the modes. The README and `scripts/build.py` still run them through
`bash`, which works whatever the mode is.

Windows installer (`packaging/windows/latkan.iss`): per-user Inno Setup install; optional tasks
`desktopicon` and `autostart` (writes the same `Run\Latkan` value as `set_autostart` and clears the
`StartupApproved` flag). `UsePreviousTasks=no`: a task applies only when ticked in that run (or passed
with `/TASKS=`/`/MERGETASKS=`), so a silent or winget upgrade never re-enables autostart the user turned
off from the tray or Task Manager; an unticked task leaves what an earlier install created untouched.

## 4. Platform risk register

| Risk | Mitigation |
|---|---|
| Wayland: no global positioning / always-on-top | force XWayland (`xcb`); documented |
| GNOME has no tray without AppIndicator ext. | Ubuntu ships it; fallback right-click menu on charms |
| macOS: Tool windows hide when app inactive | `WA_MacAlwaysShowToolWindow` + `setHidesOnDeactivate_(False)` |
| macOS: charms must show on all Spaces, above other windows | NSStatusWindowLevel + collection behaviour |
| macOS: anchors over menu titles, status items, the notch | overlay starts below the menu bar (`availableGeometry().top()`) |
| macOS: mask gives no click-through; alpha hit-test undocumented | explicit `ignoresMouseEvents` toggled from NSEvent mouse monitors (`track_pointer`) |
| macOS: clicking a charm steals focus | non-activating NSPanel (`NSWindowStyleMaskNonactivatingPanel`); menus/dialogs activate on purpose |
| macOS: no hover events while inactive | the same NSEvent monitors feed `hover()` |
| macOS: status item may be invisible (notch, hidden) | manual start and reopen of Latkan.app show the charms |
| macOS: login item registered from DMG / translocated path | refused with "move to Applications"; missing program = autostart off |
| macOS: unsigned app blocked by Gatekeeper | ad-hoc sign in CI; optional Developer ID + notarize via secrets; README explains right-click → Open |
| Windows/GNOME ≥ 50: full-screen topmost (O-R) window side effects (taskbar/notifications; top bar and dock hidden) | overlay height = screen height − 1 |
| GNOME on Xorg: the top bar takes presses over the overlay (input region on the Composite overlay window) | rope-top drag zone reaches `anchor_top_inset` (the bar height) further down |
| Linux/X11 without a compositor: transparent overlay pixels show as black boxes | `_NET_WM_CM_S<n>` owner check (+ XFixes watch); mask = painted pixels, no hit pads; warning logged; README |
| Linux/X11: tray-menu dialogs open behind the active window (stale `_NET_WM_USER_TIME`) | `activate_app` refreshes Qt's X server time via a helper window's PropertyNotify; message boxes stay on top |
| Linux packages: bundled GTK 3 theme runs the build machine's GTK/GLib against the host's newer GIO/GTK/pixbuf modules (file chooser crash) | spec prunes `libqgtk3.so` + the GTK-only libraries; `build.py` fails a bundle that still has GTK 3 |
| Windows: click steals focus | `WindowDoesNotAcceptFocus` + `WS_EX_NOACTIVATE` |
| Windows: installer upgrade re-enables autostart | `UsePreviousTasks=no`: tasks apply only when ticked in that run |
| Idle CPU / battery | timer stops when settled; masks pushed only when outgrown (24 px slack while animating), none on Windows; breeze at 30 fps by speed, not pose |
| Mixed-DPI multi-monitor | one overlay per QScreen; Qt 6 per-monitor DPI; logical coordinates |
| Corrupt settings | atomic writes; corrupt file backed up, defaults used |
| Settings file unreadable at start (Windows backup/sync/AV lock at login, POSIX EACCES) | read retried on Windows; defaults used, file copied to `.bak-<n>` before the first save over it, or not overwritten at all |
| Screen change while a charm menu / dialog from a menu runs (nested event loop) | overlay disposal deferred until `show_charm_menu()` returns; tray rebuild waits until no menu action runs |
| Click-through | window mask = charm + rope regions only (+ 1/255 hit pads for per-pixel hit-testing); Windows: no mask, the pads on the per-pixel-alpha layered window; macOS: explicit `ignoresMouseEvents`; everything else passes through |
