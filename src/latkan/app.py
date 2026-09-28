"""Application entry point: CLI, logging, environment prep, QApplication, single instance, wiring.

Start-up order matters:

1. parse the command line (``--version`` / ``--help`` return before anything else happens);
2. configure logging (rotating file in ``log_dir()`` + stderr when there is a console);
3. ``platform.prepare_environment()`` - must run BEFORE ``QApplication`` exists (Linux switches
   Wayland sessions to XWayland here);
4. create ``QApplication``, then ``platform.configure_application(app)``;
5. single-instance check - a second launch only asks the running one to show itself and exits 0;
6. ``--reset`` (only in the primary, so a running instance cannot overwrite the fresh defaults);
7. load settings, start the overlay, create the tray icon, run the event loop. On macOS a manual
   start always shows the charms, and opening the running app again is watched for (it starts no
   second process there; see ``_ReopenWatcher``).
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import sys
import threading
from collections.abc import Callable, Sequence
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import FrameType, TracebackType
from typing import Any

from PySide6.QtCore import (
    QCoreApplication,
    QEvent,
    QMessageLogContext,
    QSocketNotifier,
    Qt,
    QTimer,
    QtMsgType,
    qInstallMessageHandler,
)
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from latkan import APP_NAME, __version__, charms
from latkan.config import Settings, backup_settings, load_settings, log_dir, save_settings, settings_path
from latkan.menu import actions_running, call_after_actions, rebuild_main_menu
from latkan.overlay import OverlayManager
from latkan.platform import get_platform
from latkan.platform.base import AUTOSTART_FLAG, PlatformIntegration
from latkan.singleinstance import SingleInstance

log = logging.getLogger(__name__)

__all__ = ["main"]

LOG_FILE_NAME = "latkan.log"
LOG_MAX_BYTES = 1_000_000
LOG_BACKUPS = 3
LOG_FORMAT = "%(asctime)s %(levelname)-8s [%(name)s] %(message)s"
DESKTOP_FILE_NAME = "latkan"  # packaging/linux/latkan.desktop
_HANDLER_MARK = "_latkan_handler"
_HEADLESS_QPA = frozenset({"offscreen", "minimal", "vnc"})
#: With no tray at start-up (a panel that starts after Latkan at login), look again this often ...
TRAY_RETRY_MS = 2000
#: ... this many times (about a minute) before settling for the per-charm right-click menu.
TRAY_RETRY_LIMIT = 30

_QT_LOG_LEVELS = {
    QtMsgType.QtDebugMsg: logging.DEBUG,
    QtMsgType.QtInfoMsg: logging.INFO,
    QtMsgType.QtWarningMsg: logging.WARNING,
    QtMsgType.QtCriticalMsg: logging.ERROR,
    QtMsgType.QtFatalMsg: logging.CRITICAL,
}
_qt_log = logging.getLogger("latkan.qt")
#: Identical Qt messages logged before further repeats are suppressed. Some Qt warnings fire on
#: every animation frame (e.g. "plugin does not support setting window masks"), which would
#: otherwise mean constant disk writes and log rotation.
QT_REPEAT_LIMIT = 3
_QT_SEEN_MAX = 512
_qt_seen: dict[str, int] = {}
_qt_seen_lock = threading.Lock()


# ------------------------------------------------------------------------------------------ entry


def main(argv: Sequence[str] | None = None) -> int:
    """Run Latkan. ``argv`` excludes the program name (defaults to ``sys.argv[1:]``).

    Returns the process exit code. Arguments Latkan does not know (e.g. ``-platform xcb``) are
    passed on to Qt.
    """
    has_console = sys.stderr is not None
    _ensure_std_streams()
    try:
        args, qt_args = _build_parser().parse_known_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as exc:  # --version, --help and usage errors: argparse already printed
        return _exit_code(exc)

    log_file = _configure_logging(debug=args.debug, console=has_console)
    restore_exception_hooks = _install_exception_hooks()
    previous_qt_handler = qInstallMessageHandler(_qt_message_handler)
    try:
        return _run(args, qt_args, log_file)
    finally:
        qInstallMessageHandler(previous_qt_handler)
        restore_exception_hooks()
        _remove_log_handlers()


def _run(args: argparse.Namespace, qt_args: list[str], log_file: Path | None) -> int:
    log.info("Starting %s %s (Python %s, %s)", APP_NAME, __version__, sys.version.split()[0], sys.platform)
    platform = get_platform()
    _call_native("prepare the environment", platform.prepare_environment)

    app = _create_application(qt_args, platform)
    try:
        single = SingleInstance()
        if not single.is_primary():
            if args.reset:
                log.warning("%s is already running; quit it first to use --reset", APP_NAME)
            return 0

        if args.reset:
            _reset_settings()

        settings = load_settings()
        manager = OverlayManager(settings, platform)
        manager.start()
        tray = _Tray(app, manager, platform)
        reason = _startup_unhide_reason(tray.available, args.autostart, manager.settings)
        if reason is not None:
            log.info("Showing the charms at start-up: %s", reason)
            _ensure_visible(manager)

        def on_activated() -> None:
            log.info("Latkan was launched again; showing the charms")
            _ensure_visible(manager)
            manager.nudge_all()

        single.activated.connect(on_activated)
        reopen: _ReopenWatcher | None = None  # a local of this frame: alive until exec returns
        if sys.platform == "darwin":
            state = QGuiApplication.applicationState()
            reopen = _ReopenWatcher(
                manager,
                platform,
                on_activated,
                state=state,
                launch_pending=args.autostart and state != Qt.ApplicationState.ApplicationActive,
            )
            app.applicationStateChanged.connect(reopen.on_state_changed)
            manager.changed.connect(reopen.on_settings_changed)
        # Only a change not on disk yet: a session that changed nothing never rewrites the file
        # (Windows emits aboutToQuit at session end and kills the process right after).
        app.aboutToQuit.connect(manager.flush_save)
        app.aboutToQuit.connect(tray.hide)
        app.aboutToQuit.connect(single.close)
        restore_signals = _install_signal_handlers(app)
    except Exception as exc:  # last-resort start-up handler: log it and tell the user
        log.critical("%s failed to start", APP_NAME, exc_info=True)
        _show_fatal_error(exc, log_file, platform)
        return 1

    try:
        # single, manager and tray are locals of this frame, so they stay alive until exec returns.
        code = app.exec()
    finally:
        restore_signals()
        # Tear the Qt objects down deterministically while QApplication still exists, instead of
        # leaving it to the garbage collector's (undefined) order when this frame is released.
        manager.shutdown()
        tray.hide()
        single.close()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    log.info("%s exited with code %s", APP_NAME, code)
    return int(code)


# ---------------------------------------------------------------------------------------- CLI


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="latkan",
        description="Charms that hang from the top of your screen on physics-simulated ropes.",
        allow_abbrev=False,
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    parser.add_argument(
        "--reset", action="store_true", help="back up the current settings and start again with the defaults"
    )
    parser.add_argument("--debug", action="store_true", help="verbose (DEBUG) logging")
    # Passed by the macOS login item: a login start keeps charms the user hid hidden, while
    # starting Latkan by hand there always shows them (see _startup_unhide_reason).
    parser.add_argument(AUTOSTART_FLAG, dest="autostart", action="store_true", help=argparse.SUPPRESS)
    return parser


def _exit_code(exc: SystemExit) -> int:
    if exc.code is None:
        return 0
    if isinstance(exc.code, int):
        return exc.code
    return 1


def _ensure_std_streams() -> None:
    """GUI launches (pythonw, PyInstaller --windowed) have ``sys.stdout``/``sys.stderr`` = None."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            # Kept open for the life of the process, like a real console stream.
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def _reset_settings() -> None:
    """``--reset``: move the current settings aside (``settings.json.bak-<n>``) and write defaults."""
    had_file = settings_path().exists()
    backup = backup_settings()
    if had_file and backup is None:
        log.error("Settings were not reset because the current file could not be backed up")
        return
    try:
        save_settings(Settings.default())
    except OSError as exc:
        log.error("Could not write default settings: %s", exc)
        return
    if backup is not None:
        log.info("Settings reset to defaults; the previous settings were saved as %s", backup)
    else:
        log.info("Settings reset to defaults")


