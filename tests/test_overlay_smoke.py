"""Headless smoke tests for latkan.overlay (OverlayManager + OverlayWindow).

Runs on the Qt ``offscreen`` platform. The animation loop is driven deterministically by calling
``OverlayManager.tick()`` with simulated time instead of waiting for the real timer.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QPoint, QPointF, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication, QImage, QMouseEvent, QRegion
from PySide6.QtWidgets import QApplication

from latkan import charms, config, menu, overlay, rendering
from latkan.config import CharmSpec, Settings
from latkan.overlay import BREEZE_INTERVAL_MS, FRAME_INTERVAL_MS, OverlayManager, OverlayWindow
from latkan.platform.base import PlatformIntegration

FRAME = 8 / 120  # simulated seconds per tick: the physics' max_substeps worth of fixed steps


class FakePlatform(PlatformIntegration):
    """Base integration that records configure_overlay calls and can fake screen geometry."""

    name = "fake"

    def __init__(self) -> None:
        self.configured: list[OverlayWindow] = []
        self.geometry_override: QRect | None = None
        self.inset = 0  # anchor_top_inset(), e.g. GNOME's top bar on Xorg
        self.composited = True  # compositing_active()
        self.compositing_queries = 0
        self.watcher = None  # the callback given to watch_compositing()
        self.window_mask = True  # uses_window_mask(); False = per-pixel alpha hit-testing (Windows)

    def configure_overlay(self, widget) -> None:
        self.configured.append(widget)

    def uses_window_mask(self) -> bool:
        return self.window_mask

    def anchor_top_inset(self, screen) -> int:
        return self.inset

    def compositing_active(self) -> bool:
        self.compositing_queries += 1
        return self.composited

    def watch_compositing(self, callback) -> None:
        self.watcher = callback

    def overlay_geometry(self, screen) -> QRect:
        if self.geometry_override is not None:
            return QRect(self.geometry_override)
        return super().overlay_geometry(screen)


# --------------------------------------------------------------------------- fixtures / helpers


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LATKAN_HOME", str(tmp_path / "home"))
    charms._clear_caches()
    yield tmp_path / "home"
    charms._clear_caches()


@pytest.fixture
def platform():
    return FakePlatform()


@pytest.fixture
def make_manager(qt_app, home, platform):
    created: list[OverlayManager] = []

    def factory(settings: Settings | None = None) -> OverlayManager:
        mgr = OverlayManager(settings if settings is not None else Settings.default(), platform)
        mgr.start()
        created.append(mgr)
        return mgr

    yield factory
    for mgr in created:
        mgr.shutdown()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture
def mgr(make_manager) -> OverlayManager:
    return make_manager()


def settle(mgr: OverlayManager, max_seconds: float = 90.0) -> float:
    """Tick with simulated time until the animation timer stops; returns simulated seconds."""
    elapsed = 0.0
    while mgr.animating and elapsed < max_seconds:
        mgr.tick(FRAME)
        elapsed += FRAME
    return elapsed


def run_for(mgr: OverlayManager, seconds: float) -> None:
    for _ in range(max(1, round(seconds / FRAME))):
        mgr.tick(FRAME)


def send_mouse(widget, etype: QEvent.Type, pos: QPointF, button=Qt.MouseButton.LeftButton, buttons=None):
    if buttons is None:
        buttons = Qt.MouseButton.NoButton if etype == QEvent.Type.MouseButtonRelease else button
    global_pos = widget.mapToGlobal(pos)
    modifiers = Qt.KeyboardModifier.NoModifier
    event = QMouseEvent(etype, QPointF(pos), QPointF(global_pos), button, buttons, modifiers)
    QApplication.sendEvent(widget, event)
    return event


def by_name(mgr: OverlayManager, name: str):
    return next(c for c in mgr.charms if c.spec.name == name)


def alpha_near(image: QImage, x: float, y: float, radius: int = 2) -> int:
    best = 0
    for dx in range(-radius, radius + 1):
        for dy in range(-radius, radius + 1):
            px, py = round(x) + dx, round(y) + dy
            if 0 <= px < image.width() and 0 <= py < image.height():
                best = max(best, QColor(image.pixelColor(px, py)).alpha())
    return best


# --------------------------------------------------------------------------- start / windows


def test_start_builds_one_window_per_screen_and_hangs_charms(mgr, platform):
    screens = QGuiApplication.screens()
    assert len(mgr.windows) == len(screens) >= 1
    win = mgr.primary_window()
    assert win is not None and win.qscreen is QGuiApplication.primaryScreen()
    assert win.isVisible()
    assert platform.configured == [win]  # configured once, after the first show
    assert win.geometry() == platform.overlay_geometry(win.qscreen)

    flags = win.windowFlags()
    for flag in (Qt.WindowType.FramelessWindowHint, Qt.WindowType.WindowStaysOnTopHint, Qt.WindowType.Tool):
        assert flags & flag
    for attr in (
        Qt.WidgetAttribute.WA_TranslucentBackground,
        Qt.WidgetAttribute.WA_ShowWithoutActivating,
        Qt.WidgetAttribute.WA_NoSystemBackground,
    ):
        assert win.testAttribute(attr)
    assert win.hasMouseTracking()

    assert [c.spec.name for c in win.charms] == ["nazar", "star"]
    for charm in win.charms:
        assert charm.window is win
        assert charm.rope.anchor_y == 0.0
        assert charm.rope.anchor_x == pytest.approx(charm.spec.x_frac * win.width())
        assert charm.rope.length == pytest.approx(charm.spec.length)
        assert any(r is charm.rope for r in mgr.sim.ropes)
        # new ropes start tilted so they swing in
        assert abs(charm.rope.end()[0] - charm.rope.anchor_x) > 10
    assert mgr.animating


def test_animation_settles_stops_timer_and_leaves_exact_mask(mgr):
    win = mgr.primary_window()
    elapsed = settle(mgr)
    assert not mgr.animating, f"still animating after {elapsed:.1f} simulated seconds"
    assert mgr.sim.settled()

    mask = win.mask()
    assert not mask.isEmpty()
    scale = mgr.settings.scale
    for charm in win.charms:
        ex, ey = charm.rope.end()
        assert ex == pytest.approx(charm.rope.anchor_x, abs=1.0)
        assert ey == pytest.approx(charm.rope.length, rel=0.02)
        assert mask.contains(charm.body_center(scale).toPoint())
        assert mask.contains(QPoint(round(charm.rope.anchor_x), 3))  # rope top is grabbable
    # far from every charm the overlay is click-through
    assert not mask.contains(QPoint(20, win.height() - 20))
    assert not mask.contains(QPoint(20, 20))


def test_wake_restarts_the_loop(mgr):
    settle(mgr)
    assert not mgr.animating
    mgr.wake()
    assert mgr.animating
    assert mgr.tick(FRAME) >= 1
    settle(mgr)
    assert not mgr.animating


# --------------------------------------------------------------------------- input


def test_grab_move_release_with_mouse_events(mgr):
    win = mgr.primary_window()
    settle(mgr)
    nazar = by_name(mgr, "nazar")
    center = nazar.body_center(mgr.settings.scale)
    end_before = QPointF(*nazar.rope.end())

    send_mouse(win, QEvent.Type.MouseButtonPress, center)
    assert nazar.rope.is_grabbed
    assert win.cursor().shape() == Qt.CursorShape.ClosedHandCursor
    assert mgr.animating

    offset = QPointF(80, -60)
    send_mouse(win, QEvent.Type.MouseMove, center + offset, buttons=Qt.MouseButton.LeftButton)
    run_for(mgr, 0.3)
    ex, ey = nazar.rope.end()
    target = end_before + offset  # the grabbed point keeps its offset from the rope end
    assert ex == pytest.approx(target.x(), abs=1.0)
    assert ey == pytest.approx(target.y(), abs=1.0)
    assert mgr.animating  # grabbed -> never idles

    send_mouse(win, QEvent.Type.MouseButtonRelease, center + offset)
    assert not nazar.rope.is_grabbed
    assert win.cursor().shape() != Qt.CursorShape.ClosedHandCursor
    mgr.tick(FRAME)
    assert not nazar.rope.settled()  # it swings back after release
    settle(mgr)
    assert not mgr.animating


def test_anchor_drag_persists_x_frac(mgr):
    win = mgr.primary_window()
    settle(mgr)
    nazar = by_name(mgr, "nazar")
    changes: list[int] = []
    mgr.changed.connect(lambda: changes.append(1))
    ax = nazar.rope.anchor_x

    send_mouse(win, QEvent.Type.MouseButtonPress, QPointF(ax + 5, 12))
    assert not nazar.rope.is_grabbed  # rope top drags the anchor, not the charm
    send_mouse(win, QEvent.Type.MouseMove, QPointF(ax - 150, 14), buttons=Qt.MouseButton.LeftButton)
    assert nazar.rope.anchor_x == pytest.approx(ax - 150)
    send_mouse(win, QEvent.Type.MouseMove, QPointF(-50, 14), buttons=Qt.MouseButton.LeftButton)
    assert nazar.rope.anchor_x == pytest.approx(8.0)  # clamped to the edge margin
    send_mouse(win, QEvent.Type.MouseMove, QPointF(ax - 150, 14), buttons=Qt.MouseButton.LeftButton)
    assert changes == []  # persisted only on release
    send_mouse(win, QEvent.Type.MouseButtonRelease, QPointF(ax - 150, 14))

    assert nazar.spec.x_frac == pytest.approx((ax - 150) / win.width())
    assert changes
    mgr.save_now()
    saved = config.load_settings()
    assert next(s for s in saved.charms if s.id == nazar.spec.id).x_frac == pytest.approx(nazar.spec.x_frac)
    settle(mgr)
    assert nazar.rope.end()[0] == pytest.approx(ax - 150, abs=1.0)  # the charm followed its anchor


def test_double_click_nudges(mgr):
    win = mgr.primary_window()
    settle(mgr)
    star = by_name(mgr, "star")
    send_mouse(win, QEvent.Type.MouseButtonDblClick, star.body_center(mgr.settings.scale))
    assert mgr.animating
    mgr.tick(FRAME)
    assert not star.rope.settled()
    # The other charm is not pushed. (Not `settled()`: a rope that just came to rest may still
    # sway by a fraction of a pixel and cross the settle threshold again while the loop runs.)
    assert by_name(mgr, "nazar").rope.speed() < 5.0


def test_hover_swing_pushes_only_when_enabled(mgr):
    win = mgr.primary_window()
    settle(mgr)
    nazar = by_name(mgr, "nazar")
    center = nazar.body_center(mgr.settings.scale)

    mgr.set_hover_swing(False)
    settle(mgr)
    win.hover(center - QPointF(60, 0), now=10.0)
    win.hover(center, now=10.02)
    assert not mgr.animating and nazar.rope.settled()
    assert win.cursor().shape() == Qt.CursorShape.OpenHandCursor  # grab affordance

    win.hover(QPointF(20, 700), now=11.0)  # leave the charm
    mgr.set_hover_swing(True)
    settle(mgr)
    win.hover(center - QPointF(60, 0), now=12.0)
    win.hover(center, now=12.02)  # 3000 px/s -> clamped to 1500 -> x0.35
    assert mgr.animating
    mgr.tick(FRAME)
    assert not nazar.rope.settled()
    assert nazar.rope.end()[0] > nazar.rope.anchor_x  # pushed in the cursor's direction
    # staying inside the charm does not keep pumping energy in
    speed = nazar.rope.speed()
    win.hover(center + QPointF(1, 0), now=12.03)
    assert nazar.rope.speed() == pytest.approx(speed)


def test_right_click_opens_charm_menu(mgr, monkeypatch):
    import latkan.menu as menu_module

    calls = []
    foreground = []

    class FakeMenu:
        def exec(self, pos):
            calls.append(pos)
            foreground.append((mgr.platform.in_foreground, win.in_menu))

    def fake_charm_menu(manager, charm, platform):
        calls.append((manager, charm, platform))
        return FakeMenu()

    monkeypatch.setattr(menu_module, "charm_menu", fake_charm_menu)
    win = mgr.primary_window()
    settle(mgr)
    star = by_name(mgr, "star")
    center = star.body_center(mgr.settings.scale)
    send_mouse(win, QEvent.Type.MouseButtonPress, center, button=Qt.MouseButton.RightButton)
    assert calls[0] == (mgr, star, mgr.platform)
    assert calls[1] == win.mapToGlobal(center).toPoint()
    assert foreground == [(True, True)] and not mgr.platform.in_foreground and not win.in_menu

    calls.clear()
    send_mouse(win, QEvent.Type.MouseButtonPress, QPointF(20, 700), button=Qt.MouseButton.RightButton)
    assert calls == []  # nothing under the cursor -> no menu


def test_screen_change_during_a_charm_menu_deletes_the_window_only_afterwards(mgr, monkeypatch):
    """The menu (and any dialog opened from it) runs a nested loop inside mousePressEvent.

    Qt runs a deleteLater() posted inside a nested loop within that loop, so disposing the
    window there would free it (and its QWidgetWindow) under the handler still on the stack.
    """
    win = mgr.primary_window()
    key = win.screen_name
    settle(mgr)
    star = by_name(mgr, "star")
    seen: dict[str, bool] = {}

    class NestedLoopMenu:
        def exec(self, pos):
            loop = QEventLoop()

            def screen_removed():  # e.g. a monitor dropping out while "About" is open
                mgr._on_screen_removed(win.qscreen)
                mgr._sync_windows()  # the coalesced sync, fired inside the nested loop
                QTimer.singleShot(30, loop.quit)  # let posted events (deferred deletes) run

            QTimer.singleShot(0, screen_removed)
            QTimer.singleShot(2000, loop.quit)  # safety net
            loop.exec()
            seen["alive in the menu"] = shiboken6.isValid(win)
            seen["replaced"] = mgr.windows.get(key) is not win

    monkeypatch.setattr(menu, "charm_menu", lambda manager, charm, platform: NestedLoopMenu())
    center = star.body_center(mgr.settings.scale)
    send_mouse(win, QEvent.Type.MouseButtonPress, center, button=Qt.MouseButton.RightButton)
    assert seen == {"alive in the menu": True, "replaced": True}
    assert shiboken6.isValid(win) and win.retired and not win.isVisible() and win.charms == []
    fresh = mgr.windows[key]  # the screen is still there here, so its overlay was rebuilt
    assert star.window is fresh and any(c is star for c in fresh.charms)
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not shiboken6.isValid(win)  # deleted once the menu's handler returned
    assert shiboken6.isValid(fresh)


def test_topmost_charm_is_hit_first(make_manager):
    first = CharmSpec.new_builtin("heart", 0.5, 150.0)
    second = CharmSpec.new_builtin("moon", 0.5, 150.0)
    mgr = make_manager(Settings(charms=[first, second]))
    settle(mgr)
    win = mgr.primary_window()
    hit, mode = win.charm_at(win.charms[0].body_center(mgr.settings.scale))
    assert mode == "charm" and hit.spec is second


# --------------------------------------------------------------------------- mutators


def test_add_and_remove_builtin_updates_settings(mgr):
    win = mgr.primary_window()
    changes: list[int] = []
    mgr.changed.connect(lambda: changes.append(1))

    heart = mgr.add_builtin("heart")
    assert heart is not None and heart.spec in mgr.settings.charms
    assert len(mgr.settings.charms) == 3
    assert heart.window is win and win.charms[-1] is heart  # newest on top
    assert heart.spec.screen == win.screen_name
    assert 110.0 <= heart.spec.length <= 230.0
    min_gap = min(abs(heart.rope.anchor_x - c.rope.anchor_x) for c in win.charms if c is not heart)
    assert min_gap >= rendering.CHARM_BASE_PX * mgr.settings.scale - 1e-6
    assert abs(heart.rope.end()[0] - heart.rope.anchor_x) > 10  # swings in
    assert changes and mgr.animating
    assert mgr._save_timer.isActive()  # debounced save pending

    mgr.remove(heart)
    assert heart.spec not in mgr.settings.charms and len(mgr.settings.charms) == 2
    assert all(c is not heart for c in win.charms)
    assert all(r is not heart.rope for r in mgr.sim.ropes)
    assert len(changes) == 2
    mgr.remove(heart)  # removing twice is a no-op
    assert len(changes) == 2

    with pytest.raises(ValueError):
        mgr.add_builtin("unicorn")


def test_debounced_save_writes_settings(mgr, home):
    mgr.add_builtin("clover")
    assert mgr._save_timer.isActive() and mgr._save_timer.interval() == 400
    assert not config.settings_path().exists()
    mgr.save_now()
    assert not mgr._save_timer.isActive()
    saved = config.load_settings()
    assert [s.name for s in saved.charms] == ["nazar", "star", "clover"]


def test_quit_flush_writes_only_unsaved_changes(mgr, home, monkeypatch):
    mgr.flush_save()  # nothing changed this session: the file is not (re)written
    assert not config.settings_path().exists()

    mgr.add_builtin("moon")
    mgr.flush_save()
    assert not mgr._save_timer.isActive()
    assert [s.name for s in config.load_settings().charms] == ["nazar", "star", "moon"]
    config.settings_path().unlink()
    mgr.flush_save()  # already on disk
    assert not config.settings_path().exists()

    def locked(_settings):
        raise PermissionError(13, "Permission denied")

    mgr.add_builtin("heart")
    with monkeypatch.context() as patch:
        patch.setattr(overlay, "save_settings", locked)
        mgr.save_now()  # the debounced save fails (logged) ...
    assert not config.settings_path().exists()
    mgr.flush_save()  # ... so quitting tries again
    assert [s.name for s in config.load_settings().charms] == ["nazar", "star", "moon", "heart"]


def _write_png(path, color=Qt.GlobalColor.red):
    image = QImage(64, 80, QImage.Format.Format_ARGB32)
    image.fill(color)
    assert image.save(str(path), "PNG")
    return path


def test_add_image_from_dialog_and_remove_deletes_file(mgr, tmp_path, monkeypatch):
    source = _write_png(tmp_path / "photo.png")

    class FakeDialog:
        result = (str(source), "Images")

        @staticmethod
        def getOpenFileName(parent, caption, directory, filters):  # noqa: N802 (Qt name)
            assert "*.png" in filters
            return FakeDialog.result

    monkeypatch.setattr(overlay, "QFileDialog", FakeDialog)
    charm = mgr.add_image_from_dialog()
    assert charm is not None and charm.spec.kind == "image"
    stored = config.charms_dir() / charm.spec.image
    assert stored.is_file()
    assert charm.pix.width() == 64  # loaded from the imported copy, not the nazar fallback

    mgr.remove(charm)
    assert not stored.exists()

    FakeDialog.result = ("", "")  # cancelled
    assert mgr.add_image_from_dialog() is None
    assert len(mgr.settings.charms) == 2


def test_add_image_failure_shows_warning(mgr, tmp_path, monkeypatch):
    bogus = tmp_path / "notes.png"
    bogus.write_text("definitely not an image", encoding="utf-8")
    warnings = []

    foreground = []

    class FakeDialog:
        @staticmethod
        def getOpenFileName(*_args):  # noqa: N802 (Qt name)
            foreground.append(mgr.platform.in_foreground)
            return str(bogus), "Images"

    class FakeMessageBox:
        @staticmethod
        def warning(parent, title, text):
            foreground.append(mgr.platform.in_foreground)
            warnings.append(text)

    monkeypatch.setattr(overlay, "QFileDialog", FakeDialog)
    monkeypatch.setattr(menu, "QMessageBox", FakeMessageBox)  # menu.message_box() shows it
    assert mgr.add_image_from_dialog() is None
    assert len(warnings) == 1 and "notes.png" in warnings[0]
    assert len(mgr.settings.charms) == 2
    assert foreground == [True, True] and not mgr.platform.in_foreground


def test_shared_image_file_is_kept_until_last_user_is_removed(make_manager, home, tmp_path):
    image = charms.import_image(_write_png(tmp_path / "shared.png"))
    specs = [CharmSpec.new_image(image, 0.3, 120.0), CharmSpec.new_image(image, 0.6, 140.0)]
    mgr = make_manager(Settings(charms=specs))
    stored = config.charms_dir() / image
    first, second = mgr.charms
    mgr.remove(first)
    assert stored.is_file()
    mgr.remove(second)
    assert not stored.exists()


def test_clear_all_hides_window(mgr):
    win = mgr.primary_window()
    mgr.clear_all()
    assert mgr.settings.charms == [] and mgr.charms == []
    assert not win.isVisible()
    assert not mgr.animating
    assert mgr.sim.ropes == []
    mgr.add_builtin("star")
    assert win.isVisible() and not win.mask().isEmpty()


def test_set_hidden_hides_and_restores_windows(mgr):
    win = mgr.primary_window()
    mgr.set_hidden(True)
    assert mgr.settings.hidden
    assert not win.isVisible()
    assert not mgr.animating
    mgr.wake()  # nothing to animate while hidden
    assert not mgr.animating
    mgr.set_hidden(False)
    assert win.isVisible()
    assert mgr.animating


def test_starting_hidden_keeps_windows_hidden(make_manager):
    settings = Settings.default()
    settings.hidden = True
    mgr = make_manager(settings)
    assert all(not w.isVisible() for w in mgr.windows.values())
    assert not mgr.animating


def test_scale_and_rope_style(mgr):
    win = mgr.primary_window()
    settle(mgr)
    small = win.mask().boundingRect()
    mgr.set_scale(1.6)
    assert mgr.settings.scale == pytest.approx(1.6)
    settle(mgr)
    big = win.mask().boundingRect()
    assert big.width() > small.width() and big.height() > small.height()

    mgr.set_rope_style("neon")
    assert mgr.settings.rope_style == "neon"
    with pytest.raises(ValueError):
        mgr.set_rope_style("spaghetti")


def test_change_length_clamps_to_screen(mgr):
    win = mgr.primary_window()
    nazar = by_name(mgr, "nazar")
    mgr.change_length(nazar, 100.0)
    assert nazar.spec.length == pytest.approx(0.85 * win.height())
    assert nazar.rope.length == pytest.approx(nazar.spec.length)
    mgr.change_length(nazar, 1e-4)
    assert nazar.spec.length == pytest.approx(40.0)
    mgr.change_length(nazar, 1.5)
    assert nazar.spec.length == pytest.approx(60.0)
    with pytest.raises(ValueError):
        mgr.change_length(nazar, 0.0)


def test_nudge_all_wakes_everything(mgr):
    settle(mgr)
    mgr.nudge_all()
    assert mgr.animating
    mgr.tick(FRAME)
    assert all(not c.rope.settled() for c in mgr.charms)


def test_breeze_keeps_loop_alive_at_30_fps(mgr):
    mgr.set_breeze(True)
    assert mgr.settings.breeze
    run_for(mgr, 40.0)  # the swing-in has long decayed; only the breeze moves the charms now
    assert mgr.animating
    assert mgr._timer.interval() == BREEZE_INTERVAL_MS
    assert any(c.rope.speed() > 0.5 for c in mgr.charms)
    offsets = [abs(c.rope.end()[0] - c.rope.anchor_x) for c in mgr.charms]
    assert max(offsets) < 40  # a gentle sway, not a storm

    mgr.wake()  # interaction -> back to full frame rate
    assert mgr._timer.interval() == FRAME_INTERVAL_MS
    mgr.set_breeze(False)
    settle(mgr)
    assert not mgr.animating


def breeze_frame(mgr: OverlayManager) -> None:
    """One tick of the length the timer's current interval stands for."""
    mgr.tick(1 / 60 if mgr._timer.interval() == FRAME_INTERVAL_MS else 1 / 30)


