"""Tray menu and per-charm context menu builders.

Menus are cheap and are rebuilt every time they open (see :func:`rebuild_main_menu`), so check
marks and the Show/Hide label always reflect the current settings.

Stale references: a per-charm menu can outlive its charm (the charm is removed from another
menu, or its screen is unplugged while the menu is open). Every per-charm action therefore
re-resolves the charm against the manager right before acting - first by identity, then by
``spec.id`` in case the windows were rebuilt - and quietly does nothing if it is gone.

macOS: every action gets ``QAction.MenuRole.NoRole``. Qt's default text heuristics would
otherwise move items such as "About ..." and "Quit ..." out of our menus into the application
menu, where an Accessory (no Dock icon) app never shows them.
"""

from __future__ import annotations

import functools
import html
import logging
import math
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING

import PySide6
from PySide6.QtCore import QCoreApplication, QRect, QSize, QSysInfo, Qt, qVersion
from PySide6.QtGui import QAction, QActionGroup, QGuiApplication, QIcon, QScreen
from PySide6.QtWidgets import QMenu, QMessageBox, QWidget

from latkan import APP_NAME, __version__, charms
from latkan.config import BUILTIN_NAMES, ROPE_STYLES, SCALES

if TYPE_CHECKING:
    from latkan.overlay import HangingCharm, OverlayManager, OverlayWindow
    from latkan.platform.base import PlatformIntegration

log = logging.getLogger(__name__)

__all__ = [
    "ROPE_STYLE_LABELS",
    "actions_running",
    "build_main_menu",
    "call_after_actions",
    "charm_menu",
    "message_box",
    "rebuild_main_menu",
]

#: Human labels for :data:`latkan.config.ROPE_STYLES`.
ROPE_STYLE_LABELS: dict[str, str] = {
    "gold": "Golden thread",
    "silver": "Silver chain",
    "neon": "Neon glow",
    "kalava": "Kalava (red thread)",
}

ICON_PX = 64
LENGTH_STEP = 1.2
NUDGE_SPEED = 900.0  # px/s at the charm end; fallback only, the manager's own nudge() is preferred
TAGLINE = "Charms that hang from the top of your screen on real rope physics."
_Button = QMessageBox.StandardButton


# ---------------------------------------------------------------------------------------- public API


def build_main_menu(menu: QMenu, mgr: OverlayManager, platform: PlatformIntegration) -> None:
    """Append the main Latkan items to ``menu`` (the tray menu, or the tail of a charm menu).

    Layout: Add charm ▸, Rope style ▸, Charm size ▸ | Gentle breeze, Swing on hover |
    Nudge all, Show/Hide charms | Start at login (only when supported) | Remove all charms… |
    About Latkan, Quit Latkan.
    """
    settings = mgr.settings

    add_menu = _add_submenu(menu, "Add charm")
    for name in BUILTIN_NAMES:
        _add_action(
            add_menu,
            charms.BUILTIN_LABELS.get(name, name.title()),
            functools.partial(mgr.add_builtin, name),
            icon=QIcon(charms.builtin_pixmap(name, ICON_PX)),
        )
    add_menu.addSeparator()
    _add_action(add_menu, "From your image…", mgr.add_image_from_dialog)

    rope_menu = _add_submenu(menu, "Rope style")
    rope_group = _exclusive_group(rope_menu)
    for style in ROPE_STYLES:
        _add_action(
            rope_menu,
            ROPE_STYLE_LABELS.get(style, style.title()),
            functools.partial(mgr.set_rope_style, style),
            checked=settings.rope_style == style,
            group=rope_group,
        )

    size_menu = _add_submenu(menu, "Charm size")
    size_group = _exclusive_group(size_menu)
    for scale in SCALES:
        _add_action(
            size_menu,
            f"{round(scale * 100)}%",
            functools.partial(mgr.set_scale, scale),
            checked=math.isclose(settings.scale, scale, abs_tol=1e-3),
            group=size_group,
        )

    menu.addSeparator()
    _add_toggle(menu, "Gentle breeze", settings.breeze, mgr.set_breeze)
    _add_toggle(menu, "Swing on hover", settings.hover_swing, mgr.set_hover_swing)

    menu.addSeparator()
    _add_action(menu, "Nudge all", mgr.nudge_all)
    # The label and the effect are fixed together at build time, so a stale menu can never do
    # the opposite of what it says.
    hidden = settings.hidden
    _add_action(
        menu, "Show charms" if hidden else "Hide charms", functools.partial(mgr.set_hidden, not hidden)
    )

    if _autostart_supported(platform):
        menu.addSeparator()
        _add_toggle(
            menu, "Start at login", _autostart_enabled(platform), functools.partial(_set_autostart, platform)
        )

    menu.addSeparator()
    _add_action(
        menu,
        "Remove all charms…",
        functools.partial(_confirm_remove_all, mgr, platform),
        enabled=bool(settings.charms),
    )

    menu.addSeparator()
    _add_action(menu, f"About {APP_NAME}", functools.partial(_show_about, platform))
    _add_action(menu, f"Quit {APP_NAME}", QCoreApplication.quit)


