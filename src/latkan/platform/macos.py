"""macOS integration: accessory app (no Dock icon), overlay below the menu bar on every Space,
explicit click-through and hover for the overlay, and a per-user LaunchAgent for autostart.

PyObjC (``pyobjc-framework-Cocoa``) is optional: without it Latkan still runs, the overlay just
behaves like an ordinary Qt tool window (a warning is logged once).

Overlay input. A Qt window mask does not make anything click-through on Cocoa (it only masks
the layer; clicks outside it go to Latkan's own responder chain), and the WindowServer's per-pixel
alpha hit-testing of a transparent window is undocumented (its threshold is not 1/255). So with
PyObjC the overlay sets ``ignoresMouseEvents`` explicitly: YES (every click goes to the app
below) except while the cursor is over a click target or a drag is in progress. NSEvent mouse
monitors (a global one for moves over other apps, a local one for moves over Latkan; mouse
monitors need no Accessibility permission) report every cursor move to
``OverlayWindow.track_pointer()``, which also runs hover swing: Qt drops button-less mouse moves
while the app is inactive, which an accessory app whose overlay refuses key status nearly always
is. If the monitors cannot be installed, the flag is never touched and the old alpha
hit-testing applies.

The overlay panel is also made non-activating, so clicking or dragging a charm does not take
keyboard focus from the frontmost app (the macOS counterpart of Windows' ``WS_EX_NOACTIVATE``).
"""

from __future__ import annotations

import functools
import logging
import math
import os
import plistlib
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

import shiboken6
from PySide6.QtCore import QObject, QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QCursor

from .. import APP_ID
from .base import AUTOSTART_FLAG, PlatformIntegration, atomic_write_bytes
from .base import qpa_platform_name as _qpa_platform_name

logger = logging.getLogger(__name__)

NS_APPLICATION_ACTIVATION_POLICY_ACCESSORY = 1
NS_STATUS_WINDOW_LEVEL = 25  # above the menu bar (NSMainMenuWindowLevel = 24)
NS_WINDOW_COLLECTION_CAN_JOIN_ALL_SPACES = 1 << 0
NS_WINDOW_COLLECTION_STATIONARY = 1 << 4
NS_WINDOW_COLLECTION_IGNORES_CYCLE = 1 << 6
NS_WINDOW_COLLECTION_FULL_SCREEN_AUXILIARY = 1 << 8
OVERLAY_COLLECTION_BEHAVIOR = (
    NS_WINDOW_COLLECTION_CAN_JOIN_ALL_SPACES
    | NS_WINDOW_COLLECTION_STATIONARY
    | NS_WINDOW_COLLECTION_IGNORES_CYCLE
    | NS_WINDOW_COLLECTION_FULL_SCREEN_AUXILIARY
)
#: NSWindowStyleMaskNonactivatingPanel: clicks on the panel do not activate the app (NSPanel only).
NS_WINDOW_STYLE_MASK_NONACTIVATING_PANEL = 1 << 7
#: NSEventMask bits (1 << NSEventType).
NS_EVENT_MASK_MOUSE_MOVED = 1 << 5
NS_EVENT_MASK_LEFT_MOUSE_DRAGGED = 1 << 6

#: Cursor moves are evaluated at most this often (a 1000 Hz mouse must not mean 1000 updates/s).
POINTER_INTERVAL_MS = 16
#: While an overlay takes clicks, re-check this often even without mouse moves: a charm can
#: swing away from a resting cursor, and a drag can end without a move.
CAPTURE_RECHECK_MS = 50

LAUNCH_AGENT_LABEL = APP_ID
#: Error shown when "Start at login" would register a path that is gone after eject/reboot.
MOVE_TO_APPLICATIONS = "Move Latkan to the Applications folder first, then turn on Start at login."


class _PyObjC(NamedTuple):
    AppKit: Any
    objc: Any


@functools.lru_cache(maxsize=1)
def _load_pyobjc() -> _PyObjC | None:
    """Import PyObjC lazily (once); ``None`` when it is not installed (always the case off macOS)."""
    try:
        import AppKit
        import objc
    except ImportError:
        return None
    return _PyObjC(AppKit, objc)