def test_breeze_stays_at_30_fps_with_long_ropes(make_manager, platform):
    # A long rope swaying in the breeze hangs well off vertical (and near 500 px it resonates),
    # though slowly: it used to count as "not settled" and run at 60 fps for good.
    platform.geometry_override = QRect(0, 0, 1920, 1080)
    specs = [CharmSpec.new_builtin("heart", 0.3, 500.0), CharmSpec.new_builtin("moon", 0.6, 600.0)]
    mgr = make_manager(Settings(charms=specs, breeze=True))
    assert [round(c.rope.length) for c in mgr.charms] == [500, 600]
    for _ in range(20 * 60):  # 20 s: the swing-in decays
        breeze_frame(mgr)
    fast = 0
    for _ in range(40 * 30):  # 40 s of breeze
        breeze_frame(mgr)
        fast += mgr._timer.interval() == FRAME_INTERVAL_MS
    assert fast == 0
    assert mgr.animating

    mgr.charms[0].rope.nudge(900.0)  # a hard push without wake(): only the speed test sees it ...
    breeze_frame(mgr)
    assert mgr._timer.interval() == FRAME_INTERVAL_MS  # ... gets the full frame rate again
    for _ in range(15 * 60):
        breeze_frame(mgr)
    assert mgr._timer.interval() == BREEZE_INTERVAL_MS