def rebuild_main_menu(menu: QMenu, mgr: OverlayManager, platform: PlatformIntegration) -> None:
    """Clear ``menu`` (deleting the actions and submenus it owns) and build the main menu into it.

    Connect this to the tray menu's ``aboutToShow``. Do not call it from inside one of the menu's
    own action handlers (defer it with a zero-timeout timer instead), nor while
    :func:`actions_running` (a dialog opened from an action keeps that action's menu on the
    stack, and a timer can fire inside the dialog): wait with :func:`call_after_actions`.
    """
    submenus = menu.findChildren(QMenu, "", Qt.FindChildOption.FindDirectChildrenOnly)
    menu.clear()  # deletes actions owned by the menu; submenus are owned separately
    for submenu in submenus:
        submenu.deleteLater()
    build_main_menu(menu, mgr, platform)


def charm_menu(mgr: OverlayManager, charm: HangingCharm, platform: PlatformIntegration) -> QMenu:
    """Build the right-click menu for ``charm``: charm actions, a separator, then the main menu.

    The caller owns (and usually ``exec()``\\ s) the returned parentless menu.
    """
    menu = QMenu()
    menu.setObjectName("LatkanCharmMenu")
    _add_action(menu, "Nudge", _for_charm(mgr, charm, _nudge_charm))
    _add_action(menu, "Longer rope", _for_charm(mgr, charm, lambda m, c: m.change_length(c, LENGTH_STEP)))
    _add_action(
        menu, "Shorter rope", _for_charm(mgr, charm, lambda m, c: m.change_length(c, 1.0 / LENGTH_STEP))
    )

    if len(mgr.windows) > 1:
        screens_menu = _add_submenu(menu, "Move to screen")
        group = _exclusive_group(screens_menu)
        current = charm.window.screen_name
        for index, (name, window) in enumerate(mgr.windows.items(), start=1):
            _add_action(
                screens_menu,
                _screen_label(index, name, window),
                _for_charm(mgr, charm, functools.partial(_move_to_screen, screen_name=name)),
                checked=name == current,
                group=group,
            )

    _add_action(menu, "Remove this charm", _for_charm(mgr, charm, lambda m, c: m.remove(c)))
    menu.addSeparator()
    build_main_menu(menu, mgr, platform)
    return menu


# ------------------------------------------------------------------------------------ menu helpers


def _escape_mnemonic(text: str) -> str:
    """Stop Qt from turning ``&`` in dynamic text (screen names) into keyboard accelerators."""
    return text.replace("&", "&&")


def _new_action(parent: QMenu, text: str) -> QAction:
    action = QAction(text, parent)
    action.setMenuRole(QAction.MenuRole.NoRole)
    return action


def _add_submenu(menu: QMenu, title: str) -> QMenu:
    submenu = menu.addMenu(title)
    submenu.menuAction().setMenuRole(QAction.MenuRole.NoRole)
    return submenu


def _exclusive_group(parent: QMenu) -> QActionGroup:
    group = QActionGroup(parent)
    group.setExclusive(True)
    return group


def _add_action(
    menu: QMenu,
    text: str,
    slot: Callable[[], object] | None = None,
    *,
    icon: QIcon | None = None,
    checked: bool | None = None,
    group: QActionGroup | None = None,
    enabled: bool = True,
) -> QAction:
    """Add a plain action; passing ``checked`` makes it checkable (a radio item with ``group``)."""
    action = _new_action(menu, text)
    if icon is not None:
        action.setIcon(icon)
    if checked is not None:
        action.setCheckable(True)
        action.setChecked(checked)
    if group is not None:
        group.addAction(action)
    action.setEnabled(enabled)
    if slot is not None:
        action.triggered.connect(lambda _checked=False: _run_action(slot))
    menu.addAction(action)
    return action