# ------------------------------------------------------------------------------------ logging


def _configure_logging(*, debug: bool, console: bool) -> Path | None:
    """Rotating file log (1 MB x 3) in ``log_dir()`` plus stderr. Returns the log file, if any."""
    _remove_log_handlers()  # main() may run more than once in a process (tests)
    root = logging.getLogger()
    handlers: list[logging.Handler] = []
    log_file: Path | None = None
    file_error: OSError | None = None
    try:
        log_file = log_dir() / LOG_FILE_NAME
        handlers.append(
            RotatingFileHandler(log_file, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8")
        )
    except OSError as exc:
        file_error, log_file = exc, None
    if console and sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))

    formatter = logging.Formatter(LOG_FORMAT)
    for handler in handlers:
        handler.setFormatter(formatter)
        setattr(handler, _HANDLER_MARK, True)
        root.addHandler(handler)
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    if file_error is not None:
        log.warning("Logging to stderr only; could not open the log file: %s", file_error)
    return log_file


def _remove_log_handlers() -> None:
    """Detach and close the handlers :func:`_configure_logging` added (and only those)."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_MARK, False):
            root.removeHandler(handler)
            handler.close()


def _qt_message_handler(mode: QtMsgType, context: QMessageLogContext, message: str) -> None:
    """Route Qt's own warnings (qWarning & co.) into the Python log, rate-limiting repeats."""
    category = getattr(context, "category", None)
    text = f"{category}: {message}" if category and category != "default" else message
    with _qt_seen_lock:  # Qt may call this from any thread
        count = _qt_seen.get(text, 0)
        if count >= QT_REPEAT_LIMIT:
            return
        if len(_qt_seen) >= _QT_SEEN_MAX:
            _qt_seen.clear()
        _qt_seen[text] = count + 1
    suffix = " (further repeats suppressed)" if count + 1 == QT_REPEAT_LIMIT else ""
    _qt_log.log(_QT_LOG_LEVELS.get(mode, logging.WARNING), "%s%s", text, suffix)


