"""OS-neutral platform integration with safe defaults.

Every per-OS integration derives from :class:`PlatformIntegration`. The base class never touches
native APIs, so it is also what an unknown platform effectively gets: plain Qt behaviour and no
"Start at login" support.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from PySide6.QtCore import QRect, Qt

logger = logging.getLogger(__name__)

#: Module that ``python -m`` runs for source (non-frozen) launches.
ENTRY_MODULE = "latkan"
#: Command-line flag a login item passes, so Latkan can tell a login start from a manual one.
AUTOSTART_FLAG = "--autostart"


class PlatformIntegration:
    """Base platform integration. Every method is a safe no-op / neutral default.

    Subclasses override only what their OS needs. ``set_autostart`` is the one method that may raise
    (``OSError``), because the UI must be able to tell the user that the change did not happen.
    """

    name: str = "generic"
    #: Nesting depth of :meth:`foreground` blocks (class default; instances count their own).
    _foreground_depth: int = 0

    # -- application lifecycle ------------------------------------------------------------------

    def prepare_environment(self) -> None:
        """Adjust the process environment. Called BEFORE ``QApplication`` is created."""

    def configure_application(self, app: Any) -> None:
        """Apply OS tweaks to the freshly created ``QApplication``."""

    # -- overlay windows ------------------------------------------------------------------------

    def overlay_window_flags(self) -> Qt.WindowType:
        """Window flags for the transparent click-through overlay windows."""
        return (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )

    def configure_overlay(self, widget: Any) -> None:
        """Apply native tweaks to an overlay window after it is shown (its ``winId()`` is valid)."""

    def overlay_geometry(self, screen: Any) -> QRect:
        """Geometry (global logical px) an overlay window should cover on ``screen``."""
        return QRect(screen.geometry())

    def anchor_top_inset(self, screen: Any) -> int:
        """Logical px at the top of the overlay on ``screen`` where another program takes the clicks.

        The rope-top drag zone is made this much deeper so its full depth stays reachable below
        e.g. a desktop panel that receives presses even though the overlay is drawn above it.
        Base 0. Never raises for a live screen.
        """
        return 0

    def uses_window_mask(self) -> bool:
        """Whether the overlay needs a window mask (``QWidget.setMask``) to be click-through.

        Base True: the mask is the input shape (X11) or at least bounds the input area. False
        where the window system already hit-tests every pixel by its alpha, so fully transparent
        pixels let clicks through by themselves and the overlay's 1/255 hit pads are the input
        area; the overlay then sets no mask and saves pushing a new region to the OS every frame.
        Without a compositor (:meth:`compositing_active` False) the overlay shapes a mask anyway.
        """
        return True

    def compositing_active(self) -> bool:
        """Whether the window system composites translucent windows (alpha shows what is below).

        Base True. Where it is False, fully transparent overlay pixels would show as opaque black,
        so the overlay shapes its window mask to the painted pixels instead. Cheap enough to call
        whenever an overlay appears or the animation resumes. Never raises.
        """
        return True

    def watch_compositing(self, callback: Callable[[], None] | None) -> None:
        """Call ``callback`` (on the GUI thread) whenever :meth:`compositing_active` may have changed.

        ``None`` stops watching. Base: compositing never changes, so this is a no-op.
        """

    def dialogs_stay_on_top(self) -> bool:
        """Whether Latkan's message boxes should be kept above other windows.

        Base False. True where the window manager may refuse a tray app's dialog the focus and
        stack it below the active window (X11 focus-stealing prevention).
        """
        return False

    def tray_icon_is_mask(self) -> bool:
        """Whether the tray icon should be a template/mask image (macOS menu bar)."""
        return False

    def activate_app(self) -> None:
        """Bring Latkan to the front right before it shows a dialog (file picker, message box).

        Only needed where the app is never active on its own (a macOS accessory app has no Dock
        icon, so its dialogs would otherwise open behind other apps' windows). Never raises.
        Callers use :meth:`foreground`, which also records that the activation is Latkan's own.
        """

    @contextlib.contextmanager
    def foreground(self) -> Iterator[None]:
        """Run a dialog or popup menu in front: ``activate_app()``, then the ``with`` block.

        :attr:`in_foreground` stays True until the block ends (the dialog has returned), so the
        app activation the dialog itself causes is never mistaken for the user opening Latkan
        again (macOS reports both the same way, see ``app.py``). Blocks may nest. A failing
        activation is logged, never raised: a dialog behind other windows beats no dialog.
        """
        self._foreground_depth += 1
        try:
            try:
                self.activate_app()
            except Exception:  # native integration
                logger.warning("Could not bring the application to the front", exc_info=True)
            yield
        finally:
            self._foreground_depth -= 1

    @property
    def in_foreground(self) -> bool:
        """True while a :meth:`foreground` block (a dialog or menu of Latkan's own) runs."""
        return self._foreground_depth > 0

    # -- autostart ("Start at login") -----------------------------------------------------------

    def autostart_supported(self) -> bool:
        """Whether this platform can register Latkan to start at login."""
        return False

    def autostart_enabled(self) -> bool:
        """Whether Latkan is currently registered to start at login. Never raises."""
        return False

    def set_autostart(self, enabled: bool) -> None:
        """Register (``True``) or unregister (``False``) Latkan to start at login.

        Raises:
            OSError: the change could not be made. Unsupported platforms raise when enabling;
                disabling is always a harmless no-op there.
        """
        if enabled:
            raise OSError(f"Start at login is not supported on this platform ({self.name}).")

    def launch_command(self) -> list[str]:
        """Command line that starts Latkan the way the current process was started.

        Frozen builds (PyInstaller) return ``[sys.executable]``; source runs return
        ``[python, "-m", "latkan"]``.
        """
        if getattr(sys, "frozen", False):
            return [sys.executable]
        return [self._source_interpreter(), "-m", ENTRY_MODULE]

    # -- helpers for subclasses -----------------------------------------------------------------

    def _source_interpreter(self) -> str:
        """Interpreter used for source runs; on Windows prefers the console-less ``pythonw.exe``."""
        if sys.platform.startswith("win"):
            return windowless_python(sys.executable)
        return sys.executable

    def _checked_launch_command(self) -> list[str]:
        """``launch_command()`` validated for use in an autostart entry.

        Raises:
            OSError: the running executable cannot be determined (e.g. an embedded interpreter).
        """
        command = self.launch_command()
        if not command or not command[0]:
            raise OSError("Cannot determine the Latkan executable to start at login.")
        return command


def one_pixel_short(screen: Any) -> QRect:
    """``screen.geometry()`` with the height reduced by one pixel (never below 1).

    A topmost window that exactly covers a monitor counts as a full-screen app: Windows then
    auto-hides the taskbar and suppresses notifications, and Mutter 50+ (GNOME 50) treats an
    override-redirect one as full-screen and hides the top bar and dock. One row short avoids both.
    """
    g = screen.geometry()
    return QRect(g.x(), g.y(), g.width(), max(1, g.height() - 1))


def qpa_platform_name() -> str:
    """Lower-case name of the running Qt platform plugin ("windows", "cocoa", "xcb", "offscreen" ...).

    Empty when no Qt application exists. Native window tweaks only make sense on the OS's real
    plugin: headless plugins (offscreen, minimal) hand out fake window ids.
    """
    try:
        from PySide6.QtGui import QGuiApplication

        return str(QGuiApplication.platformName()).lower()
    except Exception:  # defensive: only used to decide whether native calls make sense
        logger.debug("Could not query the Qt platform name", exc_info=True)
        return ""


def windowless_python(executable: str) -> str:
    """Return ``pythonw.exe`` next to a Windows ``python*.exe`` if it exists, else ``executable``.

    Registering ``pythonw.exe`` for autostart avoids a console window flashing up at every login.
    """
    if not executable:
        return executable
    exe = Path(executable)
    lower = exe.name.lower()
    if lower.startswith("pythonw") or not (lower.startswith("python") and lower.endswith(".exe")):
        return executable
    candidate = exe.with_name("pythonw.exe")
    try:
        if candidate.is_file():
            return str(candidate)
    except OSError:
        logger.debug("Could not probe %s", candidate, exc_info=True)
    return executable


def atomic_write_bytes(path: Path, data: bytes, mode: int = 0o644) -> None:
    """Write ``data`` to ``path`` atomically (temp file in the same dir, fsync, ``os.replace``).

    Parent directories are created and, on POSIX, the file gets permission bits ``mode``
    (``mkstemp`` would otherwise leave it at 0600). Raises ``OSError`` on failure; no partial
    file is left behind.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if os.name == "posix":
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            logger.debug("Could not remove temporary file %s", tmp, exc_info=True)
        raise