def _add_toggle(menu: QMenu, text: str, checked: bool, slot: Callable[[bool], object]) -> QAction:
    """Add a checkable action whose slot receives the new checked state."""
    action = _new_action(menu, text)
    action.setCheckable(True)
    action.setChecked(bool(checked))
    action.triggered.connect(lambda state=False: _run_action(slot, bool(state)))
    menu.addAction(action)
    return action


# Menu action slots on the stack right now. A slot that opens a dialog runs a nested event loop,
# and the QMenu (or native tray menu item) that triggered it stays on the stack under it until
# the dialog closes; a menu rebuild meanwhile would delete it under its own event handler (Qt
# runs a deleteLater() posted from a timer inside a nested loop within that loop).
_actions_running = 0
_after_actions: list[Callable[[], object]] = []


def actions_running() -> bool:
    """True while a menu action's slot runs (for instance, a dialog it opened is still open)."""
    return _actions_running > 0


def call_after_actions(callback: Callable[[], object]) -> None:
    """Call ``callback`` once no menu action runs any more (right away when none does).

    It is then called while the menu that triggered the last action is still unwinding, so it
    must only schedule work (e.g. start a timer), never rebuild a menu itself.
    """
    if not _actions_running:
        callback()
    elif callback not in _after_actions:
        _after_actions.append(callback)


def _run_action(slot: Callable[..., object], *args: object) -> None:
    global _actions_running
    _actions_running += 1
    try:
        slot(*args)
    finally:
        _actions_running -= 1
        if not _actions_running:
            pending = list(_after_actions)
            _after_actions.clear()
            for callback in pending:
                try:
                    callback()
                except Exception:  # e.g. its tray is gone; never let it break the action
                    log.warning("A deferred menu callback failed", exc_info=True)


def _window_screen(window: OverlayWindow) -> QScreen | None:
    """The QScreen an overlay covers (window keys are not always plain screen names)."""
    screen = getattr(window, "qscreen", None)
    if screen is None:
        return None
    try:
        screen.name()  # raises RuntimeError once Qt deleted the screen (monitor unplugged)
    except RuntimeError:
        return None
    return screen


def _screen_label(index: int, name: str, window: OverlayWindow) -> str:
    screen = _window_screen(window)
    geometry: QRect = screen.geometry() if screen is not None else window.geometry()
    model = screen.model().strip() if screen is not None else ""
    label = f"{index}. {model or name} ({geometry.width()}×{geometry.height()})"
    primary = QGuiApplication.primaryScreen()
    if primary is not None and screen is not None and screen is primary:
        label += " - primary"
    return _escape_mnemonic(label)


# ---------------------------------------------------------------------------- per-charm actions


def _live_charm(mgr: OverlayManager, charm: HangingCharm) -> HangingCharm | None:
    """Return the charm as the manager currently knows it, or None if it is no longer hanging."""
    same_id: HangingCharm | None = None
    for window in list(mgr.windows.values()):
        for candidate in window.charms:
            if candidate is charm:
                return candidate
            if same_id is None and candidate.spec.id == charm.spec.id:
                same_id = candidate
    return same_id


def _for_charm(
    mgr: OverlayManager, charm: HangingCharm, action: Callable[[OverlayManager, HangingCharm], object]
) -> Callable[[], None]:
    """Wrap ``action`` so it runs only while ``charm`` still hangs (see the module docstring)."""

    def run() -> None:
        live = _live_charm(mgr, charm)
        if live is None:
            log.info("Charm %s is no longer hanging; ignoring a stale menu action", charm.spec.id)
            return
        action(mgr, live)

    return run


def _nudge_charm(mgr: OverlayManager, charm: HangingCharm) -> None:
    nudge = getattr(mgr, "nudge", None)  # OverlayManager.nudge(charm): same feel as double-click
    if callable(nudge):
        nudge(charm)
        return
    direction = -1.0 if charm.rope.end()[0] > charm.rope.anchor_x else 1.0  # swing back through centre
    charm.rope.nudge(direction * NUDGE_SPEED)
    mgr.wake()


def _move_to_screen(mgr: OverlayManager, charm: HangingCharm, *, screen_name: str) -> None:
    mgr.move_to_screen(charm, screen_name)


# ------------------------------------------------------------------------------ main-menu actions


def _autostart_supported(platform: PlatformIntegration) -> bool:
    try:
        return bool(platform.autostart_supported())
    except OSError as exc:
        log.warning("Could not check start-at-login support: %s", exc)
        return False


