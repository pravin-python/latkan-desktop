"""Tray and charm context menus (headless).

Most tests drive the menus against a small recording fake manager so they check menu behaviour
only; two integration tests use a real ``OverlayManager`` built from ``Settings.default()``.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QRect, Qt
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox

import latkan.menu as menu_mod
from latkan import __version__, charms
from latkan.config import CharmSpec, Settings
from latkan.menu import build_main_menu, charm_menu, rebuild_main_menu
from latkan.platform.base import PlatformIntegration

MAIN_ITEMS = [
    "Add charm",
    "Rope style",
    "Charm size",
    "Gentle breeze",
    "Swing on hover",
    "Nudge all",
    "Hide charms",
    "Remove all charms…",
    "About Latkan",
    "Quit Latkan",
]


# ------------------------------------------------------------------------------------ fixtures


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    return app if isinstance(app, QApplication) else QApplication([])


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("LATKAN_HOME", str(tmp_path))
    return tmp_path


class FakeManager:
    """Duck-typed OverlayManager: real Settings, fake windows, records every method call."""

    def __init__(self, settings: Settings, windows: dict[str, Any] | None = None) -> None:
        self.settings = settings
        self.windows: dict[str, Any] = windows or {}
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)

        def record(*args: Any) -> None:
            self.calls.append((name, args))

        return record

    def called(self, name: str) -> list[tuple[Any, ...]]:
        return [args for called, args in self.calls if called == name]


class FakePlatform(PlatformIntegration):
    name = "fake"

    def __init__(self, supported: bool = True, enabled: bool = False, error: OSError | None = None) -> None:
        self.supported, self.enabled, self.error = supported, enabled, error
        self.requests: list[bool] = []

    def autostart_supported(self) -> bool:
        return self.supported

    def autostart_enabled(self) -> bool:
        return self.enabled

    def set_autostart(self, enabled: bool) -> None:
        self.requests.append(enabled)
        if self.error is not None:
            raise self.error
        self.enabled = enabled


class FakeBox:
    """Stands in for QMessageBox's static dialogs (which would block headless)."""

    StandardButton = QMessageBox.StandardButton
    answer = QMessageBox.StandardButton.No
    log: list[tuple[str, tuple[Any, ...]]] = []

    @classmethod
    def question(cls, *args: Any) -> QMessageBox.StandardButton:
        cls.log.append(("question", args))
        return cls.answer

    @classmethod
    def warning(cls, *args: Any) -> QMessageBox.StandardButton:
        cls.log.append(("warning", args))
        return QMessageBox.StandardButton.Ok

    @classmethod
    def about(cls, *args: Any) -> None:
        cls.log.append(("about", args))


@pytest.fixture
def box(monkeypatch: pytest.MonkeyPatch) -> type[FakeBox]:
    FakeBox.log = []
    FakeBox.answer = QMessageBox.StandardButton.No
    monkeypatch.setattr(menu_mod, "QMessageBox", FakeBox)
    return FakeBox


def make_charm(name: str = "nazar", screen: str = "A") -> SimpleNamespace:
    spec = CharmSpec.new_builtin(name, 0.5, 150.0, screen=screen)
    rope = SimpleNamespace(anchor_x=100.0, end=lambda: (100.0, 150.0), nudge=lambda *a: None)
    return SimpleNamespace(spec=spec, rope=rope, pix=None, window=None)


def make_window(name: str, charms: list[SimpleNamespace]) -> SimpleNamespace:
    window = SimpleNamespace(screen_name=name, charms=charms, geometry=lambda: QRect(0, 0, 1920, 1080))
    for charm in charms:
        charm.window = window
    return window


def texts(menu: QMenu) -> list[str]:
    return [a.text() for a in menu.actions() if not a.isSeparator()]


def find(menu: QMenu, text: str) -> QAction:
    for action in menu.actions():
        if action.text() == text:
            return action
        sub = action.menu()
        if sub is not None:
            try:
                return find(sub, text)
            except LookupError:
                pass
    raise LookupError(text)


_ALIVE: list[QMenu] = []


@pytest.fixture(autouse=True)
def _release_menus() -> Iterator[None]:
    yield
    _ALIVE.clear()


def main_menu(mgr: Any, platform: PlatformIntegration | None = None) -> QMenu:
    """Build a main menu that stays alive for the rest of the test (its actions die with it)."""
    menu = QMenu()
    build_main_menu(menu, mgr, platform or PlatformIntegration())
    _ALIVE.append(menu)
    return menu


# ------------------------------------------------------------------------- integration (real)


@pytest.fixture
def real_manager(qapp: QApplication) -> Iterator[Any]:
    from latkan.overlay import OverlayManager

    mgr = OverlayManager(Settings.default(), PlatformIntegration())
    mgr.start()
    yield mgr
    shutdown = getattr(mgr, "shutdown", None)
    if callable(shutdown):
        shutdown()


