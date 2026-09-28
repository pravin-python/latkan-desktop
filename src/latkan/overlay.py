"""Overlay windows and the runtime manager that owns the rope simulation.

Latkan draws its charms into one frameless, transparent, always-on-top :class:`OverlayWindow`
per connected screen. Each window is click-through everywhere except over its ropes and charms:
its input area is the union of small padded rectangles around every rope segment and charm body,
recomputed while something moves. Where the window system needs a window mask for that, the mask
is pushed only when the area outgrows the last one (with some slack while animating, exact at
rest); Windows hit-tests layered windows per pixel, so there the invisible hit pads alone do it.

:class:`OverlayManager` owns the :class:`~latkan.config.Settings`, the
:class:`~latkan.physics.Simulation` and the windows. It keeps the runtime state
(:class:`HangingCharm` = spec + rope + pixmap + window) in sync with ``settings.charms`` and the
connected screens, drives the animation loop and persists every change (debounced).

Coordinates: every rope lives in the logical-pixel coordinate system of its window with the
anchor on the top edge (``anchor_y == 0``) and ``anchor_x == spec.x_frac * window.width()``.

Idle cost: the animation timer stops as soon as every rope has settled, nothing is grabbed and
the breeze is off, so a resting desktop costs 0 % CPU. Any interaction calls
:meth:`OverlayManager.wake`.
"""

from __future__ import annotations

import logging
import math
import random
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import (
    QElapsedTimer,
    QEvent,
    QObject,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QStandardPaths,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QBitmap,
    QColor,
    QContextMenuEvent,
    QGuiApplication,
    QImage,
    QImageReader,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPixmap,
    QRegion,
    QResizeEvent,
    QScreen,
)
from PySide6.QtWidgets import QFileDialog, QWidget

from latkan import APP_NAME, charms, rendering
from latkan.config import BUILTIN_NAMES, ROPE_STYLES, CharmSpec, Settings, save_settings
from latkan.physics import Rope, Simulation

if TYPE_CHECKING:
    from latkan.platform.base import PlatformIntegration

logger = logging.getLogger(__name__)

__all__ = ["HangingCharm", "OverlayManager", "OverlayWindow"]

# --------------------------------------------------------------------------- tuning constants

#: Animation frame interval while something is moving / being interacted with (~60 fps).
FRAME_INTERVAL_MS = 16
#: Frame interval when only the breeze keeps the loop alive (~30 fps).
BREEZE_INTERVAL_MS = 33
#: With the breeze on (and no interaction) the loop drops to BREEZE_INTERVAL_MS once every rope
#: moves slower than this (px/s) ...
BREEZE_IDLE_SPEED = 60.0
#: ... and returns to FRAME_INTERVAL_MS only when one moves faster than this (or on wake()).
#: A breeze sway itself stays well below it at every rope length (~130 px/s at 900 px).
BREEZE_WAKE_SPEED = 150.0
#: Breeze force amplitude (px/s^2) of the primary sine term.
BREEZE_ACCEL = 60.0

#: Mask padding around each rope segment / charm body (logical px).
ROPE_MASK_PAD = 9.0
CHARM_MASK_PAD = 6.0
#: While animating, a window mask is replaced only when the input area leaves the last one pushed,
#: and then with this much extra room (px), so it is not re-sent to the window system every frame.
MASK_SLACK_PX = 24

#: The top of a rope can be dragged sideways within this box around its anchor.
ANCHOR_GRAB_DEPTH = 36.0
ANCHOR_GRAB_HALF_WIDTH = 14.0
#: Anchors never get closer than this to the left/right window edge.
ANCHOR_EDGE_MARGIN = 8.0

#: Rope length limits: absolute minimum and maximum as a fraction of the window height.
MIN_ROPE_LENGTH = 40.0
MAX_ROPE_FRACTION = 0.85
#: Random rope length range for newly added charms.
NEW_ROPE_MIN = 110.0
NEW_ROPE_MAX = 230.0
#: Newly added charms get placed within this band of the screen width.
NEW_X_MIN_FRAC = 0.08
NEW_X_MAX_FRAC = 0.92
#: Horizontal start offset of a new rope's end as a fraction of its length (so it swings in).
SWING_IN_MIN = 0.25
SWING_IN_MAX = 0.45

#: Nudge strength (px/s at the charm end) for double-click / "Nudge all".
NUDGE_SPEED = 900.0
#: Small nudge after a rope length change so the change is visible.
LENGTH_CHANGE_NUDGE = 180.0
#: Hover swing: the cursor velocity is clamped to this (px/s) and scaled by HOVER_TRANSFER.
HOVER_MAX_SPEED = 1500.0
HOVER_TRANSFER = 0.35
#: Hover samples older than this (s) are stale (the cursor was elsewhere, outside the mask).
HOVER_SAMPLE_MAX_AGE = 0.1
#: Assumed cursor speed (px/s) when the cursor enters a charm without a fresh velocity sample.
HOVER_FALLBACK_SPEED = 400.0
#: Hover pushes slower than this (after clamping, before scaling) are ignored.
HOVER_MIN_SPEED = 30.0

#: Settings writes are debounced by this many milliseconds.
SAVE_DEBOUNCE_MS = 400
#: Screen add/remove/primary-change notifications are coalesced over this many milliseconds.
SCREEN_SYNC_DELAY_MS = 50

_DRAG_CHARM = "charm"
_DRAG_ANCHOR = "anchor"

_TWO_PI = 2.0 * math.pi


#: Invisible wash painted over click targets (see OverlayWindow._paint_hit_pads).
_HIT_PAD_COLOR = QColor(0, 0, 0, 1)


def _clamp(value: float, lo: float, hi: float) -> float:
    return lo if value < lo else hi if value > hi else value


def _anchor_grab_rect(anchor_x: float, depth: float = ANCHOR_GRAB_DEPTH) -> QRectF:
    """Window-local area around a rope's top in which a press drags the anchor."""
    return QRectF(anchor_x - ANCHOR_GRAB_HALF_WIDTH, 0.0, 2 * ANCHOR_GRAB_HALF_WIDTH, depth)


def _breeze_phase(spec_id: str) -> float:
    """Stable per-charm breeze phase in ``[0, 2*pi)`` (``hash()`` is salted per process)."""
    return zlib.crc32(spec_id.encode("utf-8")) / 2**32 * _TWO_PI


def breeze_force(t: float, phase: float) -> float:
    """Horizontal breeze acceleration (px/s^2) at time ``t`` for a rope with ``phase``."""
    return BREEZE_ACCEL * (math.sin(t * 0.7 + phase) + 0.5 * math.sin(t * 1.9 + 2.0 * phase))


# --------------------------------------------------------------------------- runtime charm