def test_breeze_after_a_stall_gives_no_kick(mgr):
    mgr.set_breeze(True)
    run_for(mgr, 40.0)
    # The first frame after an hour of sleep: QElapsedTimer counts through standby on Windows.
    mgr.tick(3600.0)
    assert max(r.speed() for r in mgr.sim.ropes) < 60.0
    for _ in range(round(2.0 / FRAME)):
        mgr.tick(FRAME)
        offsets = [abs(c.rope.end()[0] - c.rope.anchor_x) for c in mgr.charms]
        assert max(offsets) < 40  # still the gentle sway, not a fling


# --------------------------------------------------------------------------- screens


def test_unknown_screen_hangs_on_primary_and_keeps_its_name(make_manager):
    ghost = CharmSpec.new_builtin("moon", 0.4, 150.0, screen="GHOST-MONITOR")
    mgr = make_manager(Settings(charms=[ghost]))
    win = mgr.primary_window()
    assert [c.spec for c in win.charms] == [ghost]
    assert ghost.screen == "GHOST-MONITOR"
    mgr.save_now()
    assert config.load_settings().charms[0].screen == "GHOST-MONITOR"


def test_move_to_screen_and_screen_removal(mgr):
    primary = mgr.primary_window()
    nazar = by_name(mgr, "nazar")
    mgr.move_to_screen(nazar, "no-such-screen")
    assert nazar.window is primary and nazar.spec.screen is None

    # Simulate a second monitor by adding another overlay window for a fake screen key.
    second = OverlayWindow(mgr, primary.qscreen, "SECOND")
    mgr.windows["SECOND"] = second
    mgr.move_to_screen(nazar, "SECOND")
    assert nazar.window is second and nazar.spec.screen == "SECOND"
    assert second.charms == [nazar] and all(c is not nazar for c in primary.charms)
    assert nazar.rope.anchor_x == pytest.approx(nazar.spec.x_frac * second.width())
    assert second.isVisible()

    # The "monitor" goes away: the charm falls back to the primary screen but remembers its own.
    mgr._sync_windows()
    assert "SECOND" not in mgr.windows
    assert nazar.window is primary and nazar.spec.screen == "SECOND"
    assert any(c is nazar for c in primary.charms)
    assert not second.isVisible()