def test_main_menu_on_real_manager_has_expected_items(real_manager: Any) -> None:
    menu = main_menu(real_manager)
    assert texts(menu) == MAIN_ITEMS  # base platform: no "Start at login"


def test_charm_menu_on_real_manager_has_remove_action(real_manager: Any) -> None:
    charm = next(c for w in real_manager.windows.values() for c in w.charms)
    menu = charm_menu(real_manager, charm, PlatformIntegration())
    items = texts(menu)
    assert items[:3] == ["Nudge", "Longer rope", "Shorter rope"]
    assert "Remove this charm" in items
    assert items[-len(MAIN_ITEMS) :] == MAIN_ITEMS

    before = len(real_manager.settings.charms)
    find(menu, "Remove this charm").trigger()
    assert len(real_manager.settings.charms) == before - 1
    find(menu, "Remove this charm").trigger()  # stale now: must be a harmless no-op
    assert len(real_manager.settings.charms) == before - 1


# ------------------------------------------------------------------------------ main menu


def test_add_charm_submenu_lists_builtins_with_icons(qapp: QApplication) -> None:
    mgr = FakeManager(Settings.default())
    add = find(main_menu(mgr), "Add charm").menu()
    builtins = [a for a in add.actions() if not a.isSeparator()]
    assert len(builtins) == 7  # 6 built-ins + "From your image…"
    assert builtins[-1].text() == "From your image…"
    assert all(not a.icon().isNull() for a in builtins[:6])
    builtins[1].trigger()
    builtins[-1].trigger()
    assert mgr.called("add_builtin") == [("heart",)]
    assert mgr.called("add_image_from_dialog") == [()]


def test_rope_style_is_an_exclusive_radio_group(qapp: QApplication) -> None:
    settings = Settings.default()
    settings.rope_style = "silver"
    mgr = FakeManager(settings)
    rope = find(main_menu(mgr), "Rope style").menu()
    checked = [a.text() for a in rope.actions() if a.isChecked()]
    assert checked == ["Silver chain"]
    assert rope.actions()[0].actionGroup().isExclusive()
    find(rope, "Neon glow").trigger()
    assert mgr.called("set_rope_style") == [("neon",)]
    assert [a.text() for a in rope.actions() if a.isChecked()] == ["Neon glow"]


def test_charm_size_radio_marks_current_scale(qapp: QApplication) -> None:
    settings = Settings.default()
    settings.scale = 1.3
    mgr = FakeManager(settings)
    size = find(main_menu(mgr), "Charm size").menu()
    assert texts(size) == ["60%", "80%", "100%", "130%", "160%"]
    assert [a.text() for a in size.actions() if a.isChecked()] == ["130%"]
    find(size, "60%").trigger()
    assert mgr.called("set_scale") == [(0.6,)]


def test_toggles_pass_the_new_state(qapp: QApplication) -> None:
    settings = Settings.default()
    settings.breeze, settings.hover_swing = False, True
    mgr = FakeManager(settings)
    menu = main_menu(mgr)
    breeze, hover = find(menu, "Gentle breeze"), find(menu, "Swing on hover")
    assert breeze.isCheckable() and not breeze.isChecked()
    assert hover.isCheckable() and hover.isChecked()
    breeze.trigger()
    hover.trigger()
    assert mgr.called("set_breeze") == [(True,)]
    assert mgr.called("set_hover_swing") == [(False,)]


def test_show_hide_label_follows_settings(qapp: QApplication) -> None:
    settings = Settings.default()
    mgr = FakeManager(settings)
    find(main_menu(mgr), "Hide charms").trigger()
    settings.hidden = True
    find(main_menu(mgr), "Show charms").trigger()
    assert mgr.called("set_hidden") == [(True,), (False,)]
    find(main_menu(mgr), "Nudge all").trigger()
    assert mgr.called("nudge_all") == [()]


def test_start_at_login_only_when_supported(qapp: QApplication) -> None:
    mgr = FakeManager(Settings.default())
    assert "Start at login" not in texts(main_menu(mgr, FakePlatform(supported=False)))
    platform = FakePlatform(supported=True, enabled=True)
    action = find(main_menu(mgr, platform), "Start at login")
    assert action.isCheckable() and action.isChecked()
    action.trigger()
    assert platform.requests == [False]


def test_start_at_login_failure_shows_warning(qapp: QApplication, box: type[FakeBox]) -> None:
    platform = FakePlatform(error=PermissionError("registry is read-only"))
    find(main_menu(FakeManager(Settings.default()), platform), "Start at login").trigger()
    assert platform.requests == [True]
    warnings = [args for kind, args in box.log if kind == "warning"]
    assert len(warnings) == 1
    assert "registry is read-only" in warnings[0][2]


