"""Linux / BSD integration: XWayland on Wayland sessions, override-redirect overlays, X11 window
manager quirks (GNOME's full-screen heuristics and top bar, compositor detection, focus-stealing
prevention) and an XDG autostart ``.desktop`` entry.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from PySide6.QtCore import QRect, Qt

from .. import APP_NAME
from .base import PlatformIntegration, atomic_write_bytes, one_pixel_short
from .base import qpa_platform_name as _qpa_platform_name

logger = logging.getLogger(__name__)

AUTOSTART_FILE = "latkan.desktop"
ICON_NAME = "latkan"

#: Characters that force an Exec argument to be quoted (Desktop Entry Specification, "Exec key").
_EXEC_RESERVED = frozenset(" \t\n\"'\\><~|&;$*?#()`")
#: Characters that must be backslash-escaped inside a double-quoted Exec argument.
_EXEC_QUOTED_ESCAPES = frozenset('"`$\\')

#: XFixes selection event mask: SetSelectionOwner | SelectionWindowDestroy | SelectionClientClose.
_XFIXES_SELECTION_EVENTS = 0x7
#: ``XEventsQueued`` mode that only counts events Xlib has already read (no I/O).
_QUEUED_ALREADY = 0


def _wayland_session(env: Mapping[str, str] = os.environ) -> bool:
    """Whether this is a Wayland session (Latkan then runs on XWayland; the compositor composites)."""
    return bool(env.get("WAYLAND_DISPLAY")) or env.get("XDG_SESSION_TYPE", "").lower() == "wayland"


def _gnome_shell_on_xorg(env: Mapping[str, str] = os.environ) -> bool:
    """GNOME Shell running as the X11 window manager ("Ubuntu on Xorg" and the like).

    ``XDG_CURRENT_DESKTOP`` is a colon-separated list ("ubuntu:GNOME", "GNOME-Classic:GNOME").
    GNOME Flashback lists GNOME too but has no Shell (its panel is an ordinary dock window).
    """
    if env.get("XDG_SESSION_TYPE", "").lower() != "x11":
        return False
    desktops = {part.strip().lower() for part in env.get("XDG_CURRENT_DESKTOP", "").split(":")}
    return "gnome" in desktops and "gnome-flashback" not in desktops


def _load_library(ctypes: Any, name: str, soname: str) -> Any:
    """``ctypes.CDLL`` for ``soname``, falling back to ``ctypes.util.find_library(name)``."""
    try:
        return ctypes.CDLL(soname)
    except OSError:
        import ctypes.util

        path = ctypes.util.find_library(name)
        if not path:
            raise
        return ctypes.CDLL(path)


class _X11Compositor:
    """Tracks the owner of the ``_NET_WM_CM_S<screen>`` selection, i.e. the compositing manager.

    Talks to the X server over a private Xlib connection (ctypes), never Qt's, so nothing here can
    disturb Qt's own event handling; any failure just means "cannot tell". With libXfixes, XFixes
    selection events on that connection report a compositor starting or stopping (KWin suspends
    compositing for full-screen games, for instance) through a ``QSocketNotifier``.
    """

    def __init__(self) -> None:
        self._xlib: Any = None
        self._xfixes: Any = None
        self._display: int | None = None
        self._atom = 0
        self._root = 0
        self._event: Any = None
        self._unavailable = False
        self._watch_failed = False
        self._notifier: Any = None
        self._callback: Callable[[], None] | None = None

    def _connect(self) -> bool:
        if self._display is not None:
            return True
        if self._unavailable:
            return False
        try:
            import ctypes

            xlib = _load_library(ctypes, "X11", "libX11.so.6")
            ptr, ulong, cint = ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int
            xlib.XOpenDisplay.argtypes, xlib.XOpenDisplay.restype = [ctypes.c_char_p], ptr
            xlib.XDefaultScreen.argtypes, xlib.XDefaultScreen.restype = [ptr], cint
            xlib.XRootWindow.argtypes, xlib.XRootWindow.restype = [ptr, cint], ulong
            xlib.XInternAtom.argtypes, xlib.XInternAtom.restype = [ptr, ctypes.c_char_p, cint], ulong
            xlib.XGetSelectionOwner.argtypes, xlib.XGetSelectionOwner.restype = [ptr, ulong], ulong
            xlib.XEventsQueued.argtypes, xlib.XEventsQueued.restype = [ptr, cint], cint
            xlib.XNextEvent.argtypes, xlib.XNextEvent.restype = [ptr, ptr], cint
            for func in (xlib.XPending, xlib.XConnectionNumber, xlib.XFlush):
                func.argtypes, func.restype = [ptr], cint
            display = xlib.XOpenDisplay(None)  # $DISPLAY, like Qt
            if not display:
                raise OSError("XOpenDisplay() failed")
            screen = xlib.XDefaultScreen(display)
            self._atom = xlib.XInternAtom(display, b"_NET_WM_CM_S%d" % screen, 0)
            self._root = xlib.XRootWindow(display, screen)
            self._event = (ctypes.c_long * 24)()  # sizeof(XEvent)
        except Exception:  # native: never let it take Latkan down
            self._unavailable = True
            logger.warning("Cannot check for an X11 compositing manager; assuming one runs", exc_info=True)
            return False
        self._xlib, self._display = xlib, display
        return True

    def _drain(self) -> None:
        """Discard every pending event (they only say "the selection owner may have changed")."""
        while self._xlib.XPending(self._display) > 0:
            self._xlib.XNextEvent(self._display, self._event)

    def active(self) -> bool | None:
        """True / False: a compositing manager runs / does not. None: cannot tell."""
        if not self._connect():
            return None
        xlib, display = self._xlib, self._display
        try:
            owner = 0
            for _attempt in range(8):
                self._drain()
                owner = xlib.XGetSelectionOwner(display, self._atom)  # one round trip
                # Xlib may have read events newer than the reply along with it; the socket
                # notifier never fires for those, so ask again until none are left.
                if xlib.XEventsQueued(display, _QUEUED_ALREADY) == 0:
                    break
            return owner != 0
        except Exception:  # native call
            logger.debug("Could not query the compositing manager selection", exc_info=True)
            return None

    def watch(self, callback: Callable[[], None] | None) -> None:
        """Call ``callback`` when the compositor may have started or stopped (None: stop)."""
        self._callback = callback
        if self._notifier is not None:
            self._notifier.setEnabled(callback is not None)
            return
        if callback is None or self._watch_failed or not self._connect():
            return
        xlib, display = self._xlib, self._display
        try:
            import ctypes

            from PySide6.QtCore import QSocketNotifier

            xfixes = _load_library(ctypes, "Xfixes", "libXfixes.so.3")
            ptr, ulong, cint_p = ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_int)
            for func in (xfixes.XFixesQueryExtension, xfixes.XFixesQueryVersion):
                func.argtypes, func.restype = [ptr, cint_p, cint_p], ctypes.c_int
            select = xfixes.XFixesSelectSelectionInput
            select.argtypes, select.restype = [ptr, ulong, ulong, ulong], None
            event_base, error_base = ctypes.c_int(), ctypes.c_int()
            if not xfixes.XFixesQueryExtension(display, ctypes.byref(event_base), ctypes.byref(error_base)):
                raise OSError("the X server has no XFixes extension")
            major, minor = ctypes.c_int(5), ctypes.c_int(0)
            xfixes.XFixesQueryVersion(display, ctypes.byref(major), ctypes.byref(minor))
            select(display, self._root, self._atom, _XFIXES_SELECTION_EVENTS)
            xlib.XFlush(display)
            notifier = QSocketNotifier(xlib.XConnectionNumber(display), QSocketNotifier.Type.Read)
            notifier.activated.connect(self._on_readable)
        except Exception:  # native: fall back to re-checking when charms appear or start moving
            self._watch_failed = True
            logger.info("Cannot watch for compositor changes; re-checking on activity only", exc_info=True)
            return
        self._xfixes, self._notifier = xfixes, notifier

    def _on_readable(self, *_args: object) -> None:
        try:
            self._drain()
        except Exception:  # native call
            logger.debug("Could not read compositor selection events", exc_info=True)
        callback = self._callback
        if callback is None:
            return
        try:
            callback()
        except Exception:  # runs from a socket notifier: log instead of raising into Qt
            logger.exception("Compositor change handler failed")


def desktop_exec(args: Sequence[str]) -> str:
    """Render ``args`` as a Desktop-Entry ``Exec`` value.

    Like :func:`shlex.join`, plain arguments stay unquoted; others are double-quoted (the only
    quoting the spec allows) with ``" ` $ \\`` escaped. ``%`` is doubled (field-code escape) and the
    general string-value escapes are applied last, so a literal backslash ends up as ``\\\\``.
    """
    parts: list[str] = []
    for arg in args:
        if arg and not _EXEC_RESERVED.intersection(arg):
            parts.append(arg)
        else:
            escaped = "".join("\\" + ch if ch in _EXEC_QUOTED_ESCAPES else ch for ch in arg)
            parts.append(f'"{escaped}"')
    value = " ".join(parts).replace("%", "%%")
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")


def _read_desktop_entry(path: Path) -> dict[str, str]:
    """Parse the ``[Desktop Entry]`` group of a .desktop file (raw, unescaped values)."""
    entries: dict[str, str] = {}
    in_group = False
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            in_group = line == "[Desktop Entry]"
            continue
        if in_group and "=" in line:
            key, _, value = line.partition("=")
            entries[key.strip()] = value.strip()
    return entries


class LinuxPlatform(PlatformIntegration):
    """Linux (X11, and Wayland through XWayland) integration; also used for the BSDs."""

    name = "linux"

    def __init__(self, config_home: Path | None = None) -> None:
        self._config_home = config_home
        self._time_window: Any = None  # never-shown QWindow, see activate_app()
        self._time_toggle = False
        self._x11_compositor: _X11Compositor | None = None
        self._warned_no_compositor = False

    # -- environment ----------------------------------------------------------------------------

    def prepare_environment(self) -> None:
        """Route Wayland sessions through XWayland.

        Wayland clients can neither position their windows nor keep them above others, which the
        overlay needs. ``QT_QPA_PLATFORM`` chosen by the user is always respected.
        """
        env = os.environ
        platform = env.get("QT_QPA_PLATFORM")
        if platform:
            if platform.split(";", 1)[0].strip().startswith("wayland"):
                logger.warning(
                    "QT_QPA_PLATFORM=%s: on native Wayland charms cannot stay on top of other windows",
                    platform,
                )
            return
        if not env.get("WAYLAND_DISPLAY"):
            return
        if env.get("DISPLAY"):
            # Fall back to native Wayland rather than aborting if the xcb plugin cannot load
            # (e.g. libxcb-cursor0 missing); Qt 6 tries the ';'-separated list in order.
            env["QT_QPA_PLATFORM"] = "xcb;wayland"
            logger.info(
                "Wayland session detected: using XWayland (QT_QPA_PLATFORM=%s)", env["QT_QPA_PLATFORM"]
            )
        else:
            logger.warning(
                "Wayland session without XWayland (DISPLAY is unset): charms cannot be positioned at "
                "the top of the screen or kept above other windows. Install/enable XWayland."
            )

    def configure_application(self, app: Any) -> None:
        try:
            platform_name = str(app.platformName())
        except Exception:  # defensive: only used for a diagnostic
            logger.debug("Could not query the Qt platform name", exc_info=True)
            return
        logger.info("Qt platform plugin: %s", platform_name)
        if platform_name.startswith("wayland"):
            logger.warning(
                "Running as a native Wayland client; charms may not stay on top. If the xcb plugin "
                "failed to load, install libxcb-cursor0 (Debian/Ubuntu) or xcb-util-cursor."
            )

    # -- overlay --------------------------------------------------------------------------------

    def overlay_window_flags(self) -> Qt.WindowType:
        # Override-redirect: stays above panels/docks and gets no WM decoration or placement.
        return super().overlay_window_flags() | Qt.WindowType.X11BypassWindowManagerHint

    def overlay_geometry(self, screen: Any) -> QRect:
        """Screen geometry minus one pixel of height.

        Mutter 50 (GNOME 50) also counts the topmost override-redirect window as full-screen when
        it is exactly monitor-sized, and then hides the top bar and the dock on that monitor for as
        long as it is mapped. One row short never matches; elsewhere it costs one unused row.
        """
        return one_pixel_short(screen)

    def anchor_top_inset(self, screen: Any) -> int:
        """GNOME Shell on Xorg: the height of the top bar on ``screen`` (else 0).

        There the Shell sets its input region, top bar included, on the Composite overlay window,
        which lies above every window, override-redirect ones too: presses in the top-bar strip go
        to the Shell although the charms are drawn over it. The rope-top drag zone therefore
        reaches this much further down. Not on Wayland, where the Shell picks input by stacking
        and the overlay is above the panel, so a deeper zone would only block panel clicks.
        """
        if not _gnome_shell_on_xorg():
            return 0
        geometry, available = screen.geometry(), screen.availableGeometry()
        inset = available.top() - geometry.top()
        return max(0, min(inset, geometry.height() // 4))

    def compositing_active(self) -> bool:
        """X11: whether a compositing manager owns ``_NET_WM_CM_S<screen>``.

        Without one the X server shows the overlay's ARGB pixels as they are, so every transparent
        pixel inside the window mask would be black. Wayland sessions (XWayland) always composite;
        when the check itself fails, the normal translucent mode is kept.
        """
        if _qpa_platform_name() != "xcb" or _wayland_session():
            return True
        if self._x11_compositor is None:
            self._x11_compositor = _X11Compositor()
        active = self._x11_compositor.active()
        if active is None:
            return True
        if not active and not self._warned_no_compositor:
            self._warned_no_compositor = True
            logger.warning(
                "No compositing window manager is running: charms are drawn as shaped windows "
                "(harder edges, only painted pixels take clicks). Turn on your window manager's "
                "compositor (or start one such as picom) for smooth, translucent charms."
            )
        return active

    def watch_compositing(self, callback: Callable[[], None] | None) -> None:
        if callback is None:
            if self._x11_compositor is not None:
                self._x11_compositor.watch(None)
            return
        if _qpa_platform_name() != "xcb" or _wayland_session():
            return
        if self._x11_compositor is None:
            self._x11_compositor = _X11Compositor()
        self._x11_compositor.watch(callback)

    def dialogs_stay_on_top(self) -> bool:
        """X11: yes, in case the window manager still refuses a dialog the focus (see activate_app)."""
        return _qpa_platform_name() == "xcb"

    def activate_app(self) -> None:
        """X11: bring Qt's X server timestamp up to date right before a dialog opens. Never raises.

        Tray clicks (StatusNotifierItem / DBusMenu) reach Latkan over D-Bus, not as X events, so
        the last X server time Qt knows - which it writes into a new window's
        ``_NET_WM_USER_TIME`` and its ``_NET_ACTIVE_WINDOW`` request - can be minutes old. Mutter
        and KWin then refuse the dialog the focus and stack it below the active window. Changing
        a property of a never-shown helper window makes the X server send a PropertyNotify whose
        timestamp Qt adopts. Nothing is raised or activated here.
        """
        if _qpa_platform_name() != "xcb":
            return
        try:
            from PySide6.QtGui import QGuiApplication, QWindow

            window = self._time_window
            if window is None:
                window = QWindow()
                window.setObjectName("LatkanTimestampWindow")
                window.create()  # a native X window that is never mapped
                self._time_window = window
            self._time_toggle = not self._time_toggle
            window.setTitle(f"{APP_NAME} {'1' if self._time_toggle else '2'}")  # X ChangeProperty
            # The first round trip gets the PropertyNotify out of the X server. Qt reads X events
            # on a separate thread, so the second one makes sure that thread has queued it before
            # sync()'s final processEvents() hands it to Qt, which then updates its time.
            QGuiApplication.sync()
            QGuiApplication.sync()
        except Exception:  # native integration: a dialog behind other windows beats no dialog
            logger.warning("Could not refresh the X server timestamp before a dialog", exc_info=True)

    # -- autostart ------------------------------------------------------------------------------

    @property
    def config_home(self) -> Path:
        """Injected config home, else ``$XDG_CONFIG_HOME`` (if absolute), else ``~/.config``."""
        if self._config_home is not None:
            return self._config_home
        xdg = os.environ.get("XDG_CONFIG_HOME", "")
        if xdg and os.path.isabs(xdg):
            return Path(xdg)
        return Path.home() / ".config"

    @property
    def autostart_path(self) -> Path:
        return self.config_home / "autostart" / AUTOSTART_FILE

    def launch_command(self) -> list[str]:
        """Inside an AppImage, autostart the ``.AppImage`` file itself.

        ``sys.executable`` lives in the image's temporary mount point, which is gone after exit.
        The ``APPIMAGE`` variable is only trusted in a frozen build: the AppImage runtime exports it
        to child processes, so a source run started from e.g. an AppImage IDE inherits a foreign one.
        """
        appimage = os.environ.get("APPIMAGE")
        if appimage and getattr(sys, "frozen", False):
            return [appimage]
        return super().launch_command()

    def autostart_supported(self) -> bool:
        return True

    def autostart_enabled(self) -> bool:
        path = self.autostart_path
        try:
            entry = _read_desktop_entry(path)
        except FileNotFoundError:
            return False
        except OSError:
            logger.warning("Unreadable autostart entry %s; treating autostart as off", path, exc_info=True)
            return False
        if not entry.get("Exec"):
            return False
        # Session managers (GNOME "Startup Applications", KDE, XFCE) disable entries in place.
        if entry.get("Hidden", "").lower() == "true":
            return False
        return entry.get("X-GNOME-Autostart-enabled", "true").lower() != "false"

    def set_autostart(self, enabled: bool) -> None:
        path = self.autostart_path
        try:
            if enabled:
                atomic_write_bytes(path, self._desktop_entry().encode("utf-8"))
                logger.info("Autostart enabled: %s", path)
            else:
                path.unlink(missing_ok=True)
                logger.info("Autostart disabled: removed %s", path)
        except OSError:
            logger.error(
                "Could not %s autostart (%s)", "enable" if enabled else "disable", path, exc_info=True
            )
            raise

    def _desktop_entry(self) -> str:
        lines = [
            "[Desktop Entry]",
            "Type=Application",
            f"Name={APP_NAME}",
            "Comment=Charms that hang from the top of your screen",
            f"Exec={desktop_exec(self._checked_launch_command())}",
            f"Icon={ICON_NAME}",
            "Terminal=false",
            "StartupNotify=false",
            "X-GNOME-Autostart-enabled=true",
        ]
        return "\n".join(lines) + "\n"