def _cursor_pos() -> QPoint:
    """Global cursor position in logical px (a module function so tests can replace it)."""
    return QCursor.pos()


def _is_translocated(path: str) -> bool:
    """Gatekeeper App Translocation runs a quarantined app from a random, temporary path."""
    return "/AppTranslocation/" in path


def _on_read_only_volume(path: str) -> bool:
    """True for a read-only file system, which is how a mounted DMG appears."""
    statvfs = getattr(os, "statvfs", None)
    if statvfs is None:  # not POSIX
        return False
    try:
        return bool(statvfs(path).f_flag & getattr(os, "ST_RDONLY", 1))
    except OSError:
        logger.debug("Could not stat the file system of %s", path, exc_info=True)
        return False


class MacOSPlatform(PlatformIntegration):
    """macOS 12+ integration."""

    name = "macos"

    def __init__(self, home: Path | None = None) -> None:
        self._home = home
        self._warned_no_pyobjc = False
        self._tracker: _PointerTracker | None = None

    # -- application ----------------------------------------------------------------------------

    def configure_application(self, app: Any) -> None:
        """Make Latkan an accessory app: no Dock icon, no app menu, lives in the menu bar."""
        mods = self._pyobjc()
        if mods is None:
            return
        try:
            ns_app = mods.AppKit.NSApplication.sharedApplication()
            ns_app.setActivationPolicy_(NS_APPLICATION_ACTIVATION_POLICY_ACCESSORY)
        except Exception:  # native call
            logger.warning("Could not set the accessory activation policy", exc_info=True)

    # -- overlay --------------------------------------------------------------------------------

    def overlay_geometry(self, screen: Any) -> QRect:
        """The screen below the menu bar.

        Ropes hang from the menu bar's bottom edge, so no rope, charm or anchor-drag zone covers
        menu titles, status items (including Latkan's own) or the notch of a MacBook display.
        Only the top comes from ``availableGeometry()``, which also leaves out the Dock. With an
        auto-hiding menu bar both tops are the same and the whole screen is used.
        """
        full = QRect(screen.geometry())
        top = screen.availableGeometry().y()
        if not full.y() < top < full.bottom():
            return full
        return QRect(full.x(), top, full.width(), full.bottom() - top + 1)

    def configure_overlay(self, widget: Any) -> None:
        """Keep the overlay visible on all Spaces even when inactive; make its input explicit."""
        try:
            widget.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow, True)
        except Exception:  # defensive: never let a platform tweak break the overlay
            logger.warning("Could not set WA_MacAlwaysShowToolWindow", exc_info=True)

        qpa = _qpa_platform_name()
        if qpa != "cocoa":
            # offscreen/minimal (tests, CI) hand out fake window ids: never treat those as NSViews.
            logger.debug("Native overlay tweaks skipped: Qt platform plugin is %r, not 'cocoa'", qpa)
            return
        mods = self._pyobjc()
        if mods is None:
            return
        try:
            view = mods.objc.objc_object(c_void_p=int(widget.winId()))
            window = view.window() if view is not None else None
            if window is None:
                logger.warning("Overlay has no NSWindow yet; native window tweaks skipped")
                return
            self._configure_window(mods, window)
        except Exception:  # native call
            logger.warning("Could not apply native NSWindow settings to the overlay", exc_info=True)
            return
        self._track_pointer(mods, widget, window)

    def _configure_window(self, mods: _PyObjC, window: Any) -> None:
        """NSWindow settings of an overlay (applied again if Qt re-creates the NSWindow)."""
        window.setLevel_(NS_STATUS_WINDOW_LEVEL)
        window.setCollectionBehavior_(OVERLAY_COLLECTION_BEHAVIOR)
        window.setHidesOnDeactivate_(False)
        window.setHasShadow_(False)
        self._make_non_activating(mods, window)

    @staticmethod
    def _make_non_activating(mods: _PyObjC, window: Any) -> None:
        """Clicks and drags on the overlay must not activate Latkan (focus stays where it is).

        ``WindowDoesNotAcceptFocus`` only stops the panel from becoming key; the WindowServer
        still activates the owning app on mouse-down unless the panel is non-activating. Qt keeps
        an externally set NonactivatingPanel bit when it recomputes the style mask. NSPanel may
        sync the WindowServer's "prevents activation" tag only at init, hence the private setter.
        """
        try:
            panel_class = getattr(mods.AppKit, "NSPanel", None)
            if panel_class is not None and not window.isKindOfClass_(panel_class):
                logger.warning("The overlay is not an NSPanel; clicking a charm will activate Latkan")
                return
            window.setStyleMask_(window.styleMask() | NS_WINDOW_STYLE_MASK_NONACTIVATING_PANEL)
            if window.respondsToSelector_("_setPreventsActivation:"):
                window._setPreventsActivation_(True)
        except Exception:  # native call
            logger.warning("Could not make the overlay a non-activating panel", exc_info=True)

    def _track_pointer(self, mods: _PyObjC, widget: Any, window: Any) -> None:
        """Hand the overlay to the pointer tracker (see the module docstring)."""
        if not callable(getattr(widget, "track_pointer", None)):
            return
        try:
            if self._tracker is None:
                self._tracker = _PointerTracker(mods, functools.partial(self._configure_window, mods))
            if not self._tracker.add(widget, window):
                logger.warning(
                    "No mouse event monitors: charms swing on hover only while Latkan is active, "
                    "and clicks rely on the window's transparency"
                )
        except Exception:  # native call
            logger.warning("Could not track the pointer for the overlay", exc_info=True)

    def tray_icon_is_mask(self) -> bool:
        return True

    def activate_app(self) -> None:
        """Activate the accessory app so its dialogs open in front of other apps' windows."""
        mods = self._pyobjc()
        if mods is None:
            return
        try:
            ns_app = mods.AppKit.NSApplication.sharedApplication()
            activate = getattr(ns_app, "activateIgnoringOtherApps_", None)
            if activate is not None:  # deprecated in macOS 14 but still honoured
                activate(True)
            else:
                ns_app.activate()
        except Exception:  # native call
            logger.warning("Could not activate the application", exc_info=True)

    def _pyobjc(self) -> _PyObjC | None:
        mods = _load_pyobjc()
        if mods is None and not self._warned_no_pyobjc:
            self._warned_no_pyobjc = True
            logger.warning(
                "PyObjC (pyobjc-framework-Cocoa) is not installed: charms will not follow you "
                "across Spaces, clicking them takes focus from other apps, hover swing works only "
                "while Latkan is active, and a Dock icon is shown."
            )
        return mods

    # -- autostart ------------------------------------------------------------------------------

    @property
    def launch_agent_path(self) -> Path:
        """``~/Library/LaunchAgents/in.latkan.app.plist`` (under the injected home dir, if any)."""
        home = self._home if self._home is not None else Path.home()
        return home / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"

    def autostart_supported(self) -> bool:
        return True

    def autostart_enabled(self) -> bool:
        path = self.launch_agent_path
        try:
            with path.open("rb") as fh:
                data = plistlib.load(fh)
        except FileNotFoundError:
            return False
        except (OSError, plistlib.InvalidFileException, ValueError):
            logger.warning("Unreadable LaunchAgent %s; treating autostart as off", path, exc_info=True)
            return False
        if not isinstance(data, dict):
            return False
        arguments = data.get("ProgramArguments")
        if not isinstance(arguments, list) or not arguments or data.get("Disabled", False):
            return False
        program = arguments[0]
        if not isinstance(program, str) or not os.path.exists(program):
            # Moved, renamed, or registered from a DMG / App Translocation path: launchd would fail
            # silently at the next login, so do not claim that Latkan starts then.
            logger.info("LaunchAgent %s points to a missing program %r; autostart is off", path, program)
            return False
        return True

    def set_autostart(self, enabled: bool) -> None:
        path = self.launch_agent_path
        try:
            if enabled:
                command = self._checked_launch_command()
                self._check_permanent_location(command[0])
                agent = {
                    "Label": LAUNCH_AGENT_LABEL,
                    # AUTOSTART_FLAG: a login start keeps charms the user hid hidden (app.py).
                    "ProgramArguments": [*command, AUTOSTART_FLAG],
                    "RunAtLoad": True,
                    "KeepAlive": False,
                    "ProcessType": "Interactive",
                    # Only load in graphical login sessions, never for SSH/background sessions.
                    "LimitLoadToSessionType": "Aqua",
                }
                atomic_write_bytes(path, plistlib.dumps(agent, fmt=plistlib.FMT_XML))
                logger.info("Autostart enabled: %s", path)
            else:
                path.unlink(missing_ok=True)
                logger.info("Autostart disabled: removed %s", path)
        except OSError:
            logger.error(
                "Could not %s autostart (%s)", "enable" if enabled else "disable", path, exc_info=True
            )
            raise

    @staticmethod
    def _check_permanent_location(program: str) -> None:
        """Refuse to register the app bundle from where it will be gone at the next login.

        Raises:
            OSError: a frozen Latkan runs translocated or from a read-only volume (a mounted DMG).
                External drives are fine; only the frozen app is checked (source runs have no
                bundle to move).
        """
        if not getattr(sys, "frozen", False):
            return
        if _is_translocated(program) or _on_read_only_volume(program):
            raise OSError(MOVE_TO_APPLICATIONS)