def test_remove_all_asks_first(qapp: QApplication, box: type[FakeBox]) -> None:
    mgr = FakeManager(Settings.default())
    action = find(main_menu(mgr), "Remove all charms…")
    assert action.isEnabled()
    box.answer = QMessageBox.StandardButton.No
    action.trigger()
    assert mgr.called("clear_all") == []
    box.answer = QMessageBox.StandardButton.Yes
    action.trigger()
    assert mgr.called("clear_all") == [()]
    assert [kind for kind, _ in box.log] == ["question", "question"]


def test_remove_all_disabled_without_charms(qapp: QApplication) -> None:
    settings = Settings.default()
    settings.charms.clear()
    assert not find(main_menu(FakeManager(settings)), "Remove all charms…").isEnabled()


def test_about_box_content(qapp: QApplication, box: type[FakeBox]) -> None:
    find(main_menu(FakeManager(Settings.default()), FakePlatform()), "About Latkan").trigger()
    ((kind, args),) = box.log
    assert kind == "about"
    text = args[2]
    assert __version__ in text
    assert "Made with love in India" in text
    assert "fake integration" in text  # the platform name
    assert "Charms that hang" in text


def test_dialogs_run_in_the_foreground(qapp: QApplication, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every dialog is inside platform.foreground(), so macOS never mistakes it for a relaunch."""
    platform = FakePlatform(error=PermissionError("nope"))
    seen: list[tuple[str, bool]] = []

    class RecordingBox:
        StandardButton = QMessageBox.StandardButton

        @staticmethod
        def question(*_args: Any) -> QMessageBox.StandardButton:
            seen.append(("question", platform.in_foreground))
            return QMessageBox.StandardButton.Yes

        @staticmethod
        def warning(*_args: Any) -> None:
            seen.append(("warning", platform.in_foreground))

        @staticmethod
        def about(*_args: Any) -> None:
            seen.append(("about", platform.in_foreground))

    monkeypatch.setattr(menu_mod, "QMessageBox", RecordingBox)
    mgr = FakeManager(Settings.default())
    mgr.clear_all = lambda: seen.append(("clear_all", platform.in_foreground))  # type: ignore[attr-defined]
    menu = main_menu(mgr, platform)
    for text in ("Start at login", "Remove all charms…", "About Latkan"):
        find(menu, text).trigger()
    assert seen == [("warning", True), ("question", True), ("clear_all", True), ("about", True)]
    assert not platform.in_foreground


def test_dialogs_stay_on_top_where_the_platform_asks(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    """X11: a window manager may still refuse a tray app's dialog the focus; keep it above anyway."""

    class OnTopPlatform(FakePlatform):
        def dialogs_stay_on_top(self) -> bool:
            return True

    shown: list[dict[str, Any]] = []

    class RecordingBox(QMessageBox):
        answer = QMessageBox.StandardButton.Yes

        def exec(self) -> int:
            shown.append(
                {
                    "on_top": bool(self.windowFlags() & Qt.WindowType.WindowStaysOnTopHint),
                    "icon": self.icon(),
                    "text": self.text(),
                    "default": self.standardButton(self.defaultButton()) if self.defaultButton() else None,
                    "pixmap": not self.iconPixmap().isNull(),
                }
            )
            button = self.button(self.answer)
            if button is not None:
                button.click()  # as if the user clicked it
            return 0

    monkeypatch.setattr(menu_mod, "QMessageBox", RecordingBox)
    platform = OnTopPlatform(error=PermissionError("read-only"))
    mgr = FakeManager(Settings.default())
    menu = main_menu(mgr, platform)
    find(menu, "Start at login").trigger()
    find(menu, "Remove all charms…").trigger()
    assert mgr.called("clear_all") == [()]  # the answer of the on-top box counts
    RecordingBox.answer = QMessageBox.StandardButton.No
    find(menu, "Remove all charms…").trigger()
    assert mgr.called("clear_all") == [()]
    previous_icon = qapp.windowIcon()
    qapp.setWindowIcon(QIcon(charms.builtin_pixmap("nazar", 64)))  # app.py sets the app icon
    try:
        find(menu, "About Latkan").trigger()
    finally:
        qapp.setWindowIcon(previous_icon)

    warning, question, _question_no, about = shown
    assert all(box["on_top"] for box in shown)
    assert warning["icon"] == QMessageBox.Icon.Warning and "read-only" in warning["text"]
    assert question["icon"] == QMessageBox.Icon.Question
    assert question["default"] == QMessageBox.StandardButton.No  # Enter never removes everything
    assert "Made with love in India" in about["text"] and about["pixmap"]  # the app icon, like about()


def test_actions_opt_out_of_macos_menu_role_heuristics(qapp: QApplication) -> None:
    menu = main_menu(FakeManager(Settings.default()))
    for text in ("About Latkan", "Quit Latkan"):
        assert find(menu, text).menuRole() == QAction.MenuRole.NoRole


def test_rebuild_replaces_items_without_leaking_submenus(qapp: QApplication) -> None:
    settings = Settings.default()
    mgr = FakeManager(settings)
    menu = QMenu()
    for _ in range(3):
        rebuild_main_menu(menu, mgr, PlatformIntegration())
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert texts(menu) == MAIN_ITEMS
    submenus = menu.findChildren(QMenu, "", Qt.FindChildOption.FindDirectChildrenOnly)
    assert len(submenus) == 3
    settings.hidden = True
    rebuild_main_menu(menu, mgr, PlatformIntegration())
    assert "Show charms" in texts(menu)


def test_callbacks_wait_until_no_menu_action_runs(qapp: QApplication) -> None:
    events: list[object] = []

    def slot() -> None:  # e.g. an action that opens a dialog (a nested event loop)
        events.append(("running", menu_mod.actions_running()))
        menu_mod.call_after_actions(lambda: events.append("after"))
        toggle.trigger()  # another action from inside it: still deferred until the outer one ends
        events.append("slot done")

    menu = QMenu()
    _ALIVE.append(menu)
    action = menu_mod._add_action(menu, "Do it", slot)
    toggle = menu_mod._add_toggle(menu, "Flip", False, lambda state: events.append(("toggle", state)))
    assert not menu_mod.actions_running()
    action.trigger()
    assert events == [("running", True), ("toggle", True), "slot done", "after"]
    assert not menu_mod.actions_running()
    menu_mod.call_after_actions(lambda: events.append("now"))  # nothing runs: called right away
    assert events[-1] == "now"

    def boom() -> None:
        menu_mod.call_after_actions(lambda: events.append("after boom"))
        raise RuntimeError("slot failed")

    with pytest.raises(RuntimeError):
        menu_mod._run_action(boom)
    assert not menu_mod.actions_running() and events[-1] == "after boom"


# ------------------------------------------------------------------------------ charm menu


def test_charm_menu_actions_target_the_charm(qapp: QApplication) -> None:
    charm = make_charm()
    mgr = FakeManager(Settings.default(), {"A": make_window("A", [charm])})
    menu = charm_menu(mgr, charm, PlatformIntegration())
    assert "Move to screen" not in texts(menu)  # single screen
    for text in ("Nudge", "Longer rope", "Shorter rope", "Remove this charm"):
        find(menu, text).trigger()
    assert mgr.called("nudge") == [(charm,)]
    (grow,), (shrink,) = [args[1:] for args in mgr.called("change_length")]
    assert grow > 1.0 and shrink < 1.0 and abs(grow * shrink - 1.0) < 1e-9
    assert mgr.called("remove") == [(charm,)]


def test_charm_menu_ignores_removed_charm(qapp: QApplication) -> None:
    charm = make_charm()
    window = make_window("A", [charm])
    mgr = FakeManager(Settings.default(), {"A": window})
    menu = charm_menu(mgr, charm, PlatformIntegration())
    window.charms.clear()  # removed elsewhere while the menu was open
    for text in ("Nudge", "Longer rope", "Shorter rope", "Remove this charm"):
        find(menu, text).trigger()
    assert mgr.calls == []


def test_charm_menu_follows_charm_across_window_rebuild(qapp: QApplication) -> None:
    charm = make_charm()
    mgr = FakeManager(Settings.default(), {"A": make_window("A", [charm])})
    menu = charm_menu(mgr, charm, PlatformIntegration())
    rebuilt = SimpleNamespace(spec=charm.spec, rope=charm.rope, pix=None, window=None)
    mgr.windows = {"A": make_window("A", [rebuilt])}  # e.g. monitor re-plugged: new runtime object
    find(menu, "Remove this charm").trigger()
    assert mgr.called("remove") == [(rebuilt,)]


def test_move_to_screen_with_several_screens(qapp: QApplication) -> None:
    charm = make_charm(screen="A")
    mgr = FakeManager(Settings.default(), {"A": make_window("A", [charm]), "B": make_window("B", [])})
    menu = charm_menu(mgr, charm, PlatformIntegration())
    screens = find(menu, "Move to screen").menu()
    options = screens.actions()
    assert len(options) == 2
    assert [a.isChecked() for a in options] == [True, False]
    assert options[0].actionGroup().isExclusive()
    options[1].trigger()
    assert mgr.called("move_to_screen") == [(charm, "B")]