def _autostart_enabled(platform: PlatformIntegration) -> bool:
    try:
        return bool(platform.autostart_enabled())
    except OSError as exc:
        log.warning("Could not read the start-at-login state: %s", exc)
        return False


def message_box(
    platform: PlatformIntegration,
    kind: str,
    title: str,
    text: str,
    *,
    parent: QWidget | None = None,
    buttons: QMessageBox.StandardButton = _Button.Ok,
    default: QMessageBox.StandardButton = _Button.NoButton,
) -> QMessageBox.StandardButton:
    """Run a ``"warning"``, ``"question"`` or ``"about"`` box; call it inside ``platform.foreground()``.

    Normally Qt's static helper of that name. Where ``platform.dialogs_stay_on_top()`` (X11: the
    window manager may still refuse a tray app's dialog the focus and stack it below the active
    window), the same box is built with ``WindowStaysOnTopHint`` so it cannot hide there while it
    blocks the charms. Returns the button that closed it (``Ok`` for about boxes).
    """
    if not platform.dialogs_stay_on_top():
        if kind == "about":
            QMessageBox.about(parent, title, text)  # modeless on macOS, like every Mac about box
            return _Button.Ok
        if kind == "question":
            return QMessageBox.question(parent, title, text, buttons, default)
        return QMessageBox.warning(parent, title, text)
    icons = {"warning": QMessageBox.Icon.Warning, "question": QMessageBox.Icon.Question}
    box = QMessageBox(icons.get(kind, QMessageBox.Icon.NoIcon), title, text, buttons, parent)
    if kind == "about":  # what QMessageBox.about() shows: the application icon
        icon = box.windowIcon()
        box.setIconPixmap(icon.pixmap(icon.actualSize(QSize(64, 64))))
    if default != _Button.NoButton:
        box.setDefaultButton(default)
    box.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
    try:
        box.exec()
        clicked = box.clickedButton()
        return box.standardButton(clicked) if clicked is not None else _Button.NoButton
    finally:
        box.deleteLater()


def _set_autostart(platform: PlatformIntegration, enabled: bool) -> None:
    try:
        platform.set_autostart(enabled)
    except OSError as exc:
        verb = "turn on" if enabled else "turn off"
        log.warning("Could not %s start at login: %s", verb, exc)
        # foreground(): a macOS accessory app is never active; bring the dialog to the front.
        with platform.foreground():
            message_box(platform, "warning", APP_NAME, f"Could not {verb} “Start at login”.\n\n{exc}")
    else:
        log.info("Start at login %s", "enabled" if enabled else "disabled")


def _confirm_remove_all(mgr: OverlayManager, platform: PlatformIntegration) -> None:
    count = len(mgr.settings.charms)
    if count == 0:
        return
    what = "your charm" if count == 1 else f"all {count} charms"
    # clear_all() stays inside the block: on macOS an activation that arrives once no charms are
    # left must not look like Latkan being opened again (app.py).
    with platform.foreground():
        answer = message_box(
            platform,
            "question",
            f"{APP_NAME} - remove all charms",
            f"Remove {what} from your screen?\n\nThis cannot be undone.",
            buttons=QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            default=QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            mgr.clear_all()


def _platform_description(platform: PlatformIntegration) -> str:
    """E.g. ``Windows 11 Version 23H2 (x86_64), windows integration, Qt platform "windows"``."""
    product = QSysInfo.prettyProductName() or sys.platform
    arch = QSysInfo.currentCpuArchitecture()
    qpa = QGuiApplication.platformName()
    return f'{product} ({arch}), {platform.name} integration, Qt platform "{qpa}"'


def _about_text(platform: PlatformIntegration) -> str:
    python = ".".join(str(part) for part in sys.version_info[:3])
    return (
        f"<h3>{html.escape(APP_NAME)} {html.escape(__version__)}</h3>"
        f"<p>{html.escape(TAGLINE)}</p>"
        "<p>Made with love in India</p>"
        f"<p><small>Platform: {html.escape(_platform_description(platform))}<br>"
        f"Qt {html.escape(qVersion())} · PySide6 {html.escape(PySide6.__version__)} · "
        f"Python {python}</small></p>"
    )


def _show_about(platform: PlatformIntegration) -> None:
    with platform.foreground():
        message_box(platform, "about", f"About {APP_NAME}", _about_text(platform))