# ------------------------------------------------------------------------------ pointer tracking


@dataclass(eq=False)
class _Overlay:
    widget: Any
    window_number: int


class _PointerTracker(QObject):
    """Drives ``ignoresMouseEvents`` and hover of the overlays from NSEvent mouse monitors.

    The monitor handlers only schedule :meth:`update` on a Qt timer (throttled to
    ``POINTER_INTERVAL_MS``, with the last move always evaluated), so no Qt code runs inside
    AppKit's event dispatch and a Python error can never propagate into it. ``update`` asks each
    overlay whether it must take clicks at the cursor (``track_pointer``) and sets the flag:
    NO only there, YES everywhere else. The actual NSWindow value is compared on every update, so
    a value Qt rewrote (it does on ``setWindowFlags``) or a re-created NSWindow heals itself on
    the next cursor move; a re-created NSWindow also gets its native settings again. Overlays
    are dropped when their widget is destroyed, and the monitors are removed with the last one.
    """

    def __init__(self, mods: _PyObjC, configure_window: Any) -> None:
        super().__init__()
        self._mods = mods
        self._configure_window = configure_window
        self._overlays: dict[int, _Overlay] = {}
        self._monitors: list[Any] = []
        self._last_update = -math.inf
        self._reported_failure = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.update)

    @property
    def installed(self) -> bool:
        return bool(self._monitors)

    # -- lifecycle ------------------------------------------------------------------------------

    def install(self) -> bool:
        """Add the mouse-move monitors; False (and nothing installed) when AppKit refuses."""
        ns_event = self._mods.AppKit.NSEvent
        try:
            monitors = [
                ns_event.addGlobalMonitorForEventsMatchingMask_handler_(
                    NS_EVENT_MASK_MOUSE_MOVED, self._on_global_event
                ),
                ns_event.addLocalMonitorForEventsMatchingMask_handler_(
                    NS_EVENT_MASK_MOUSE_MOVED | NS_EVENT_MASK_LEFT_MOUSE_DRAGGED, self._on_local_event
                ),
            ]
        except Exception:  # native call
            logger.warning("Could not install mouse event monitors", exc_info=True)
            return False
        self._monitors = [monitor for monitor in monitors if monitor is not None]
        if len(self._monitors) != len(monitors):
            logger.warning("AppKit refused a mouse event monitor")
            self.uninstall()
            return False
        logger.debug("Mouse event monitors installed")
        return True

    def uninstall(self) -> None:
        """Remove the monitors (the overlays keep whatever flag they have; they are going away)."""
        self._timer.stop()
        monitors, self._monitors = self._monitors, []
        for monitor in monitors:
            try:
                self._mods.AppKit.NSEvent.removeMonitor_(monitor)
            except Exception:  # native call
                logger.debug("Could not remove a mouse event monitor", exc_info=True)

    def add(self, widget: Any, window: Any) -> bool:
        """Start deciding ``widget``'s input explicitly; False when no monitors could be installed."""
        if not self._monitors and not self.install():
            return False
        key = id(widget)
        if key not in self._overlays:
            self._overlays[key] = _Overlay(widget, int(window.windowNumber()))
            widget.destroyed.connect(lambda *_args, key=key: self._forget(key))
            handle = widget.windowHandle()
            if handle is not None:  # shown again later: re-check right after Qt showed the NSWindow
                handle.visibleChanged.connect(lambda *_args: self._schedule(immediate=True))
        window.setIgnoresMouseEvents_(True)  # click-through until the cursor is over a target
        self._schedule(immediate=True)
        return True

    def _forget(self, key: int) -> None:
        self._overlays.pop(key, None)
        if not self._overlays:
            self.uninstall()

    # -- event monitors (run by AppKit on the main thread) --------------------------------------

    def _on_global_event(self, _event: Any) -> None:
        try:
            self._schedule()
        except Exception:  # never let an exception escape into AppKit
            self._report_failure()

    def _on_local_event(self, event: Any) -> Any:
        try:
            self._schedule()
        except Exception:  # never let an exception escape into AppKit
            self._report_failure()
        return event  # a local monitor must hand the event on, or Latkan would never see it

    def _schedule(self, immediate: bool = False) -> None:
        """Run :meth:`update` soon: now-ish, but no sooner than ``POINTER_INTERVAL_MS`` after the last."""
        if not any(_visible(overlay.widget) for overlay in self._overlays.values()):
            return  # all hidden: nothing to hover, and the flag of a hidden NSWindow is moot
        delay = 0
        if not immediate:
            remaining = POINTER_INTERVAL_MS - (time.monotonic() - self._last_update) * 1000.0
            delay = math.ceil(remaining) if remaining > 0 else 0
        if self._timer.isActive() and self._timer.remainingTime() <= delay:
            return
        self._timer.start(delay)

    # -- the decision ---------------------------------------------------------------------------

    def update(self) -> None:
        """Let every overlay look at the cursor, then set each one's ``ignoresMouseEvents``."""
        now = time.monotonic()
        self._last_update = now
        try:
            pos = _cursor_pos()
        except Exception:  # defensive
            self._report_failure()
            return
        capturing = False
        for key, overlay in list(self._overlays.items()):
            widget = overlay.widget
            if not shiboken6.isValid(widget):
                self._forget(key)
                continue
            try:
                capture = bool(widget.track_pointer(pos, now))
                if widget.isVisible():
                    self._sync(overlay, capture)
            except Exception:  # native call / widget going away
                self._report_failure()
                self._release(overlay)
                continue
            capturing = capturing or capture
        if capturing and not self._timer.isActive():
            self._timer.start(CAPTURE_RECHECK_MS)

    def _ns_window(self, overlay: _Overlay) -> Any:
        view = self._mods.objc.objc_object(c_void_p=int(overlay.widget.winId()))
        return view.window() if view is not None else None

    def _sync(self, overlay: _Overlay, capture: bool) -> None:
        window = self._ns_window(overlay)
        if window is None:
            return
        number = int(window.windowNumber())
        if number != overlay.window_number:
            overlay.window_number = number
            logger.debug("Overlay has a new NSWindow; applying its native settings again")
            self._configure_window(window)
        ignore = not capture
        if bool(window.ignoresMouseEvents()) != ignore:
            window.setIgnoresMouseEvents_(ignore)

    def _release(self, overlay: _Overlay) -> None:
        """After an error: click-through (charms unclickable) beats a window swallowing the screen."""
        try:
            window = self._ns_window(overlay)
            if window is not None:
                window.setIgnoresMouseEvents_(True)
        except Exception:
            logger.debug("Could not make the overlay click-through after an error", exc_info=True)

    def _report_failure(self) -> None:
        if not self._reported_failure:
            self._reported_failure = True
            logger.warning("Pointer tracking for the overlay failed", exc_info=True)
        else:
            logger.debug("Pointer tracking for the overlay failed", exc_info=True)


def _visible(widget: Any) -> bool:
    try:
        return shiboken6.isValid(widget) and bool(widget.isVisible())
    except RuntimeError:
        return False