@dataclass(eq=False)
class HangingCharm:
    """Runtime pairing of a persisted :class:`CharmSpec` with its rope, pixmap and window.

    Compared by identity. ``rope`` coordinates are local to ``window`` (anchor on its top edge).
    """

    spec: CharmSpec
    rope: Rope
    pix: QPixmap
    window: OverlayWindow
    phase: float = 0.0

    def body_center(self, scale: float) -> QPointF:
        """Centre of the charm body in window coordinates (handy for hit tests and tests)."""
        local = rendering.charm_local_rect(self.pix, scale).center()
        return rendering.charm_transform(self.rope.end(), self.rope.end_angle()).map(local)


# --------------------------------------------------------------------------- overlay window


class OverlayWindow(QWidget):
    """Transparent, click-through, always-on-top window covering one screen.

    Only the padded rope + charm areas receive mouse input; everything else passes through to
    the windows below. The window mask bounds that input area where the platform needs one
    (``PlatformIntegration.uses_window_mask``); Windows hit-tests the layered window per pixel
    instead, so the 1/255 hit pads are the input area there. (macOS decides input explicitly,
    from :meth:`track_pointer`; see ``platform/macos.py``.)
    """

    def __init__(self, manager: OverlayManager, screen: QScreen, screen_name: str | None = None) -> None:
        super().__init__(None, manager.platform.overlay_window_flags())
        # Plain attributes first: Qt may deliver events (resize, show) from the calls below.
        self.manager = manager
        self.screen_name: str = screen_name if screen_name is not None else (screen.name() or "screen")
        self.charms: list[HangingCharm] = []
        self._qscreen = screen
        self._configured = False
        self._retired = False  # its screen is gone; never show again (about to be deleted)
        self._menu_depth = 0  # show_charm_menu() calls on the stack (each runs a nested loop)
        self._dispose_pending = False  # dispose() came during a menu: delete once it returns
        self._last_mask: QRegion | None = None  # the mask last pushed with setMask (None: no mask)
        self._last_region: QRegion | None = None  # the exact input region of the last update_mask
        self._last_area: QRegion | None = None  # padded rope + charm rects: what painting may touch
        self._use_mask = self._query_uses_mask()
        # False: no compositor (X11), so the mask follows the painted pixels (see update_mask).
        self._composited = True
        self._shape_cache: dict[int, tuple[HangingCharm, tuple[object, ...], QRegion]] = {}
        self._anchor_depth = ANCHOR_GRAB_DEPTH
        self._was_active = False
        self._drag: tuple[HangingCharm, str] | None = None
        self._grab_offset = (0.0, 0.0)
        self._hover_charm: HangingCharm | None = None
        self._hover_sample: tuple[float, float, float] | None = None  # (x, y, monotonic time)

        self.setObjectName("LatkanOverlay")
        self.setWindowTitle(APP_NAME)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        # macOS: Qt reads this when it creates the NSWindow, so it must be set before the first
        # show() (platform.configure_overlay() runs after it). Ignored on other platforms.
        self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow, True)
        self.setAutoFillBackground(False)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(True)
        if hasattr(self, "setScreen"):  # QWidget.setScreen: Qt >= 6.0 on every platform we ship
            self.setScreen(screen)
        # Always set explicitly (even if equal to the default) so no window manager places us.
        self.apply_geometry(force=True)
        self._update_anchor_depth()
        screen.geometryChanged.connect(self._on_screen_geometry_changed)
        screen.availableGeometryChanged.connect(self._on_screen_geometry_changed)

    # ------------------------------------------------------------------ geometry / state

    @property
    def qscreen(self) -> QScreen:
        """The QScreen this window covers (``QWidget.screen()`` may differ transiently)."""
        return self._qscreen

    @property
    def dragging(self) -> bool:
        """True while the user drags one of this window's charms or anchors."""
        return self._drag is not None

    @property
    def retired(self) -> bool:
        """True once the screen went away; the window is hidden and awaiting disposal."""
        return self._retired

    def apply_geometry(self, force: bool = False) -> bool:
        """Cover ``platform.overlay_geometry(screen)``; return True when the geometry changed."""
        if self._retired:
            return False
        try:
            rect = self.manager.platform.overlay_geometry(self._qscreen)
        except RuntimeError:  # the QScreen was already destroyed (monitor unplugged)
            logger.debug("Screen of overlay %r is gone; keeping geometry", self.screen_name)
            return False
        if not rect.isValid() or (rect == self.geometry() and not force):
            return False
        logger.debug("Overlay %r geometry -> %s", self.screen_name, rect)
        self.setGeometry(rect)
        return True

    @property
    def anchor_depth(self) -> float:
        """Depth (px from the top edge) of the rope-top drag zone on this screen."""
        return self._anchor_depth

    def _update_anchor_depth(self) -> bool:
        """``ANCHOR_GRAB_DEPTH`` + ``platform.anchor_top_inset(screen)``; True when it changed."""
        if self._retired:
            return False
        try:
            inset = float(self.manager.platform.anchor_top_inset(self._qscreen))
        except RuntimeError:  # the QScreen was already destroyed (monitor unplugged)
            return False
        except Exception:  # platform hook: never let it take the overlay down
            logger.warning("Could not get the anchor inset for %r", self.screen_name, exc_info=True)
            return False
        depth = ANCHOR_GRAB_DEPTH + max(0.0, inset) if math.isfinite(inset) else ANCHOR_GRAB_DEPTH
        if depth == self._anchor_depth:
            return False
        logger.debug("Overlay %r anchor drag zone depth -> %s px", self.screen_name, depth)
        self._anchor_depth = depth
        return True

    @property
    def composited(self) -> bool:
        """False while no compositor runs: the mask then follows the painted pixels."""
        return self._composited

    def set_composited(self, active: bool) -> None:
        """Switch between the padded-rect mask (compositor) and the painted-pixel mask (none)."""
        active = bool(active)
        if active == self._composited:
            return
        logger.info(
            "Overlay %r: %s", self.screen_name, "compositing on" if active else "no compositor, shaped mask"
        )
        self._composited = active
        self._shape_cache.clear()
        if self.isVisible():
            self.refresh()
            self.update()  # the hit pads appear / go away

    def set_charms(self, charms_: list[HangingCharm]) -> None:
        """Replace the charms shown in this window (order = z-order, last on top) and refresh."""
        self.charms = list(charms_)
        if self._drag is not None and not any(c is self._drag[0] for c in self.charms):
            self.cancel_drag()
        if self._hover_charm is not None and not any(c is self._hover_charm for c in self.charms):
            self._hover_charm = None
        self.refresh()

    def cancel_drag(self, charm: HangingCharm | None = None) -> None:
        """Abort the current drag (only if it concerns ``charm`` when one is given)."""
        if self._drag is None:
            return
        dragged, mode = self._drag
        if charm is not None and dragged is not charm:
            return
        self._drag = None
        if mode == _DRAG_CHARM and dragged.rope.is_grabbed:
            dragged.rope.release()
        self.unsetCursor()

    def is_dragging_anchor(self, charm: HangingCharm) -> bool:
        return self._drag is not None and self._drag[0] is charm and self._drag[1] == _DRAG_ANCHOR

    def retire(self) -> None:
        """Hide for good: the screen went away and this window is about to be disposed."""
        self._retired = True
        self._hide_overlay()

    @property
    def in_menu(self) -> bool:
        """True while :meth:`show_charm_menu` runs (its nested event loop, or a dialog from it)."""
        return self._menu_depth > 0

    def dispose(self) -> None:
        """Retire the window, drop its charms and delete it; deferred while :attr:`in_menu`.

        The per-charm menu runs a nested event loop inside :meth:`mousePressEvent`, and so does
        every dialog opened from it (About, the file picker, "Remove all…"). A screen change
        handled meanwhile must not delete this window: Qt runs a ``deleteLater()`` posted inside
        a nested loop within that loop, and would free the widget and its QWidgetWindow under
        the mouse handler that is still on the stack. :meth:`show_charm_menu` deletes it instead,
        once the menu has returned.
        """
        self.retire()
        self.charms = []
        if self._menu_depth:
            self._dispose_pending = True
            return
        self.deleteLater()

    # ------------------------------------------------------------------ mask / repaint

    def _should_show(self) -> bool:
        return not self._retired and bool(self.charms) and not self.manager.settings.hidden

    def update_mask(self, exact: bool = True) -> bool:
        """Recompute the input region and apply it as the window mask; return whether it is visible.

        The region is the union of ``rendering.rope_bounds(pad 9)``, ``rendering.charm_bounds(pad
        6)`` and each rope's top drag zone (so the documented anchor-drag area is reachable
        everywhere). Without a compositor (X11) the X server would show every transparent pixel
        inside the mask as black, so the region is then the painted pixels themselves
        (alpha >= 128), like a classic shaped window.

        Where the platform needs no mask (Windows: per-pixel alpha hit-testing) none is set; the
        region still decides hide-when-empty and what :meth:`refresh` repaints. Otherwise
        ``exact=False`` (animation frames) keeps the mask pushed last while it still contains the
        region, and else pushes one ``MASK_SLACK_PX`` roomier: X11 (a ShapeNotify for the
        compositor) and macOS (a new CAShapeLayer) pay for every new mask. ``exact=True`` pushes
        the region itself (at rest; always without a compositor, where slack would show black).

        With no charms (or while hidden) the window is hidden instead: an empty mask means
        "no mask" to Qt, which would make the whole screen swallow clicks.
        """
        if not self._should_show():
            self._hide_overlay()
            return False
        scale = self.manager.settings.scale
        area = QRegion()
        shaped = QRegion()
        targets: list[QRect] = []
        cache: dict[int, tuple[HangingCharm, tuple[object, ...], QRegion]] = {}
        for charm in self.charms:
            rects = self._target_rects(charm, scale)
            targets.extend(rects)
            for rect in rects:
                area = area.united(rect)
            if not self._composited:
                shaped = shaped.united(self._painted_region(charm, rects, scale, cache))
        self._shape_cache = cache
        area = area.intersected(self.rect())
        region = area if self._composited else shaped.intersected(self.rect())
        if region.isEmpty():
            self._hide_overlay()
            return False
        if self._last_area is None or area != self._last_area:
            self._last_area = area
        self._last_region = region
        self._apply_mask(region, targets, exact or not self._composited)
        return True

    def _apply_mask(self, region: QRegion, targets: list[QRect], exact: bool) -> None:
        """Push ``region`` (or, not ``exact``, a roomier mask around ``targets``) when needed."""
        if self._composited and not self._use_mask:
            if self._last_mask is not None:  # e.g. the shaped mask of a time without a compositor
                self.clearMask()
                self._last_mask = None
            return
        pushed = self._last_mask
        if exact:
            if pushed is None or region != pushed:
                self.setMask(region)
                self._last_mask = region
            return
        if pushed is not None and region.subtracted(pushed).isEmpty():
            return  # still inside the mask pushed earlier
        slack = QRegion()
        for rect in targets:
            slack = slack.united(rect.adjusted(-MASK_SLACK_PX, -MASK_SLACK_PX, MASK_SLACK_PX, MASK_SLACK_PX))
        slack = slack.intersected(self.rect())
        self.setMask(slack)
        self._last_mask = slack

    def _target_rects(self, charm: HangingCharm, scale: float) -> list[QRect]:
        """Padded rects around one charm's drag zone, rope segments and body (window coords)."""
        rope = charm.rope
        # The rope-top drag zone is part of the input area even where the rope is thinner.
        rects = [_anchor_grab_rect(rope.anchor_x, self._anchor_depth).toAlignedRect()]
        rects.extend(rendering.rope_bounds(rope.points, ROPE_MASK_PAD))
        rects.append(rendering.charm_bounds(charm.pix, rope.end(), rope.end_angle(), scale, CHARM_MASK_PAD))
        return rects

    def _painted_region(
        self,
        charm: HangingCharm,
        rects: list[QRect],
        scale: float,
        cache: dict[int, tuple[HangingCharm, tuple[object, ...], QRegion]],
    ) -> QRegion:
        """The pixels ``paintEvent`` paints for ``charm`` with alpha >= 128 (no-compositor mask).

        Rendered at logical resolution, since the mask is in logical px. Cached per charm until
        its rope, the style, the scale or its pixmap change, so resting charms cost nothing.
        """
        box = QRect()
        for rect in rects:
            box = box.united(rect)
        box = box.intersected(self.rect())
        style = self.manager.settings.rope_style
        key: tuple[object, ...] = (tuple(charm.rope.points), style, scale, charm.pix.cacheKey())
        key += box.getRect()
        entry = self._shape_cache.get(id(charm))
        if entry is not None and entry[0] is charm and entry[1] == key:
            cache[id(charm)] = entry
            return entry[2]
        region = QRegion()
        if not box.isEmpty():
            image = QImage(box.size(), QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(Qt.GlobalColor.transparent)
            painter = QPainter(image)
            try:
                painter.translate(-box.x(), -box.y())
                self._draw_charm(painter, charm, style, scale)
            finally:
                painter.end()
            # createAlphaMask's default threshold dither keeps exactly the pixels with alpha >= 128.
            region = QRegion(QBitmap.fromImage(image.createAlphaMask()))
            region.translate(box.topLeft())
        cache[id(charm)] = (charm, key, region)
        return region

    def refresh(self, exact: bool = True) -> None:
        """Recompute the mask, show/hide as needed and schedule a repaint.

        ``exact=False`` (animation frames) lets :meth:`update_mask` keep a roomier mask.
        """
        if not self.isVisible() and self._should_show():
            # About to (re)appear: compositing may have been switched on or off meanwhile.
            self.set_composited(self._query_composited())
        previous = self._last_area
        if not self.update_mask(exact):
            return
        if not self.isVisible():
            self.show()
            self._configure_once()
            self.update()
            return
        # Everything we draw lies inside the padded rects, so only the old + new area can change.
        # Repainting just that (instead of the whole screen-sized translucent surface) is what
        # keeps the per-frame cost low, especially for layered windows on Windows. It must cover
        # the old area too: without a mask (Windows) nothing else clears stale pixels, which
        # would stay visible and clickable.
        current = self._last_area
        if current is None:
            self.update()
        elif previous is None or previous is current:
            self.update(current)
        else:
            self.update(previous.united(current))

    def _query_uses_mask(self) -> bool:
        try:
            return bool(self.manager.platform.uses_window_mask())
        except Exception:  # platform hook: a mask is always correct, just slower
            logger.warning("Could not ask whether the overlay needs a window mask", exc_info=True)
            return True

    def _query_composited(self) -> bool:
        try:
            return bool(self.manager.platform.compositing_active())
        except Exception:  # platform hook: keep the normal translucent mode
            logger.warning("Could not check whether a compositor runs", exc_info=True)
            return True

    def animate_frame(self, keep_alive: bool) -> None:
        """Called by the manager after the simulation advanced: refresh if anything moved."""
        active = (
            keep_alive
            or self._drag is not None
            or any(c.rope.is_grabbed or not c.rope.settled() for c in self.charms)
        )
        if active or self._was_active:  # one extra frame so the resting pose is drawn exactly
            self.refresh(exact=not active)  # (and gets an exact mask again)
        self._was_active = active

    def _hide_overlay(self) -> None:
        self.cancel_drag()
        self._hover_charm = None
        self._was_active = False
        if self.isVisible():
            self.hide()

    def _configure_once(self) -> None:
        if self._configured:
            return
        self._configured = True
        try:
            self.manager.platform.configure_overlay(self)
        except Exception:  # native integration: never let it take the overlay down
            logger.warning("Platform overlay configuration failed for %r", self.screen_name, exc_info=True)

    # ------------------------------------------------------------------ hit testing

    def charm_at(self, pos: QPointF) -> tuple[HangingCharm | None, str | None]:
        """Topmost charm under ``pos`` and what was hit (``"charm"`` body or rope ``"anchor"``)."""
        scale = self.manager.settings.scale
        for charm in reversed(self.charms):
            rope = charm.rope
            if rendering.charm_hit(pos, charm.pix, rope.end(), rope.end_angle(), scale):
                return charm, _DRAG_CHARM
        if pos.y() <= self._anchor_depth:
            for charm in reversed(self.charms):
                if abs(pos.x() - charm.rope.anchor_x) <= ANCHOR_GRAB_HALF_WIDTH:
                    return charm, _DRAG_ANCHOR
        return None, None

    def _update_cursor(self, mode: str | None) -> None:
        if mode == _DRAG_CHARM:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        elif mode == _DRAG_ANCHOR:
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        else:
            self.unsetCursor()

    # ------------------------------------------------------------------ Qt event handlers

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        try:
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            painter.fillRect(event.rect(), Qt.GlobalColor.transparent)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            style, scale = self.manager.settings.rope_style, self.manager.settings.scale
            if self._composited:  # without a compositor the pads would show as black boxes
                self._paint_hit_pads(painter, scale)
            for charm in self.charms:  # list order = z-order: the last added is drawn on top
                self._draw_charm(painter, charm, style, scale)
        finally:
            painter.end()

    @staticmethod
    def _draw_charm(painter: QPainter, charm: HangingCharm, style: str, scale: float) -> None:
        """Draw one rope and its charm (shared by paintEvent and the no-compositor mask)."""
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        rope = charm.rope
        rendering.draw_rope(painter, rope.points, style)
        rendering.draw_charm(painter, charm.pix, rope.end(), rope.end_angle(), scale, style)

    def _paint_hit_pads(self, painter: QPainter, scale: float) -> None:
        """Fill every click target with an invisible (alpha 1/255) wash.

        Windows (layered windows) hit-tests per pixel and lets clicks on fully transparent pixels
        fall through, regardless of the window mask. Without this, only the painted pixels of a
        charm and the 3 px rope line would be clickable there, while on X11 the whole mask is.
        The wash makes every OS behave like :meth:`charm_at`; on Windows, which therefore needs
        no mask at all (``uses_window_mask``), it alone is the input area. macOS normally decides input
        explicitly (``ignoresMouseEvents`` follows :meth:`track_pointer`); the wash only matters
        there as the fallback when its event monitors are unavailable, and Apple's undocumented
        alpha threshold may well be above 1/255. Skipped without a compositor (X11): the X server
        would show the wash as opaque black, and the shaped mask decides input there.
        """
        painter.save()
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)  # invisible anyway; faster
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(_HIT_PAD_COLOR)
            for charm in self.charms:
                rope = charm.rope
                painter.drawRect(_anchor_grab_rect(rope.anchor_x, self._anchor_depth))
                painter.setTransform(rendering.charm_transform(rope.end(), rope.end_angle()))
                painter.drawRect(rendering.charm_local_rect(charm.pix, scale))
                painter.resetTransform()
        finally:
            painter.restore()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        # A window manager / the OS may adjust our geometry after we set it: keep anchors at x_frac.
        self.manager._reanchor(self)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        pos = event.position()
        button = event.button()
        if button == Qt.MouseButton.LeftButton:
            if self.begin_drag(pos):
                event.accept()
            else:
                event.ignore()
        elif button == Qt.MouseButton.RightButton:
            charm, _mode = self.charm_at(pos)
            if charm is None:
                event.ignore()
                return
            event.accept()
            self.show_charm_menu(charm, event.globalPosition().toPoint())
        else:
            event.ignore()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        pos = event.position()
        if self._drag is not None:
            if not event.buttons() & Qt.MouseButton.LeftButton:
                # The release went elsewhere (focus steal, lost grab): finish the drag now so the
                # rope is not pinned forever and the animation can go idle again.
                self.end_drag()
                self.hover(pos, time.monotonic())
            else:
                self.drag_to(pos)
            event.accept()
            return
        if event.buttons() == Qt.MouseButton.NoButton:
            self.hover(pos, time.monotonic())
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._drag is not None:
            self.end_drag()
            _charm, mode = self.charm_at(event.position())
            self._update_cursor(mode)
            event.accept()
            return
        event.ignore()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            event.ignore()
            return
        charm, mode = self.charm_at(event.position())
        if charm is None or mode != _DRAG_CHARM:
            event.ignore()
            return
        event.accept()
        self.manager.nudge(charm)

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        # The menu is opened from mousePressEvent (same behaviour on every OS, including
        # macOS ctrl+click, which Qt reports as a right button press). Swallow the synthesized
        # context-menu event so the menu never opens twice.
        event.accept()

    def leaveEvent(self, event: QEvent) -> None:
        super().leaveEvent(event)
        self._hover_charm = None
        self._hover_sample = None
        if self._drag is None:
            self.unsetCursor()

    # ------------------------------------------------------------------ interaction (testable)

    def begin_drag(self, pos: QPointF) -> bool:
        """Start dragging the charm body or rope top under ``pos``; False if nothing is there."""
        charm, mode = self.charm_at(pos)
        if charm is None:
            return False
        self.cancel_drag()
        self._drag = (charm, mode)
        if mode == _DRAG_CHARM:
            ex, ey = charm.rope.end()
            # Keep the grabbed point under the cursor instead of snapping the rope end to it.
            self._grab_offset = (pos.x() - ex, pos.y() - ey)
            charm.rope.grab(ex, ey)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        else:
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        self.manager.wake()
        return True

    def drag_to(self, pos: QPointF) -> None:
        """Move the current drag (charm end or anchor) to follow ``pos``."""
        if self._drag is None:
            return
        charm, mode = self._drag
        if mode == _DRAG_CHARM:
            ox, oy = self._grab_offset
            charm.rope.move_grab(pos.x() - ox, pos.y() - oy)
        else:
            charm.rope.anchor_x = self.manager._clamp_anchor_x(pos.x(), self.width())
        self.manager.wake()

    def end_drag(self) -> None:
        """Finish the current drag: release the charm (keeps momentum) or persist the anchor."""
        if self._drag is None:
            return
        charm, mode = self._drag
        self._drag = None
        self.unsetCursor()
        if mode == _DRAG_CHARM:
            charm.rope.release()
            self.manager.wake()
        else:
            self.manager._commit_anchor(charm)

    def hover(self, pos: QPointF, now: float) -> str | None:
        """Handle a button-less cursor move: cursor feedback and hover swing.

        Returns what is under ``pos`` (like :meth:`charm_at`: ``"charm"``, ``"anchor"`` or None).
        """
        charm, mode = self.charm_at(pos)
        self._update_cursor(mode)
        body = charm if mode == _DRAG_CHARM else None
        if body is not None and body is not self._hover_charm and self.manager.settings.hover_swing:
            vx, vy = self._cursor_velocity(pos, now, body)
            speed = math.hypot(vx, vy)
            if speed > HOVER_MAX_SPEED:
                vx, vy = vx * HOVER_MAX_SPEED / speed, vy * HOVER_MAX_SPEED / speed
                speed = HOVER_MAX_SPEED
            if speed >= HOVER_MIN_SPEED:
                self.manager._push(body, vx * HOVER_TRANSFER, vy * HOVER_TRANSFER)
        self._hover_charm = body
        self._hover_sample = (pos.x(), pos.y(), now)
        return mode

    def _pointer_away(self) -> None:
        """:meth:`track_pointer`: the cursor is off every target; forget the hover state (once)."""
        if self._hover_charm is None and self._hover_sample is None:
            return
        self._hover_charm = None
        self._hover_sample = None
        if self._drag is None:
            self.unsetCursor()

    def track_pointer(self, global_pos: QPoint, now: float) -> bool:
        """Handle a cursor position reported outside Qt's mouse events; True = take clicks there.

        macOS feeds every cursor move through this from NSEvent monitors (``platform/macos.py``):
        Qt drops button-less moves while the app is inactive, which Latkan nearly always is, and
        the overlay there is made click-through explicitly instead of by the window mask. So
        this runs hover swing like a Qt mouse move and tells the caller whether the overlay must
        receive a click at ``global_pos``: during a drag, and over every :meth:`charm_at` target.
        """
        if self._drag is not None:
            return True
        region = self._last_region
        local = self.mapFromGlobal(global_pos)
        if (
            self._retired
            or region is None
            or not self.isVisible()
            or not self.rect().contains(local)
            or not region.boundingRect().contains(local)  # cheap pre-test: every target is inside
        ):
            self._pointer_away()
            return False
        return self.hover(QPointF(local), now) is not None

    def _cursor_velocity(self, pos: QPointF, now: float, charm: HangingCharm) -> tuple[float, float]:
        sample = self._hover_sample
        if sample is not None:
            sx, sy, st = sample
            age = now - st
            if 0.0 <= age <= HOVER_SAMPLE_MAX_AGE:
                dt = max(age, 0.004)  # event timestamps can coincide; avoid absurd speeds
                return (pos.x() - sx) / dt, (pos.y() - sy) / dt
        # No fresh sample (the cursor jumped straight into the charm from outside the mask):
        # assume it came from the side it entered on.
        end_x, _end_y = charm.rope.end()
        direction = 1.0 if pos.x() < end_x else -1.0
        return direction * HOVER_FALLBACK_SPEED, 0.0

    def show_charm_menu(self, charm: HangingCharm, global_pos: QPoint) -> None:
        """Pop up the per-charm context menu at ``global_pos`` (QPoint, global coordinates)."""
        self.cancel_drag()
        from latkan import menu as menu_module  # lazy: menu imports this module

        platform = self.manager.platform
        popup = menu_module.charm_menu(self.manager, charm, platform)
        self._menu_depth += 1  # dispose() now waits for us (see there)
        try:
            # macOS: clicking a charm no longer activates Latkan, and Qt ignores button-less mouse
            # moves in an inactive app, so the menu would get no hover highlight or keyboard input.
            with platform.foreground():
                popup.exec(global_pos)
        finally:
            self._menu_depth -= 1
            if self._dispose_pending and not self._menu_depth:
                # Posted from this (outer) event handler, so Qt runs it only after the handler
                # has returned, not in some nested loop below it.
                self.deleteLater()

    # ------------------------------------------------------------------ screen changes

    def _on_screen_geometry_changed(self, *_args: object) -> None:
        # availableGeometryChanged too: a panel appearing or resizing changes the anchor inset.
        depth_changed = self._update_anchor_depth()
        if self.apply_geometry():
            self.manager._reanchor(self)
            self.refresh()
        elif depth_changed:
            self.refresh()


