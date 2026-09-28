"""Windows integration: non-activating overlay windows and a per-user ``Run`` key for autostart."""

from __future__ import annotations

import logging
import subprocess
import sys
from typing import Any, Protocol

from PySide6.QtCore import QRect

from .. import APP_NAME
from .base import PlatformIntegration, one_pixel_short, windowless_python
from .base import qpa_platform_name as _qpa_platform_name

try:  # stdlib, but only present on Windows
    import winreg
except ImportError:  # pragma: no cover - exercised on macOS/Linux
    winreg = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
#: Explorer/Task Manager record "disabled at startup" here, overriding the Run entry.
STARTUP_APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
AUTOSTART_VALUE = APP_NAME

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020


class RegistryBackend(Protocol):
    """Minimal HKEY_CURRENT_USER accessor; tests substitute an in-memory fake."""

    def get(self, subkey: str, name: str) -> Any:
        """Return the value's data, or ``None`` when the key or value does not exist."""
        ...

    def set(self, subkey: str, name: str, value: str) -> None:
        """Create the key if needed and store ``value`` as ``REG_SZ``. Raises ``OSError``."""
        ...

    def delete(self, subkey: str, name: str) -> None:
        """Delete the value; a missing key/value is not an error. Raises ``OSError``."""
        ...


class WinRegistry:
    """:class:`RegistryBackend` backed by :mod:`winreg` (``HKEY_CURRENT_USER`` only)."""

    @property
    def available(self) -> bool:
        return winreg is not None

    @staticmethod
    def _winreg() -> Any:
        if winreg is None:
            raise OSError("The Windows registry is not available on this system.")
        return winreg

    def get(self, subkey: str, name: str) -> Any:
        reg = self._winreg()
        try:
            with reg.OpenKey(reg.HKEY_CURRENT_USER, subkey, 0, reg.KEY_QUERY_VALUE) as key:
                value, _value_type = reg.QueryValueEx(key, name)
        except FileNotFoundError:
            return None
        return value

    def set(self, subkey: str, name: str, value: str) -> None:
        reg = self._winreg()
        with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, subkey, 0, reg.KEY_SET_VALUE) as key:
            reg.SetValueEx(key, name, 0, reg.REG_SZ, value)

    def delete(self, subkey: str, name: str) -> None:
        reg = self._winreg()
        try:
            with reg.OpenKey(reg.HKEY_CURRENT_USER, subkey, 0, reg.KEY_SET_VALUE) as key:
                reg.DeleteValue(key, name)
        except FileNotFoundError:
            return


def _startup_approved_disabled(data: Any) -> bool:
    """Decode a ``StartupApproved`` value: an odd first byte (0x03, 0x07…) means user-disabled."""
    if isinstance(data, (bytes, bytearray)) and data:
        return bool(data[0] & 0x01)
    return False