def test_screen_geometry_change_reanchors(mgr, platform):
    win = mgr.primary_window()
    settle(mgr)
    old = win.geometry()
    platform.geometry_override = QRect(old.x(), old.y(), old.width() // 2, old.height() // 2)
    win._on_screen_geometry_changed(win.qscreen.geometry())
    assert win.width() == old.width() // 2
    for charm in win.charms:
        assert charm.rope.anchor_x == pytest.approx(charm.spec.x_frac * win.width())
        assert charm.rope.length <= 0.85 * win.height() + 1e-6
    settle(mgr)
    for charm in win.charms:
        assert charm.rope.end()[0] == pytest.approx(charm.rope.anchor_x, abs=1.0)


# --------------------------------------------------------------------------- rendering


def test_rendered_window_shows_charms_and_is_transparent_elsewhere(mgr):
    win = mgr.primary_window()
    settle(mgr)
    image = win.grab().toImage()
    assert not image.isNull()
    dpr = image.devicePixelRatio() or 1.0
    scale = mgr.settings.scale
    for charm in win.charms:
        center = charm.body_center(scale)
        assert alpha_near(image, center.x() * dpr, center.y() * dpr) > 0, charm.spec.name
        assert alpha_near(image, charm.rope.anchor_x * dpr, 40 * dpr, radius=3) > 0  # the rope
    assert QColor(image.pixelColor(10, image.height() - 10)).alpha() == 0
    assert QColor(image.pixelColor(10, 10)).alpha() == 0


def test_click_targets_are_hit_testable_on_per_pixel_alpha_platforms(mgr):
    """Windows/macOS let clicks through alpha-0 pixels, so every click target carries alpha > 0."""
    win = mgr.primary_window()
    settle(mgr)
    image = win.grab().toImage()
    dpr = image.devicePixelRatio() or 1.0
    mask = win.mask()
    for charm in win.charms:
        ax = charm.rope.anchor_x
        edge = QPointF(ax + 13, 34)  # inside the anchor drag zone, well off the rope line
        assert win.charm_at(edge)[1] in ("anchor", "charm")
        assert mask.contains(edge.toPoint())
        assert QColor(image.pixelColor(round(edge.x() * dpr), round(edge.y() * dpr))).alpha() > 0
        # a corner of the charm rect (transparent in the artwork) is still a click target
        corner = rendering.charm_transform(charm.rope.end(), charm.rope.end_angle()).map(
            rendering.charm_local_rect(charm.pix, mgr.settings.scale).bottomLeft() + QPointF(2, -2)
        )
        assert QColor(image.pixelColor(round(corner.x() * dpr), round(corner.y() * dpr))).alpha() > 0


def test_mask_keeps_slack_while_animating_and_is_exact_at_rest(mgr, platform):
    """X11/macOS pay for every new mask (ShapeNotify / a new CAShapeLayer): not one per frame."""
    win = mgr.primary_window()
    settle(mgr)
    pushes: list[QRegion] = []
    real_set_mask = win.setMask

    def record(region):
        pushes.append(QRegion(region))
        real_set_mask(region)

    win.setMask = record
    mgr.nudge_all()
    frames = 0
    while mgr.animating and frames < 90 * 60:
        mgr.tick(1 / 60)
        frames += 1
        assert win._last_region.subtracted(win.mask()).isEmpty()  # every target stays clickable
    assert not mgr.animating and frames > 120
    assert 0 < len(pushes) < frames / 4  # the exact region changed on nearly every frame
    exact = win.mask()
    assert exact == win._last_region  # tight again at rest
    for charm in win.charms:  # beside the rope-top drag zone: in the slack, not in the rest mask
        assert not exact.contains(QPoint(round(charm.rope.anchor_x) + 14 + 10, 5))

    platform.composited = False  # without a compositor slack pixels would show black: exact always
    platform.watcher()
    mgr.nudge_all()
    for _ in range(30):
        mgr.tick(1 / 60)
        assert win.mask() == win._last_region


def test_per_pixel_alpha_platform_sets_no_mask(make_manager, platform):
    """Windows: the layered overlay is hit-tested by alpha, so the hit pads alone are the input area."""
    platform.window_mask = False
    mgr = make_manager()
    win = mgr.primary_window()
    repaints: list[QRegion | None] = []
    real_update = win.update

    def record(*args):
        repaints.append(QRegion(args[0]) if args else None)
        real_update(*args)

    win.update = record
    settle(mgr)
    mgr.nudge_all()
    for _ in range(40):
        before = win._last_area
        repaints.clear()
        mgr.tick(1 / 30)
        assert win.mask().isEmpty()
        # Nothing clips stale pixels without a mask: every repaint covers the old and the new area.
        assert repaints
        for region in repaints:
            assert region is None or (
                before.subtracted(region).isEmpty() and win._last_area.subtracted(region).isEmpty()
            )

    settle(mgr)
    assert win.isVisible() and win.mask().isEmpty()
    image = win.grab().toImage()
    dpr = image.devicePixelRatio() or 1.0
    for charm in win.charms:
        edge = QPointF(charm.rope.anchor_x + 13, 34)  # the empty part of the anchor drag zone
        assert QColor(image.pixelColor(round(edge.x() * dpr), round(edge.y() * dpr))).alpha() > 0
        corner = rendering.charm_transform(charm.rope.end(), charm.rope.end_angle()).map(
            rendering.charm_local_rect(charm.pix, mgr.settings.scale).bottomLeft() + QPointF(2, -2)
        )
        assert QColor(image.pixelColor(round(corner.x() * dpr), round(corner.y() * dpr))).alpha() > 0
    assert QColor(image.pixelColor(10, image.height() - 10)).alpha() == 0  # clicks pass through

    mgr.clear_all()
    assert not win.isVisible()  # an empty overlay still hides
    mgr.add_builtin("star")
    assert win.isVisible() and win.mask().isEmpty()


def test_anchor_zone_reaches_below_a_panel_that_takes_the_top_clicks(make_manager, platform):
    platform.inset = 32  # e.g. GNOME's top bar on Xorg: presses in that strip go to the Shell
    mgr = make_manager()
    win = mgr.primary_window()
    settle(mgr)
    assert win.anchor_depth == overlay.ANCHOR_GRAB_DEPTH + 32
    image = win.grab().toImage()
    dpr = image.devicePixelRatio() or 1.0
    mask = win.mask()
    for charm in win.charms:
        below_panel = QPointF(charm.rope.anchor_x + 13, 32 + 30)  # a full-depth zone below the bar
        assert win.charm_at(below_panel) == (charm, "anchor")
        assert mask.contains(below_panel.toPoint())
        pixel = QColor(image.pixelColor(round(below_panel.x() * dpr), round(below_panel.y() * dpr)))
        assert pixel.alpha() > 0  # the hit pad covers the deeper zone too

    platform.inset = 0  # the panel went away (availableGeometryChanged)
    win._on_screen_geometry_changed()
    assert win.anchor_depth == overlay.ANCHOR_GRAB_DEPTH
    for charm in win.charms:
        below_panel = QPointF(charm.rope.anchor_x + 13, 32 + 30)
        assert win.charm_at(below_panel) == (None, None)
        assert not win.mask().contains(below_panel.toPoint())


def test_without_a_compositor_the_mask_follows_the_painted_pixels(make_manager, platform):
    """No compositor (X11): transparent pixels inside the mask would show as black boxes."""
    platform.composited = False  # e.g. LXQt/Openbox, Xfce with compositing off, Xvfb/VNC
    mgr = make_manager()
    win = mgr.primary_window()
    settle(mgr)
    assert not win.composited
    image = win.grab().toImage()
    dpr = image.devicePixelRatio() or 1.0
    mask = win.mask()
    scale = mgr.settings.scale
    for charm in win.charms:
        ax = charm.rope.anchor_x
        assert mask.contains(charm.body_center(scale).toPoint())
        assert any(mask.contains(QPoint(round(ax) + dx, 20)) for dx in (-1, 0, 1))  # the rope line
        edge = QPoint(round(ax + 13), 34)  # the empty part of the anchor drag zone
        assert not mask.contains(edge)
        assert QColor(image.pixelColor(round(edge.x() * dpr), round(edge.y() * dpr))).alpha() == 0  # no pads
    # Every pixel the mask lets the X server show is actually painted (alpha >= 128).
    for rect in mask:
        for x in range(rect.left(), rect.right() + 1, 2):
            for y in range(rect.top(), rect.bottom() + 1, 2):
                assert QColor(image.pixelColor(round(x * dpr), round(y * dpr))).alpha() >= 100, (x, y)

    # Resting charms reuse their shape: no re-rendering while nothing moves.
    shapes = {key: entry[2] for key, entry in win._shape_cache.items()}
    win.update_mask()
    assert shapes and all(win._shape_cache[key][2] is region for key, region in shapes.items())

    platform.composited = True  # a compositor started; the platform's watch reports it
    platform.watcher()
    assert win.composited
    for charm in win.charms:
        assert win.mask().contains(QPoint(round(charm.rope.anchor_x + 13), 34))  # padded zone again

    platform.composited = False  # stopped again (KWin suspends it for a full-screen game) ...
    queries = platform.compositing_queries
    mgr.nudge_all()  # ... also noticed whenever the animation resumes
    assert platform.compositing_queries == queries + 1
    assert not win.composited
    settle(mgr)
    assert not win.mask().contains(QPoint(round(win.charms[0].rope.anchor_x + 13), 34))


def test_compositor_watch_follows_the_manager_lifetime(make_manager, platform):
    mgr = make_manager()
    assert platform.watcher is not None
    mgr.shutdown()
    assert platform.watcher is None


def test_lost_release_ends_the_drag(mgr):
    win = mgr.primary_window()
    settle(mgr)
    nazar = by_name(mgr, "nazar")
    center = nazar.body_center(mgr.settings.scale)
    send_mouse(win, QEvent.Type.MouseButtonPress, center)
    assert nazar.rope.is_grabbed
    # the release was delivered to some other window; the next move arrives with no buttons down
    send_mouse(win, QEvent.Type.MouseMove, center + QPointF(5, 0), button=Qt.MouseButton.NoButton)
    assert not nazar.rope.is_grabbed and not win.dragging
    settle(mgr)
    assert not mgr.animating


def test_real_timer_drives_animation_and_debounced_save(mgr):
    from PySide6.QtTest import QTest

    settle(mgr)
    nazar = by_name(mgr, "nazar")
    before = nazar.rope.end()
    mgr.nudge_all()
    QTest.qWait(150)  # let the 16 ms QTimer fire a few times
    assert nazar.rope.end() != before
    assert mgr._timer.timerType() == Qt.TimerType.PreciseTimer

    mgr.set_rope_style("silver")
    assert not config.settings_path().exists()
    QTest.qWait(600)  # > 400 ms debounce
    assert config.settings_path().exists()
    assert config.load_settings().rope_style == "silver"


# --------------------------------------------------------------------------- track_pointer


def test_track_pointer_reports_click_targets_and_swings_on_hover(mgr):
    win = mgr.primary_window()
    settle(mgr)
    nazar = by_name(mgr, "nazar")
    center = nazar.body_center(mgr.settings.scale)

    def at(local: QPointF) -> QPoint:
        return win.mapToGlobal(local.toPoint())

    assert not win.track_pointer(at(QPointF(20, win.height() - 20)), now=20.0)
    assert win.track_pointer(at(QPointF(nazar.rope.anchor_x + 13, 34)), now=20.1)  # anchor zone
    assert not win.track_pointer(at(QPointF(20, 20)), now=20.2)
    assert not mgr.animating
    assert win.track_pointer(at(center - QPointF(40, 0)), now=21.0)  # the charm's left edge
    assert win.track_pointer(at(center), now=21.02)  # entering it pushes, as a Qt hover does
    assert mgr.animating
    mgr.tick(FRAME)
    assert nazar.rope.end()[0] > nazar.rope.anchor_x
    assert win.cursor().shape() == Qt.CursorShape.OpenHandCursor

    far = at(QPointF(20, win.height() - 20))
    send_mouse(win, QEvent.Type.MouseButtonPress, nazar.body_center(mgr.settings.scale))
    assert win.dragging
    assert win.track_pointer(far, now=22.0)  # the whole drag takes the input, wherever it goes
    send_mouse(win, QEvent.Type.MouseButtonRelease, QPointF(20, win.height() - 20))
    assert not win.track_pointer(far, now=22.1)
    assert win.cursor().shape() != Qt.CursorShape.OpenHandCursor

    mgr.set_hidden(True)
    assert not win.track_pointer(at(center), now=23.0)  # hidden windows take nothing


# --------------------------------------------------------------------------- macOS input


class MacNSWindow:
    """An NSWindow as far as the macOS pointer tracker uses it."""

    def __init__(self, number: int) -> None:
        self.number = number
        self.ignores = False  # AppKit's initial value reads as NO
        self.ignore_calls: list[bool] = []
        self.level = None
        self.style = 0

    def setLevel_(self, value):
        self.level = value

    def setCollectionBehavior_(self, value):
        pass

    def setHidesOnDeactivate_(self, value):
        pass

    def setHasShadow_(self, value):
        pass

    def isKindOfClass_(self, cls):
        return True

    def styleMask(self):
        return self.style

    def setStyleMask_(self, value):
        self.style = value

    def respondsToSelector_(self, selector):
        return False

    def windowNumber(self):
        return self.number

    def ignoresMouseEvents(self):
        return self.ignores

    def setIgnoresMouseEvents_(self, value):
        self.ignores = bool(value)
        self.ignore_calls.append(bool(value))


class MacNSEvent:
    """Records NSEvent monitor registrations."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.monitors: list[tuple[str, int, object, tuple[str, int]]] = []
        self.removed: list[tuple[str, int]] = []

    def _add(self, kind, mask, handler):
        if self.fail:
            raise ValueError("no monitors for you")
        token = (kind, len(self.monitors))
        self.monitors.append((kind, mask, handler, token))
        return token

    def addGlobalMonitorForEventsMatchingMask_handler_(self, mask, handler):  # noqa: N802
        return self._add("global", mask, handler)

    def addLocalMonitorForEventsMatchingMask_handler_(self, mask, handler):  # noqa: N802
        return self._add("local", mask, handler)

    def removeMonitor_(self, token):  # noqa: N802
        self.removed.append(token)

    def handler(self, kind):
        return next(h for k, _mask, h, _token in self.monitors if k == kind)


@pytest.fixture
def mac(qt_app, home, monkeypatch):
    """A real OverlayManager on MacOSPlatform, with AppKit faked and the cursor injectable."""
    from latkan.platform import macos as macos_mod

    ns_windows: dict[int, MacNSWindow] = {}
    created: list[OverlayManager] = []

    class View:
        def __init__(self, win_id):
            self.win_id = win_id

        def window(self):
            if self.win_id not in ns_windows:
                ns_windows[self.win_id] = MacNSWindow(len(ns_windows) + 1)
            return ns_windows[self.win_id]

    cursor = [QPoint(0, 0)]
    monkeypatch.setattr(macos_mod, "_qpa_platform_name", lambda: "cocoa")
    monkeypatch.setattr(macos_mod, "_cursor_pos", lambda: QPoint(cursor[0]))

    def start(events: MacNSEvent | None = None) -> SimpleNamespace:
        events = events if events is not None else MacNSEvent()
        fake = macos_mod._PyObjC(
            AppKit=SimpleNamespace(
                NSEvent=events,
                NSPanel=object,
                NSApplication=SimpleNamespace(
                    sharedApplication=lambda: SimpleNamespace(activateIgnoringOtherApps_=lambda flag: None)
                ),
            ),
            objc=SimpleNamespace(objc_object=lambda *, c_void_p: View(c_void_p)),
        )
        monkeypatch.setattr(macos_mod, "_load_pyobjc", lambda: fake)
        plat = macos_mod.MacOSPlatform(home=home)
        manager = OverlayManager(Settings.default(), plat)
        manager.start()
        created.append(manager)
        win = manager.primary_window()

        def move_to(local: QPointF) -> None:
            cursor[0] = win.mapToGlobal(local.toPoint())
            plat._tracker.update()

        return SimpleNamespace(
            mgr=manager,
            platform=plat,
            events=events,
            win=win,
            move_to=move_to,
            ns_windows=ns_windows,
            ns=lambda: ns_windows[int(win.winId())],
        )

    yield start
    for manager in created:
        manager.shutdown()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_macos_overlay_takes_clicks_only_over_its_targets(mac):
    m = mac()
    settle(m.mgr)
    ns, tracker = m.ns(), m.platform._tracker
    assert tracker is not None and tracker.installed
    # one global + one local monitor for all overlays, mouse moves (and drags) only
    assert [(kind, mask) for kind, mask, _h, _t in m.events.monitors] == [
        ("global", 1 << 5),
        ("local", (1 << 5) | (1 << 6)),
    ]
    assert ns.ignores is True  # click-through from the start: never NO for the whole screen
    assert ns.level == 25 and ns.style & (1 << 7)  # non-activating panel
    nazar = by_name(m.mgr, "nazar")
    center = nazar.body_center(m.mgr.settings.scale)

    m.move_to(center)
    assert ns.ignores is False
    m.move_to(QPointF(20, m.win.height() - 20))
    assert ns.ignores is True
    m.move_to(QPointF(nazar.rope.anchor_x + 13, 34))  # the empty part of the anchor drag zone
    assert ns.ignores is False
    m.move_to(QPointF(20, 20))
    assert ns.ignores is True

    # the whole drag takes the input, even where the cursor leaves the charm behind
    m.move_to(center)
    send_mouse(m.win, QEvent.Type.MouseButtonPress, center)
    m.move_to(QPointF(20, m.win.height() - 20))
    assert ns.ignores is False
    assert tracker._timer.isActive()  # re-checked without mouse moves while it takes input
    send_mouse(m.win, QEvent.Type.MouseButtonRelease, QPointF(20, m.win.height() - 20))
    tracker.update()
    assert ns.ignores is True
    assert ns.ignore_calls == [True, False, True, False, True, False, True]  # only real changes


def test_macos_hover_swing_is_driven_by_the_event_monitors(mac):
    m = mac()
    settle(m.mgr)
    nazar = by_name(m.mgr, "nazar")
    center = nazar.body_center(m.mgr.settings.scale)
    event = object()
    assert m.events.handler("global")(event) is None
    assert m.events.handler("local")(event) is event  # a local monitor must hand the event on
    assert m.platform._tracker._timer.isActive()  # the work runs on a Qt timer, not in AppKit

    assert not m.mgr.animating
    m.move_to(center - QPointF(40, 0))
    m.move_to(center)  # Qt itself never reports this move while Latkan is inactive
    assert m.mgr.animating
    m.mgr.tick(FRAME)
    assert not nazar.rope.settled()


def test_macos_input_state_heals_and_follows_a_new_nswindow(mac):
    m = mac()
    settle(m.mgr)
    far = QPointF(20, m.win.height() - 20)
    ns = m.ns()
    ns.ignores = False  # e.g. Qt's setWindowFlags() rewrote it
    m.move_to(far)
    assert ns.ignores is True

    replacement = MacNSWindow(99)  # Qt re-created the NSWindow behind the same view
    m.ns_windows[int(m.win.winId())] = replacement
    m.move_to(far)
    assert replacement.ignores is True
    assert replacement.level == 25 and replacement.style & (1 << 7)


def test_macos_pointer_tracking_idles_while_hidden_and_resumes_on_show(mac):
    m = mac()
    tracker = m.platform._tracker
    m.mgr.set_hidden(True)
    tracker._timer.stop()  # (hiding itself schedules one last look)
    m.events.handler("global")(None)
    assert not tracker._timer.isActive()  # nothing visible: mouse moves cost nothing
    m.mgr.set_hidden(False)
    assert tracker._timer.isActive()  # re-checked right after Qt showed the window again


def test_macos_pointer_tracking_errors_never_reach_appkit(mac, monkeypatch):
    m = mac()
    settle(m.mgr)
    tracker = m.platform._tracker

    def boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    event = object()
    with monkeypatch.context() as patch:
        patch.setattr(tracker, "_schedule", boom)
        assert m.events.handler("local")(event) is event
        assert m.events.handler("global")(event) is None

    nazar = by_name(m.mgr, "nazar")
    m.move_to(nazar.body_center(m.mgr.settings.scale))
    assert m.ns().ignores is False
    monkeypatch.setattr(m.win, "track_pointer", boom)
    tracker.update()
    assert m.ns().ignores is True  # after an error: click-through, never a blocked screen


def test_macos_monitors_go_away_with_the_overlays(mac):
    m = mac()
    tracker = m.platform._tracker
    tokens = sorted(token for _k, _m, _h, token in m.events.monitors)
    m.mgr.shutdown()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert sorted(m.events.removed) == tokens
    assert not tracker.installed


def test_macos_without_monitors_input_is_left_to_the_window_server(mac):
    m = mac(MacNSEvent(fail=True))
    settle(m.mgr)
    assert not m.platform._tracker.installed
    # never set: NO would make the whole screen swallow clicks, YES would make charms unclickable
    assert m.ns().ignore_calls == []
    assert m.ns().level == 25