# --------------------------------------------------------------------------- manager


class OverlayManager(QObject):
    """Owns settings, simulation and one overlay window per screen; drives the animation."""

    #: Emitted after every settings mutation (menus refresh their check marks).
    changed = Signal()

    def __init__(
        self, settings: Settings, platform: PlatformIntegration, parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.platform = platform
        self.sim = Simulation()
        self.windows: dict[str, OverlayWindow] = {}
        self._charms: list[HangingCharm] = []
        self._started = False
        self._breeze_time = 0.0
        self._rng = random.Random()

        self._clock = QElapsedTimer()
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(FRAME_INTERVAL_MS)
        self._timer.timeout.connect(self._on_frame)

        self._unsaved = False  # a change was made that is not on disk yet (see flush_save)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(SAVE_DEBOUNCE_MS)
        self._save_timer.timeout.connect(self.save_now)

        self._screen_timer = QTimer(self)
        self._screen_timer.setSingleShot(True)
        self._screen_timer.setInterval(SCREEN_SYNC_DELAY_MS)
        self._screen_timer.timeout.connect(self._sync_windows)

    # ------------------------------------------------------------------ queries

    @property
    def charms(self) -> list[HangingCharm]:
        """All hanging charms in z-order (a copy)."""
        return list(self._charms)

    @property
    def animating(self) -> bool:
        """Whether the animation timer is running."""
        return self._timer.isActive()

    def primary_window(self) -> OverlayWindow | None:
        """Window of the primary screen (falls back to any window; None without screens)."""
        primary = QGuiApplication.primaryScreen()
        live = [w for w in self.windows.values() if not w.retired]
        for window in live:
            if window.qscreen is primary:
                return window
        return live[0] if live else None

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Build the windows, hang the charms and start the animation (idempotent)."""
        if self._started:
            return
        app = QGuiApplication.instance()
        if app is None:
            raise RuntimeError("OverlayManager.start() requires a QApplication")
        self._started = True
        app.screenAdded.connect(self._on_screen_added)
        app.screenRemoved.connect(self._on_screen_removed)
        app.primaryScreenChanged.connect(self._schedule_screen_sync)
        try:
            self.platform.watch_compositing(self._check_compositing)
        except Exception:  # platform hook: changes are then noticed on show / wake only
            logger.warning("Could not watch for compositor changes", exc_info=True)
        self._sync_windows()
        logger.info("Overlay started: %d charm(s) on %d screen(s)", len(self._charms), len(self.windows))

    def shutdown(self) -> None:
        """Stop timers, flush a pending save and close all windows."""
        self.flush_save()
        self._timer.stop()
        self._screen_timer.stop()
        if self._started:
            app = QGuiApplication.instance()
            if app is not None:
                for signal, slot in (
                    (app.screenAdded, self._on_screen_added),
                    (app.screenRemoved, self._on_screen_removed),
                    (app.primaryScreenChanged, self._schedule_screen_sync),
                ):
                    try:
                        signal.disconnect(slot)
                    except (RuntimeError, TypeError):
                        logger.debug("Screen signal was not connected", exc_info=True)
            try:
                self.platform.watch_compositing(None)
            except Exception:  # platform hook
                logger.debug("Could not stop watching for compositor changes", exc_info=True)
            self._started = False
        for key in list(self.windows):
            self._dispose_window(key)
        for charm in self._charms:
            self.sim.remove(charm.rope)
        self._charms = []

    # ------------------------------------------------------------------ animation

    def wake(self) -> None:
        """(Re)start the animation loop; resets the physics clock when resuming from idle."""
        if not self._can_animate():
            self._timer.stop()
            return
        if self._timer.interval() != FRAME_INTERVAL_MS:
            self._timer.setInterval(FRAME_INTERVAL_MS)
        if not self._timer.isActive():
            # Resuming from idle: also the cheap moment to notice a compositor that started or
            # stopped while nothing moved (where the platform cannot report it by itself).
            self._check_compositing()
            self.sim.reset_clock()
            self._clock.restart()
            self._timer.start()

    def _can_animate(self) -> bool:
        return self._started and bool(self._charms) and bool(self.windows) and not self.settings.hidden

    def _check_compositing(self) -> None:
        """Ask the platform whether a compositor runs and switch the windows' mask mode to match."""
        windows = [w for w in self.windows.values() if not w.retired]
        if not windows:
            return
        try:
            active = bool(self.platform.compositing_active())
        except Exception:  # platform hook: keep the current mode
            logger.warning("Could not check whether a compositor runs", exc_info=True)
            return
        for window in windows:
            window.set_composited(active)

    def _on_frame(self) -> None:
        elapsed = self._clock.nsecsElapsed() / 1e9 if self._clock.isValid() else 0.0
        self._clock.restart()
        self.tick(elapsed)

    def tick(self, elapsed: float) -> int:
        """Advance the animation by ``elapsed`` seconds; returns the physics steps run.

        The timer calls this every frame; tests may call it directly with simulated time.
        Stops the timer (after a final exact repaint) once everything has come to rest.
        """
        if not self._can_animate():
            self._timer.stop()
            return 0
        elapsed = max(0.0, float(elapsed))
        # advance() runs at most max_substeps and drops the rest; drop it here already, because
        # the breeze impulse below scales with the frame time. After a stall (debugger, busy
        # system, VM pause, or sleep: QElapsedTimer counts through standby on Windows) the raw
        # time would hit every rope with one giant kick in a single step.
        params = self.sim.params
        elapsed = min(elapsed, params.max_substeps * params.dt)
        breeze = self.settings.breeze
        if breeze:
            self._apply_breeze(elapsed)
        steps = self.sim.advance(elapsed)
        if steps:
            for window in self.windows.values():
                window.animate_frame(keep_alive=breeze)

        interacting = self._interacting()
        if not breeze and not interacting and self.sim.settled():
            self._timer.stop()
            for window in self.windows.values():
                window.refresh()
                window._was_active = False
            logger.debug("All charms at rest; animation paused")
        else:
            interval = FRAME_INTERVAL_MS
            if breeze and not interacting:
                # Speed only, with hysteresis: a breeze sway keeps a long rope well off vertical
                # (and resonates near 500 px), so a "hanging still" test kept those at 60 fps for
                # good. Once slow, only a real push (> BREEZE_WAKE_SPEED) or wake() speeds it up.
                fastest = max((rope.speed() for rope in self.sim.ropes), default=0.0)
                slow = self._timer.interval() == BREEZE_INTERVAL_MS
                if fastest < (BREEZE_WAKE_SPEED if slow else BREEZE_IDLE_SPEED):
                    interval = BREEZE_INTERVAL_MS
            if self._timer.interval() != interval:
                self._timer.setInterval(interval)
        return steps

    def _apply_breeze(self, elapsed: float) -> None:
        self._breeze_time += elapsed
        t = self._breeze_time
        # apply_force() acts on the next fixed step only; scale it so the impulse delivered over
        # this frame equals force * elapsed regardless of how many substeps actually run.
        substeps = elapsed / self.sim.params.dt
        if substeps <= 0.0:
            return
        for charm in self._charms:
            charm.rope.apply_force(breeze_force(t, charm.phase) * substeps, 0.0)

    def _interacting(self) -> bool:
        return any(w.dragging for w in self.windows.values()) or any(r.is_grabbed for r in self.sim.ropes)

    # ------------------------------------------------------------------ screens / layout

    def _screen_map(self, exclude: QScreen | None = None) -> dict[str, QScreen]:
        """Connected screens by unique key (the screen name; synthesized when empty/duplicate)."""
        result: dict[str, QScreen] = {}
        for index, screen in enumerate(QGuiApplication.screens()):
            if exclude is not None and screen is exclude:
                continue
            base = screen.name() or f"screen-{index + 1}"
            key, n = base, 2
            while key in result:
                key = f"{base} #{n}"
                n += 1
            result[key] = screen
        return result

    def _on_screen_added(self, screen: QScreen) -> None:
        logger.info("Screen added: %r", screen.name())
        self._schedule_screen_sync()

    def _on_screen_removed(self, screen: QScreen) -> None:
        logger.info("Screen removed: %r", screen.name())
        # Hide right away: Qt is about to move the window to another screen, where its charms
        # would show at the wrong place until the (coalesced) sync rebuilds the layout.
        for window in self.windows.values():
            if window.qscreen is screen:
                window.retire()
        self._schedule_screen_sync()

    def _schedule_screen_sync(self, *_args: object) -> None:
        if self._started:
            self._screen_timer.start()

    def _sync_windows(self) -> None:
        """Create/dispose windows to match the connected screens, then re-layout the charms."""
        if not self._started:
            return
        screens = self._screen_map()
        for key, window in list(self.windows.items()):
            if window.retired or screens.get(key) is not window.qscreen:
                self._dispose_window(key)
        for key, screen in screens.items():
            if key not in self.windows:
                logger.debug("Creating overlay for screen %r", key)
                self.windows[key] = OverlayWindow(self, screen, key)
        self._layout()
        self.wake()

    def _dispose_window(self, key: str) -> None:
        self.windows.pop(key).dispose()

    def _window_for(self, spec: CharmSpec) -> OverlayWindow | None:
        if spec.screen is not None:
            window = self.windows.get(spec.screen)
            if window is not None and not window.retired:
                return window
        # Not connected (or never set): hang on the primary screen but keep spec.screen, so the
        # charm returns to its own monitor when that is plugged in again.
        return self.primary_window()

    def _layout(self) -> None:
        """Make the hanging charms match ``settings.charms`` and the windows; refresh windows."""
        existing = {id(c.spec): c for c in self._charms}
        result: list[HangingCharm] = []
        for spec in self.settings.charms:
            target = self._window_for(spec)
            if target is None:  # no usable screen at all: hang again once one appears
                continue
            charm = existing.pop(id(spec), None)
            if charm is None:
                charm = self._hang(spec, target)
            elif charm.window is not target:
                self._rehang(charm, target)
            result.append(charm)
        for orphan in existing.values():  # specs removed from settings behind our back
            orphan.window.cancel_drag(orphan)
            self.sim.remove(orphan.rope)
        self._charms = result
        for window in self.windows.values():
            window.set_charms([c for c in result if c.window is window])

    def _hang(self, spec: CharmSpec, window: OverlayWindow) -> HangingCharm:
        length = self._rope_length(spec.length, window)
        rope = Rope(self._anchor_x(spec, window), 0.0, length, self.sim.params, self._swing_offset(length))
        self.sim.add(rope)
        return HangingCharm(spec, rope, charms.charm_pixmap(spec), window, _breeze_phase(spec.id))

    def _rehang(self, charm: HangingCharm, window: OverlayWindow) -> None:
        """Move ``charm`` to another window with a fresh rope that swings in."""
        charm.window.cancel_drag(charm)
        length = self._rope_length(charm.spec.length, window)
        x = self._anchor_x(charm.spec, window)
        rope = Rope(x, 0.0, length, self.sim.params, self._swing_offset(length))
        self.sim.remove(charm.rope)
        self.sim.add(rope)
        charm.rope = rope
        charm.window = window

    def _reanchor(self, window: OverlayWindow) -> None:
        """Keep anchors at ``x_frac * width`` (and ropes within the height) after a resize."""
        if window.width() <= 0 or window.height() <= 0:
            return
        moved = False
        for charm in window.charms:
            rope = charm.rope
            length = self._rope_length(charm.spec.length, window)
            if abs(rope.length - length) > 0.5:
                rope.length = length
                moved = True
            if window.is_dragging_anchor(charm):
                continue  # the user is placing this anchor right now
            x = self._anchor_x(charm.spec, window)
            if abs(rope.anchor_x - x) > 0.5 or rope.anchor_y != 0.0:
                rope.relocate(x, 0.0)  # translate the whole rope: no violent yank
                moved = True
        if moved:
            self.wake()

    @staticmethod
    def _clamp_anchor_x(x: float, width: float) -> float:
        if width <= 2 * ANCHOR_EDGE_MARGIN:
            return width / 2.0
        return _clamp(x, ANCHOR_EDGE_MARGIN, width - ANCHOR_EDGE_MARGIN)

    def _anchor_x(self, spec: CharmSpec, window: OverlayWindow) -> float:
        return self._clamp_anchor_x(spec.x_frac * window.width(), window.width())

    @staticmethod
    def _max_rope_length(window: OverlayWindow | None) -> float:
        if window is None or window.height() <= 0:
            return float("inf")
        return max(MIN_ROPE_LENGTH, MAX_ROPE_FRACTION * window.height())

    def _rope_length(self, length: float, window: OverlayWindow | None) -> float:
        """Runtime rope length: the persisted length limited to what fits on this screen."""
        return _clamp(float(length), MIN_ROPE_LENGTH, self._max_rope_length(window))

    def _swing_offset(self, length: float) -> float:
        return self._rng.choice((-1.0, 1.0)) * self._rng.uniform(SWING_IN_MIN, SWING_IN_MAX) * length

    def _placement(self, window: OverlayWindow | None) -> tuple[float, float]:
        """``(x_frac, length)`` for a new charm, keeping clear of existing anchors on ``window``."""
        length = self._rope_length(self._rng.uniform(NEW_ROPE_MIN, NEW_ROPE_MAX), window)
        if window is None or window.width() <= 0:
            return self._rng.uniform(NEW_X_MIN_FRAC, NEW_X_MAX_FRAC), length
        width = float(window.width())
        spacing = rendering.CHARM_BASE_PX * self.settings.scale / width
        taken = [c.rope.anchor_x / width for c in window.charms]
        best, best_gap = 0.5, -1.0
        for _ in range(64):
            candidate = self._rng.uniform(NEW_X_MIN_FRAC, NEW_X_MAX_FRAC)
            gap = min((abs(candidate - t) for t in taken), default=1.0)
            if gap >= spacing:
                return candidate, length
            if gap > best_gap:
                best, best_gap = candidate, gap
        return best, length  # screen is crowded: take the roomiest spot found

    # ------------------------------------------------------------------ interaction helpers

    def nudge(self, charm: HangingCharm, speed: float = NUDGE_SPEED) -> None:
        """Give one charm a sideways push in a random direction."""
        charm.rope.nudge(self._rng.choice((-1.0, 1.0)) * speed * self._rng.uniform(0.85, 1.1))
        self.wake()

    def _push(self, charm: HangingCharm, vx: float, vy: float) -> None:
        charm.rope.nudge(vx, vy)
        self.wake()

    def _commit_anchor(self, charm: HangingCharm) -> None:
        """Persist a dragged anchor position as ``x_frac``."""
        width = charm.window.width()
        if width > 0:
            charm.spec.x_frac = _clamp(charm.rope.anchor_x / width, 0.0, 1.0)
        self._mutated()

    def _mutated(self) -> None:
        """Common tail of every settings mutation: repaint, animate, persist, notify."""
        self._refresh_windows()
        self.wake()
        self.save()
        self.changed.emit()

    def _find(self, charm: HangingCharm) -> bool:
        return any(c is charm for c in self._charms)

    # ------------------------------------------------------------------ public mutators

    def add_builtin(self, name: str) -> HangingCharm | None:
        """Hang a new built-in charm on the primary screen; returns it (None without screens)."""
        if name not in BUILTIN_NAMES:
            raise ValueError(f"Unknown built-in charm {name!r}")
        window = self.primary_window()
        x_frac, length = self._placement(window)
        spec = CharmSpec.new_builtin(name, x_frac, length, screen=window.screen_name if window else None)
        return self._add_spec(spec)

    def add_image_from_dialog(self, parent: QWidget | None = None) -> HangingCharm | None:
        """Ask for an image file, import it and hang it as a charm. None if cancelled/failed."""
        start_dir = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.PicturesLocation)
        if not start_dir or not Path(start_dir).is_dir():
            start_dir = str(Path.home())
        filters = f"Images ({_image_patterns()});;All files (*)"
        title = "Choose an image for your charm"
        # foreground(): a macOS accessory app is never active, so bring its dialogs to the front
        # (and mark the activation as Latkan's own, see PlatformIntegration.foreground).
        with self.platform.foreground():
            path, _selected = QFileDialog.getOpenFileName(parent, title, start_dir, filters)
            if not path:
                return None
            try:
                image = charms.import_image(Path(path))
            except (ValueError, OSError) as exc:
                logger.warning("Could not import charm image %s: %s", path, exc)
                from latkan import menu as menu_module  # lazy, like show_charm_menu()

                menu_module.message_box(self.platform, "warning", APP_NAME, str(exc), parent=parent)
                return None
        window = self.primary_window()
        x_frac, length = self._placement(window)
        spec = CharmSpec.new_image(image, x_frac, length, screen=window.screen_name if window else None)
        return self._add_spec(spec)

    def _add_spec(self, spec: CharmSpec) -> HangingCharm | None:
        self.settings.charms.append(spec)
        self._layout()
        self._mutated()
        logger.info("Added charm %s (%s)", spec.id, spec.name or spec.image)
        return next((c for c in self._charms if c.spec is spec), None)

    def remove(self, charm: HangingCharm) -> None:
        """Remove a charm (and its imported image file when no other charm uses it)."""
        if not self._find(charm):
            logger.debug("remove(): charm %s is not hanging", charm.spec.id)
            return
        charm.window.cancel_drag(charm)
        self.sim.remove(charm.rope)
        self._charms = [c for c in self._charms if c is not charm]
        self.settings.charms[:] = [s for s in self.settings.charms if s is not charm.spec]
        self._release_resources([charm.spec])
        self._layout()
        self._mutated()
        logger.info("Removed charm %s", charm.spec.id)

    def clear_all(self) -> None:
        """Remove every charm."""
        specs = list(self.settings.charms)
        for window in self.windows.values():
            window.cancel_drag()
        for charm in self._charms:
            self.sim.remove(charm.rope)
        self._charms = []
        self.settings.charms.clear()
        self._release_resources(specs)
        self._layout()
        self._mutated()
        logger.info("Removed all charms (%d)", len(specs))

    def _release_resources(self, removed: list[CharmSpec]) -> None:
        """Drop cached pixmaps and delete image files no remaining charm refers to."""
        in_use = {s.image for s in self.settings.charms if s.kind == "image" and s.image}
        for spec in removed:
            charms.forget(spec.id)
            if spec.kind == "image" and spec.image and spec.image not in in_use:
                charms.delete_image(spec.image)
                in_use.add(spec.image)  # never try to delete the same file twice

    def set_rope_style(self, style: str) -> None:
        if style not in ROPE_STYLES:
            raise ValueError(f"Unknown rope style {style!r}")
        if style != self.settings.rope_style:
            self.settings.rope_style = style
            self._mutated()

    def set_scale(self, scale: float) -> None:
        value = _clamp(float(scale), 0.3, 3.0)
        if not math.isfinite(value):
            raise ValueError(f"Invalid charm scale {scale!r}")
        if value != self.settings.scale:
            self.settings.scale = value
            self._mutated()

    def set_breeze(self, enabled: bool) -> None:
        if bool(enabled) != self.settings.breeze:
            self.settings.breeze = bool(enabled)
            self._mutated()

    def set_hover_swing(self, enabled: bool) -> None:
        if bool(enabled) != self.settings.hover_swing:
            self.settings.hover_swing = bool(enabled)
            self._mutated()

    def set_hidden(self, hidden: bool) -> None:
        if bool(hidden) == self.settings.hidden:
            return
        self.settings.hidden = bool(hidden)
        self._mutated()  # refresh hides/shows the windows; wake() stops the loop while hidden

    def nudge_all(self) -> None:
        """Push every charm sideways (random directions)."""
        for charm in self._charms:
            charm.rope.nudge(self._rng.choice((-1.0, 1.0)) * NUDGE_SPEED * self._rng.uniform(0.75, 1.1))
        self.wake()

    def change_length(self, charm: HangingCharm, factor: float) -> None:
        """Multiply a charm's rope length by ``factor`` (clamped to 40 .. 85 % of the screen)."""
        if not (math.isfinite(factor) and factor > 0.0):
            raise ValueError(f"Invalid length factor {factor!r}")
        if not self._find(charm):
            return
        new = _clamp(charm.rope.length * factor, MIN_ROPE_LENGTH, self._max_rope_length(charm.window))
        charm.spec.length = new
        charm.rope.length = new
        charm.rope.nudge(self._rng.choice((-1.0, 1.0)) * LENGTH_CHANGE_NUDGE)
        self._mutated()

    def move_to_screen(self, charm: HangingCharm, screen_name: str) -> None:
        """Hang ``charm`` on the screen ``screen_name`` (a key of :attr:`windows`)."""
        if screen_name not in self.windows:
            logger.warning("move_to_screen(): no screen named %r", screen_name)
            return
        if not self._find(charm) or charm.window is self.windows[screen_name]:
            return
        charm.spec.screen = screen_name
        self._layout()
        self._mutated()

    def _refresh_windows(self) -> None:
        for window in self.windows.values():
            window.refresh()

    # ------------------------------------------------------------------ persistence

    def save(self) -> None:
        """Schedule a settings write (debounced, 400 ms)."""
        self._unsaved = True
        self._save_timer.start()

    def save_now(self) -> None:
        """Write the settings immediately (cancels a pending debounced save)."""
        self._save_timer.stop()
        try:
            save_settings(self.settings)
        except OSError:
            logger.exception("Could not save settings")
            return
        self._unsaved = False

    def flush_save(self) -> None:
        """Write the settings now if a change is not saved yet (pending, or its save failed).

        Used on quit: a session that changed nothing never rewrites the settings file.
        """
        if self._unsaved:
            self.save_now()


def _image_patterns() -> str:
    """File dialog glob patterns for every image format this Qt build can read."""
    supported = QImageReader.supportedImageFormats()
    formats = {bytes(fmt.data()).decode("ascii", "ignore").lower() for fmt in supported}
    patterns = [f"*.{fmt}" for fmt in sorted(formats) if fmt.isalnum()]
    return " ".join(patterns) or "*.png *.jpg *.jpeg *.bmp"