def _install_exception_hooks() -> Callable[[], None]:
    """Log uncaught exceptions (including those raised in Qt slots) instead of losing them."""
    previous_sys_hook = sys.excepthook
    previous_thread_hook = threading.excepthook

    def sys_hook(exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            previous_sys_hook(exc_type, exc, tb)
            return
        log.critical("Uncaught exception", exc_info=(exc_type, exc, tb))

    def thread_hook(hook_args: threading.ExceptHookArgs) -> None:
        if hook_args.exc_type is SystemExit:
            return
        name = hook_args.thread.name if hook_args.thread is not None else "?"
        log.critical(
            "Uncaught exception in thread %s",
            name,
            exc_info=(hook_args.exc_type, hook_args.exc_value, hook_args.exc_traceback),
        )

    sys.excepthook = sys_hook
    threading.excepthook = thread_hook

    def restore() -> None:
        sys.excepthook = previous_sys_hook
        threading.excepthook = previous_thread_hook

    return restore


# ---------------------------------------------------------------------------------- application


def _call_native(what: str, func: Callable[[], object]) -> None:
    """Run a platform hook; a failing native tweak must never stop Latkan from starting."""
    try:
        func()
    except Exception:  # native/OS integration: log and carry on with plain Qt behaviour
        log.warning("Could not %s", what, exc_info=True)


def _create_application(qt_args: list[str], platform: PlatformIntegration) -> QApplication:
    existing = QApplication.instance()
    if isinstance(existing, QApplication):
        app = existing
    elif existing is not None:
        raise RuntimeError("a non-widget Qt application already exists in this process")
    else:
        program = sys.argv[0] if sys.argv and sys.argv[0] else "latkan"
        app = QApplication([program, *qt_args])
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName(APP_NAME)
    app.setDesktopFileName(DESKTOP_FILE_NAME)
    app.setQuitOnLastWindowClosed(False)  # overlays are tool windows; the tray keeps us alive
    app.setWindowIcon(charms.app_icon())
    _call_native("configure the application", lambda: platform.configure_application(app))
    log.debug("Qt platform plugin: %s", QGuiApplication.platformName())
    return app


def _ensure_visible(manager: OverlayManager) -> None:
    manager.set_hidden(False)
    if not manager.settings.charms:
        manager.add_builtin("nazar")


def _startup_unhide_reason(tray_available: bool, autostart: bool, settings: Settings) -> str | None:
    """Why hidden (or no) charms must be brought back at start-up; None to keep them as saved."""
    if not (settings.hidden or not settings.charms):
        return None
    if not tray_available:
        return "there is no system tray, so nothing else on screen could be clicked"
    if sys.platform == "darwin" and not autostart:
        # QSystemTrayIcon.isSystemTrayAvailable() is always true on macOS, but the status item
        # may not be drawn at all (no room beside the notch, hidden by the user or a menu-bar
        # manager). Starting Latkan by hand is then the one way back, so it always shows them.
        return "Latkan was started by hand"
    return None


class _ReopenWatcher:
    """macOS: an app activation Latkan did not cause itself means "Latkan was opened again".

    Opening Latkan.app while it runs (Finder, Dock, Spotlight, ``open``) starts no second
    process, so :class:`SingleInstance` never hears of it: LaunchServices activates the running
    app and sends it a reopen event, which Qt reports only as ``applicationStateChanged(Active)``
    (forced, even when already active). While the charms are hidden or gone, nothing of Latkan's
    can be clicked and the status item's native menu does not activate the app, so any other
    activation is either the launch itself or one of Latkan's own dialogs/menus
    (:meth:`PlatformIntegration.foreground`); both are ignored.

    ``launch_pending``: Qt activates a freshly started app itself (``applicationDidFinishLaunching``,
    on macOS 14+ twice). For a login start that must not un-hide the charms, so until the user
    has done something (settings changed, a dialog opened) a plain inactive -> active change is
    ignored. A reopen of an app that is already active still counts: Qt re-sends Active for it
    (``forcePropagate``), and nothing else emits Active twice in a row.
    """

    def __init__(
        self,
        manager: OverlayManager,
        platform: PlatformIntegration,
        on_reopen: Callable[[], None],
        *,
        state: Qt.ApplicationState,
        launch_pending: bool,
    ) -> None:
        self._manager = manager
        self._platform = platform
        self._on_reopen = on_reopen
        self._state = state
        self._launch_pending = launch_pending

    def on_settings_changed(self) -> None:
        self._launch_pending = False

    def on_state_changed(self, state: Qt.ApplicationState) -> None:
        previous, self._state = self._state, state
        if state != Qt.ApplicationState.ApplicationActive:
            return
        if self._platform.in_foreground:
            self._launch_pending = False  # the user is working with Latkan: the launch is over
            return  # and this activation is the dialog's or menu's own
        if self._launch_pending:
            if previous != Qt.ApplicationState.ApplicationActive:
                log.debug("Ignoring an activation during the launch")
                return
            self._launch_pending = False
        settings = self._manager.settings
        if settings.hidden or not settings.charms:
            self._on_reopen()


def _show_fatal_error(
    exc: BaseException, log_file: Path | None, platform: PlatformIntegration | None = None
) -> None:
    if QApplication.instance() is None or QGuiApplication.platformName() in _HEADLESS_QPA:
        return  # nobody could click the dialog away
    details = f"\n\nDetails were written to:\n{log_file}" if log_file is not None else ""
    text = f"{APP_NAME} could not start.\n\n{exc}{details}"
    if platform is None:
        QMessageBox.critical(None, APP_NAME, text)
        return
    with platform.foreground():
        QMessageBox.critical(None, APP_NAME, text)


class _Tray:
    """System tray icon plus its menu, rebuilt whenever it opens or the settings change."""

    def __init__(self, app: QApplication, manager: OverlayManager, platform: PlatformIntegration) -> None:
        self._manager = manager
        self._platform = platform
        self.available = QSystemTrayIcon.isSystemTrayAvailable()

        # macOS: a monochrome template image the menu bar tints for light/dark mode.
        icon = charms.tray_icon(mask=platform.tray_icon_is_mask())
        self.menu = QMenu()
        self.menu.setObjectName("LatkanTrayMenu")

        # Some tray hosts (Linux StatusNotifier/DBusMenu) show the exported menu without asking first,
        # so also rebuild after every change - deferred, because the change usually comes from one of
        # this menu's own actions, which must not be deleted while it is still being triggered.
        self._refresh = QTimer(self.menu)
        self._refresh.setSingleShot(True)
        self._refresh.setInterval(0)
        self._refresh.timeout.connect(self._rebuild_if_closed)
        manager.changed.connect(lambda: self._refresh.start())

        self._rebuild()
        self.menu.aboutToShow.connect(self._rebuild)

        self.icon = QSystemTrayIcon(icon, app)
        self.icon.setToolTip(APP_NAME)
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._on_activated)
        # Shown even when no tray is available yet: Qt attaches it if a tray appears later (e.g. a
        # panel that starts after Latkan at login).
        self.icon.show()
        self._tray_checks = 0
        self._tray_retry = QTimer(self.menu)
        self._tray_retry.setInterval(TRAY_RETRY_MS)
        self._tray_retry.timeout.connect(self._check_tray)
        if not self.available:
            log.warning(
                "No system tray is available (on GNOME install the AppIndicator extension). "
                "Right-click any charm to open the %s menu.",
                APP_NAME,
            )
            self._tray_retry.start()

    def hide(self) -> None:
        """Remove the icon from the tray (on Windows a destroyed process can leave a ghost icon)."""
        self._tray_retry.stop()
        self.icon.hide()

    def _check_tray(self) -> None:
        """Poll for a tray that appeared after start-up and (re-)register the icon with it."""
        self._tray_checks += 1
        if QSystemTrayIcon.isSystemTrayAvailable():
            self._tray_retry.stop()
            self.available = True
            log.info("A system tray became available; showing the %s icon", APP_NAME)
            self.icon.hide()  # re-register with the new tray host
            self.icon.show()
        elif self._tray_checks >= TRAY_RETRY_LIMIT:
            self._tray_retry.stop()
            log.info("Still no system tray; right-click any charm to open the %s menu", APP_NAME)

    def _rebuild(self) -> None:
        if actions_running():
            # A dialog opened from a menu action is still up (a timer or aboutToShow fired inside
            # it), and the (sub)menu that triggered the action is under it on the stack: deleting
            # it now would free it under its own event handler. Rebuild once the action returned.
            call_after_actions(self._refresh.start)
            return
        rebuild_main_menu(self.menu, self._manager, self._platform)

    def _rebuild_if_closed(self) -> None:
        if not self.menu.isVisible():  # an open menu was rebuilt on aboutToShow already
            self._rebuild()

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        # On macOS a click on the status item opens the menu; elsewhere a left click nudges.
        if reason == QSystemTrayIcon.ActivationReason.Trigger and sys.platform != "darwin":
            self._manager.nudge_all()


