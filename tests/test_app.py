"""Start-up decisions in latkan.app: the --autostart flag and macOS reopen handling."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, Qt, QTimer
from PySide6.QtWidgets import QApplication, QMenu

from latkan import app as app_mod
from latkan.config import Settings
from latkan.overlay import OverlayManager
from latkan.platform.base import AUTOSTART_FLAG, PlatformIntegration

ACTIVE = Qt.ApplicationState.ApplicationActive
INACTIVE = Qt.ApplicationState.ApplicationInactive


def test_autostart_flag_is_accepted_but_not_advertised(capsys: pytest.CaptureFixture[str]) -> None:
    parser = app_mod._build_parser()
    args, rest = parser.parse_known_args([AUTOSTART_FLAG])
    assert args.autostart is True and rest == []
    assert parser.parse_known_args([])[0].autostart is False
    parser.print_help()
    assert AUTOSTART_FLAG not in capsys.readouterr().out  # internal: the login item passes it


def _settings(*, hidden: bool = False, charms: bool = True) -> Settings:
    settings = Settings.default()
    settings.hidden = hidden
    if not charms:
        settings.charms.clear()
    return settings


@pytest.mark.parametrize(
    ("platform_id", "tray", "autostart", "settings", "unhide"),
    [
        ("darwin", True, False, _settings(hidden=True), True),  # started by hand: show them
        ("darwin", True, False, _settings(charms=False), True),
        ("darwin", True, True, _settings(hidden=True), False),  # login start keeps them hidden
        ("darwin", True, False, _settings(), False),  # nothing to do
        ("linux", True, False, _settings(hidden=True), False),  # the tray is there to show them
        ("win32", True, False, _settings(hidden=True), False),
        ("linux", False, True, _settings(hidden=True), True),  # no tray: nothing to click
        ("linux", False, False, _settings(), False),
    ],
)
def test_startup_unhide_reason(
    monkeypatch: pytest.MonkeyPatch,
    platform_id: str,
    tray: bool,
    autostart: bool,
    settings: Settings,
    unhide: bool,
) -> None:
    monkeypatch.setattr(sys, "platform", platform_id)
    assert (app_mod._startup_unhide_reason(tray, autostart, settings) is not None) is unhide


def _watcher(settings: Settings, *, launch_pending: bool, state=INACTIVE):
    platform = PlatformIntegration()
    reopened: list[int] = []
    watcher = app_mod._ReopenWatcher(
        SimpleNamespace(settings=settings),
        platform,
        lambda: reopened.append(1),
        state=state,
        launch_pending=launch_pending,
    )
    return watcher, platform, reopened


def test_reopen_shows_hidden_or_removed_charms() -> None:
    settings = _settings(hidden=True)
    watcher, _platform, reopened = _watcher(settings, launch_pending=False)
    watcher.on_state_changed(INACTIVE)
    assert reopened == []
    watcher.on_state_changed(ACTIVE)  # Finder / Dock / Spotlight opened Latkan.app again
    assert reopened == [1]
    watcher.on_state_changed(ACTIVE)  # Qt re-sends Active for a reopen of an active app
    assert reopened == [1, 1]

    settings.hidden = False  # charms visible: an activation is not a reason to do anything
    watcher.on_state_changed(INACTIVE)
    watcher.on_state_changed(ACTIVE)
    assert reopened == [1, 1]
    settings.charms.clear()
    watcher.on_state_changed(ACTIVE)
    assert reopened == [1, 1, 1]


def test_reopen_ignores_latkans_own_dialogs() -> None:
    watcher, platform, reopened = _watcher(_settings(hidden=True), launch_pending=False)
    with platform.foreground():  # e.g. "About Latkan" while the charms are hidden
        watcher.on_state_changed(ACTIVE)
    watcher.on_state_changed(INACTIVE)
    assert reopened == []
    watcher.on_state_changed(ACTIVE)
    assert reopened == [1]


def test_login_start_keeps_hidden_charms_hidden() -> None:
    watcher, _platform, reopened = _watcher(_settings(hidden=True), launch_pending=True)
    watcher.on_state_changed(ACTIVE)  # Qt activates a freshly launched app itself ...
    watcher.on_state_changed(INACTIVE)
    watcher.on_state_changed(ACTIVE)  # ... on macOS 14+ possibly twice
    assert reopened == []
    watcher.on_state_changed(ACTIVE)  # Active twice in a row: only a reopen does that
    assert reopened == [1]


def test_login_start_launch_phase_ends_when_the_user_acts() -> None:
    watcher, _platform, reopened = _watcher(_settings(hidden=True), launch_pending=True)
    watcher.on_settings_changed()  # e.g. "Hide charms" from the status item menu
    watcher.on_state_changed(ACTIVE)
    assert reopened == [1]

    watcher, platform, reopened = _watcher(_settings(hidden=True), launch_pending=True)
    with platform.foreground():
        watcher.on_state_changed(ACTIVE)
    watcher.on_state_changed(INACTIVE)
    watcher.on_state_changed(ACTIVE)
    assert reopened == [1]


def _spin(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def test_tray_menu_is_not_rebuilt_under_a_running_action(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A dialog opened from a tray (sub)menu action keeps that menu on the stack.

    A settings change meanwhile (e.g. a second launch un-hides the charms) fires the deferred
    refresh inside the dialog's nested loop; rebuilding there would delete the submenu whose
    mouse handler is still running.
    """
    monkeypatch.setenv("LATKAN_HOME", str(tmp_path))
    qapp = QApplication.instance() or QApplication([])
    manager = OverlayManager(Settings.default(), PlatformIntegration())
    seen: dict[str, bool] = {}

    def file_dialog(parent=None) -> None:  # stands in for add_image_from_dialog()'s dialog
        manager.changed.emit()
        _spin(50)  # the dialog's nested event loop: the refresh timer fires in here
        seen["submenu alive"] = shiboken6.isValid(add_menu)
        seen["rebuilt"] = tray.menu.actions()[0].menu() is not add_menu

    monkeypatch.setattr(manager, "add_image_from_dialog", file_dialog)
    tray = app_mod._Tray(qapp, manager, PlatformIntegration())
    try:
        add_menu = tray.menu.actions()[0].menu()
        assert isinstance(add_menu, QMenu) and add_menu.title() == "Add charm"
        from_image = next(a for a in add_menu.actions() if a.text() == "From your image…")
        from_image.trigger()
        assert seen == {"submenu alive": True, "rebuilt": False}
        _spin(20)  # the refresh re-armed once the action returned
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert not shiboken6.isValid(add_menu)  # replaced now, outside the action
        assert tray.menu.actions()[0].menu().title() == "Add charm"
    finally:
        tray.hide()
        tray.icon.deleteLater()
        tray.menu.deleteLater()
        manager.shutdown()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