class WindowsPlatform(PlatformIntegration):
    """Windows 10+ integration."""

    name = "windows"

    def __init__(self, registry: RegistryBackend | None = None) -> None:
        self.registry: RegistryBackend = registry if registry is not None else WinRegistry()
        self._user32: Any = None

    # -- overlay --------------------------------------------------------------------------------

    def overlay_geometry(self, screen: Any) -> QRect:
        """Screen geometry minus one pixel of height.

        A topmost window covering the whole monitor makes Windows treat it as a full-screen app
        (auto-hidden taskbar, suppressed notifications, "focus assist"); one pixel short avoids that.
        """
        return one_pixel_short(screen)

    def uses_window_mask(self) -> bool:
        """False on the real ``windows`` plugin: the overlay is a per-pixel-alpha layered window.

        Qt makes a frameless translucent window ``WS_EX_LAYERED`` and flushes it with
        ``UpdateLayeredWindowIndirect(ULW_ALPHA)``; Windows hit-tests such a window per pixel, so
        alpha-0 pixels pass clicks through and the 1/255 hit pads are exactly the input area. A
        mask (``SetWindowRgn`` with a redraw, on every frame something moves) only costs CPU.
        Headless plugins (offscreen, minimal: tests, CI) keep the mask.
        """
        return _qpa_platform_name() != "windows"

    def configure_overlay(self, widget: Any) -> None:
        """Add ``WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW`` so clicks never steal focus. Never raises."""
        if not sys.platform.startswith("win"):
            logger.debug("configure_overlay skipped: not running on Windows")
            return
        qpa = _qpa_platform_name()
        if qpa != "windows":
            # offscreen/minimal (tests, CI) hand out fake window ids: never poke those natively.
            logger.debug("configure_overlay skipped: Qt platform plugin is %r, not 'windows'", qpa)
            return
        try:
            self._apply_ex_style(int(widget.winId()), WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
        except Exception:  # native call: must never take the overlay down
            logger.warning("Could not apply extended window styles to the overlay", exc_info=True)

    def _load_user32(self) -> Any:
        if self._user32 is None:
            import ctypes
            from ctypes import wintypes

            # A private WinDLL instance so our prototypes never clash with other ctypes users.
            dll = ctypes.WinDLL("user32", use_last_error=True)
            get_long = getattr(dll, "GetWindowLongPtrW", None)
            set_long = getattr(dll, "SetWindowLongPtrW", None)
            long_type: Any = ctypes.c_ssize_t  # LONG_PTR
            if get_long is None or set_long is None:  # 32-bit user32 exports only the *LongW variants
                get_long, set_long, long_type = dll.GetWindowLongW, dll.SetWindowLongW, ctypes.c_long
            get_long.argtypes = [wintypes.HWND, ctypes.c_int]
            get_long.restype = long_type
            set_long.argtypes = [wintypes.HWND, ctypes.c_int, long_type]
            set_long.restype = long_type
            set_pos = dll.SetWindowPos
            set_pos.argtypes = [
                wintypes.HWND,
                wintypes.HWND,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.UINT,
            ]
            set_pos.restype = wintypes.BOOL
            self._user32 = (ctypes, get_long, set_long, set_pos)
        return self._user32

    def _apply_ex_style(self, hwnd: int, add: int) -> None:
        if not hwnd:
            logger.warning("Overlay has no native window handle; extended styles not applied")
            return
        ctypes, get_long, set_long, set_pos = self._load_user32()

        ctypes.set_last_error(0)
        current = get_long(hwnd, GWL_EXSTYLE)
        if current == 0 and ctypes.get_last_error():
            logger.warning("GetWindowLongPtrW failed for hwnd %#x (error %d)", hwnd, ctypes.get_last_error())
            return
        wanted = current | add
        if wanted == current:
            return

        ctypes.set_last_error(0)
        previous = set_long(hwnd, GWL_EXSTYLE, wanted)
        if previous == 0 and ctypes.get_last_error():
            logger.warning("SetWindowLongPtrW failed for hwnd %#x (error %d)", hwnd, ctypes.get_last_error())
            return
        # Style changes are cached by the window manager until the frame is recomputed.
        flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED
        if not set_pos(hwnd, None, 0, 0, 0, 0, flags):
            logger.debug("SetWindowPos(SWP_FRAMECHANGED) failed (error %d)", ctypes.get_last_error())
        logger.debug("Overlay hwnd %#x ex-style %#x -> %#x", hwnd, current, wanted)

    # -- autostart ------------------------------------------------------------------------------

    def autostart_supported(self) -> bool:
        return bool(getattr(self.registry, "available", True))

    def autostart_enabled(self) -> bool:
        try:
            command = self.registry.get(RUN_KEY, AUTOSTART_VALUE)
        except OSError:
            logger.warning("Could not read the autostart registry entry", exc_info=True)
            return False
        if not isinstance(command, str) or not command.strip():
            return False
        try:
            approved = self.registry.get(STARTUP_APPROVED_KEY, AUTOSTART_VALUE)
        except OSError:
            logger.debug("Could not read the StartupApproved entry", exc_info=True)
            return True
        return not _startup_approved_disabled(approved)

    def set_autostart(self, enabled: bool) -> None:
        try:
            if enabled:
                command = subprocess.list2cmdline(self._checked_launch_command())
                self.registry.set(RUN_KEY, AUTOSTART_VALUE, command)
                logger.info("Autostart enabled: HKCU\\%s\\%s = %s", RUN_KEY, AUTOSTART_VALUE, command)
            else:
                self.registry.delete(RUN_KEY, AUTOSTART_VALUE)
                logger.info("Autostart disabled")
        except OSError:
            logger.error("Could not %s autostart", "enable" if enabled else "disable", exc_info=True)
            raise
        # A "disabled" flag left by Task Manager would keep a fresh Run entry inert; clear it.
        self._clear_startup_approved()

    def _clear_startup_approved(self) -> None:
        try:
            self.registry.delete(STARTUP_APPROVED_KEY, AUTOSTART_VALUE)
        except OSError:
            logger.warning("Could not clear the StartupApproved entry", exc_info=True)

    def _source_interpreter(self) -> str:
        return windowless_python(sys.executable)