# -------------------------------------------------------------------------------- POSIX signals


def _install_signal_handlers(app: QApplication) -> Callable[[], None]:
    """Quit cleanly (settings saved) on Ctrl+C / SIGTERM.

    Python runs signal handlers only between bytecodes, never while Qt's C++ event loop is idle.
    ``signal.set_wakeup_fd`` makes the interpreter write to a socket when a signal arrives; a
    ``QSocketNotifier`` on the other end wakes the event loop, which runs a (Python) slot, which in
    turn lets the pending handler run. No polling timer, so idle CPU stays at 0 %.
    """
    installed: dict[int, Any] = {}

    def request_quit(signum: int, _frame: FrameType | None) -> None:
        log.info("Received signal %s; quitting", signum)
        app.quit()

    for name in ("SIGINT", "SIGTERM"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            installed[signum] = signal.signal(signum, request_quit)
        except (OSError, ValueError) as exc:  # ValueError: not the main thread
            log.debug("Cannot handle %s: %s", name, exc)

    notifier: QSocketNotifier | None = None
    sockets: tuple[socket.socket, ...] = ()
    previous_wakeup_fd = -1
    if installed:
        try:
            reader, writer = socket.socketpair()
            reader.setblocking(False)
            writer.setblocking(False)
            previous_wakeup_fd = signal.set_wakeup_fd(writer.fileno())
            sockets = (reader, writer)
            notifier = QSocketNotifier(reader.fileno(), QSocketNotifier.Type.Read, app)

            def drain() -> None:
                try:
                    reader.recv(64)
                except OSError:
                    pass  # spurious wake-up; nothing to read

            notifier.activated.connect(lambda *_args: drain())
        except (OSError, ValueError) as exc:
            log.debug("Signal wake-up bridge unavailable: %s", exc)
            for sock in sockets:
                sock.close()
            sockets = ()

    def restore() -> None:
        if notifier is not None:
            notifier.setEnabled(False)
            notifier.deleteLater()
        if sockets:
            try:
                signal.set_wakeup_fd(previous_wakeup_fd)
            except (OSError, ValueError) as exc:
                log.debug("Could not restore the signal wake-up fd: %s", exc)
            for sock in sockets:
                sock.close()
        for signum, handler in installed.items():
            try:
                signal.signal(signum, handler)
            except (OSError, ValueError, TypeError) as exc:
                log.debug("Could not restore handler for signal %s: %s", signum, exc)

    return restore
