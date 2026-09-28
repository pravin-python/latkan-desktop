"""Rope styles, charm body drawing and the hit/mask geometry shared by the overlay.

Coordinates are the overlay window's logical pixels. A rope is a sequence of
``(x, y)`` nodes (``Rope.points``); the charm hangs from the last node, rotated with
the rope (see :func:`charm_transform`). In charm-local coordinates the rope end is the
origin, +y points away from the rope, the ring occupies ``0..HOOK_PX`` and the charm
image sits in ``QRectF(-w/2, HOOK_PX, w, h)``.
"""

from __future__ import annotations

import logging
import math
from collections import OrderedDict
from collections.abc import Sequence

from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap, QTransform

logger = logging.getLogger(__name__)

__all__ = [
    "CHARM_BASE_PX",
    "HOOK_PX",
    "charm_bounds",
    "charm_hit",
    "charm_local_rect",
    "charm_size",
    "charm_transform",
    "draw_charm",
    "draw_rope",
    "rope_bounds",
    "rope_path",
]

CHARM_BASE_PX = 96  # charm width at scale 1.0 (logical px)
HOOK_PX = 7  # distance from rope end to top of charm image (a small ring is drawn there)

Point = tuple[float, float]
PointLike = Point | QPointF

_RING_RADIUS = 3.0
_RING_CENTER_Y = 3.2
_CHAIN_STEP = 6.0  # px between chain links along the rope

_warned_styles: set[str] = set()

# Charm images pre-scaled to their on-screen device size: drawing a large pixmap straight
# into a small rect with bilinear filtering aliases; a smooth pre-scale looks much better
# and is cheaper per frame. Keyed by (pixmap cacheKey, device width, device height).
_SCALED_CACHE_MAX = 48
_scaled_cache: OrderedDict[tuple[int, int, int], QPixmap] = OrderedDict()


# --------------------------------------------------------------------------- helpers


def _xy(pt: PointLike) -> tuple[float, float]:
    if isinstance(pt, QPointF):
        return pt.x(), pt.y()
    return float(pt[0]), float(pt[1])


def _qp(pt: PointLike) -> QPointF:
    x, y = _xy(pt)
    return QPointF(x, y)


def _pen(color: QColor, width: float, style: Qt.PenStyle = Qt.PenStyle.SolidLine) -> QPen:
    pen = QPen(color, width, style)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def _logical_size(pix: QPixmap) -> tuple[float, float]:
    """Device-independent size of ``pix`` (accounts for devicePixelRatio != 1)."""
    size = pix.deviceIndependentSize()
    return size.width(), size.height()


# --------------------------------------------------------------------------- rope


def rope_path(points: Sequence[PointLike]) -> QPainterPath:
    """Smooth path through the rope: quadratic segments using the nodes as control points
    and the midpoints between nodes as on-curve points; starts at the first node and
    ends exactly at the last node."""
    pts = [_qp(pt) for pt in points]
    if not pts:
        return QPainterPath()
    path = QPainterPath(pts[0])
    if len(pts) == 1:
        return path
    for i in range(1, len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        path.quadTo(a, QPointF((a.x() + b.x()) / 2, (a.y() + b.y()) / 2))
    path.lineTo(pts[-1])
    return path


def _draw_gold(p: QPainter, path: QPainterPath) -> None:
    p.setPen(_pen(QColor(92, 64, 10, 170), 3.4))
    p.drawPath(path)
    p.setPen(_pen(QColor("#e3bd52"), 1.9))
    p.drawPath(path)
    p.setPen(_pen(QColor(255, 244, 196, 150), 0.6))
    p.drawPath(path)


def _draw_kalava(p: QPainter, path: QPainterPath) -> None:
    p.setPen(_pen(QColor(70, 5, 10, 150), 3.4))
    p.drawPath(path)
    p.setPen(_pen(QColor("#c0141f"), 2.5))
    p.drawPath(path)
    twist = QPen(QColor("#f2c14e"), 0.9)
    twist.setCapStyle(Qt.PenCapStyle.RoundCap)
    twist.setDashPattern([3.0, 3.6])  # in pen widths
    p.setPen(twist)
    p.drawPath(path)


def _draw_neon(p: QPainter, path: QPainterPath) -> None:
    for width, alpha in ((12.0, 28), (7.5, 55), (4.0, 120)):
        p.setPen(_pen(QColor(255, 56, 200, alpha), width))
        p.drawPath(path)
    p.setPen(_pen(QColor(255, 228, 250), 1.4))
    p.drawPath(path)


def _chain_path(path: QPainterPath) -> QPainterPath:
    """Chain links along ``path``: alternating open ellipses and edge-on bars."""
    link = QPainterPath()
    link.addEllipse(QRectF(-4.0, -2.3, 8.0, 4.6))
    bar = QPainterPath(QPointF(-3.6, 0.0))
    bar.lineTo(QPointF(3.6, 0.0))
    chain = QPainterPath()
    length = path.length()
    if length <= 0.0:
        return chain
    t, n = 0.0, 0
    while t <= length:
        pct = path.percentAtLength(t)
        pt = path.pointAtPercent(pct)
        angle = path.angleAtPercent(pct)  # degrees, counter-clockwise, y up
        xf = QTransform().translate(pt.x(), pt.y()).rotate(-angle)
        chain.addPath(xf.map(link if n % 2 == 0 else bar))
        t += _CHAIN_STEP
        n += 1
    return chain


def _draw_silver(p: QPainter, path: QPainterPath) -> None:
    chain = _chain_path(path)
    p.setPen(_pen(QColor(62, 68, 76, 200), 2.3))
    p.drawPath(chain)
    p.setPen(_pen(QColor("#c9d0d8"), 1.2))
    p.drawPath(chain)
    p.setPen(_pen(QColor(255, 255, 255, 150), 0.45))
    p.drawPath(chain)


_ROPE_STYLES = {
    "gold": _draw_gold,
    "silver": _draw_silver,
    "neon": _draw_neon,
    "kalava": _draw_kalava,
}


def draw_rope(p: QPainter, points: Sequence[PointLike], style: str) -> None:
    """Draw a rope in ``style`` (gold | silver | neon | kalava; unknown -> gold)."""
    if len(points) < 2:
        return
    draw = _ROPE_STYLES.get(style)
    if draw is None:
        if style not in _warned_styles:
            _warned_styles.add(style)
            logger.warning("Unknown rope style %r, drawing gold", style)
        draw = _draw_gold
    path = rope_path(points)
    p.save()
    try:
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setBrush(Qt.BrushStyle.NoBrush)
        draw(p, path)
    finally:
        p.restore()


# --------------------------------------------------------------------------- charm


def charm_size(pix: QPixmap, scale: float) -> tuple[float, float]:
    """On-screen charm size: width ``CHARM_BASE_PX * scale``, height keeps the aspect ratio."""
    w = CHARM_BASE_PX * float(scale)
    pw, ph = _logical_size(pix)
    if pw <= 0.0 or ph <= 0.0:
        return w, w
    return w, w * ph / pw


def charm_transform(end: PointLike, angle: float) -> QTransform:
    """Charm-local -> window transform: translate to the rope end, then rotate by -angle.

    ``angle`` follows ``Rope.end_angle()``: 0 hangs straight down, positive when the end
    is to +x of the node above, so local +y points along the rope's last segment.
    """
    x, y = _xy(end)
    t = QTransform()
    t.translate(x, y)
    t.rotate(-math.degrees(angle))
    return t


def charm_local_rect(pix: QPixmap, scale: float) -> QRectF:
    """Ring + image area in charm-local coordinates: ``QRectF(-w/2, 0, w, h + HOOK_PX)``."""
    w, h = charm_size(pix, scale)
    return QRectF(-w / 2, 0.0, w, h + HOOK_PX)


def _painter_scale(p: QPainter) -> float:
    """Device pixels per logical unit at the painter's current state."""
    try:
        dpr = float(p.device().devicePixelRatioF())
    except (AttributeError, RuntimeError):
        dpr = 1.0
    det = abs(p.worldTransform().determinant())
    world = math.sqrt(det) if det > 0.0 else 1.0
    return max(dpr, 0.01) * world


def _scaled_for_device(pix: QPixmap, dev_w: int, dev_h: int) -> QPixmap:
    """``pix`` smoothly pre-scaled to ``dev_w x dev_h`` device pixels (cached, LRU)."""
    if dev_w <= 0 or dev_h <= 0:
        return pix
    # Only worth it when noticeably downscaling; upscaling/near 1:1 is fine as is.
    if pix.width() <= dev_w * 1.25 and pix.height() <= dev_h * 1.25:
        return pix
    key = (pix.cacheKey(), dev_w, dev_h)
    cached = _scaled_cache.get(key)
    if cached is not None:
        _scaled_cache.move_to_end(key)
        return cached
    scaled = pix.scaled(
        dev_w, dev_h, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation
    )
    if scaled.isNull():
        return pix
    _scaled_cache[key] = scaled
    while len(_scaled_cache) > _SCALED_CACHE_MAX:
        _scaled_cache.popitem(last=False)
    return scaled


def _draw_ring(p: QPainter, style: str) -> None:
    if style == "silver":
        dark, mid, light = QColor(70, 76, 84, 220), QColor("#c3cad2"), QColor(255, 255, 255, 190)
    else:
        dark, mid, light = QColor(96, 68, 10, 220), QColor("#dcb343"), QColor(255, 246, 205, 200)
    centre = QPointF(0.0, _RING_CENTER_Y)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(_pen(dark, 2.5))
    p.drawEllipse(centre, _RING_RADIUS, _RING_RADIUS)
    p.setPen(_pen(mid, 1.4))
    p.drawEllipse(centre, _RING_RADIUS, _RING_RADIUS)
    p.setPen(_pen(light, 0.6))
    r = _RING_RADIUS
    p.drawArc(QRectF(-r, _RING_CENTER_Y - r, 2 * r, 2 * r), 100 * 16, 70 * 16)


def draw_charm(p: QPainter, pix: QPixmap, end: PointLike, angle: float, scale: float, style: str) -> None:
    """Draw the ring at the rope end and the charm image below it, rotated with the rope."""
    w, h = charm_size(pix, scale)
    p.save()
    try:
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        device_scale = _painter_scale(p)
        p.setTransform(charm_transform(end, angle), True)
        _draw_ring(p, style)
        if not pix.isNull():
            src = _scaled_for_device(pix, round(w * device_scale), round(h * device_scale))
            p.drawPixmap(
                QRectF(-w / 2, float(HOOK_PX), w, h),
                src,
                QRectF(0.0, 0.0, float(src.width()), float(src.height())),
            )
    finally:
        p.restore()


def charm_hit(pos: QPointF, pix: QPixmap, end: PointLike, angle: float, scale: float) -> bool:
    """True when window position ``pos`` is on the charm (ring + image rect)."""
    inverse, ok = charm_transform(end, angle).inverted()
    if not ok:
        return False
    return charm_local_rect(pix, scale).contains(inverse.map(QPointF(pos)))


# --------------------------------------------------------------------------- mask geometry


def _padded(xs: Sequence[float], ys: Sequence[float], pad: float) -> QRect:
    left, right = min(xs), max(xs)
    top, bottom = min(ys), max(ys)
    return QRectF(left - pad, top - pad, right - left + 2 * pad, bottom - top + 2 * pad).toAlignedRect()


def rope_bounds(points: Sequence[PointLike], pad: float) -> list[QRect]:
    """One padded rect per rope segment whose union covers the drawn rope.

    Each rect bounds the control polygon of one piece of :func:`rope_path` (a quadratic
    lies inside its control triangle), so the union is guaranteed to contain the curve
    even where the rope bends sharply.
    """
    pts = [_xy(pt) for pt in points]
    n = len(pts)
    if n < 2:
        return []
    if n == 2:
        (x0, y0), (x1, y1) = pts
        return [_padded((x0, x1), (y0, y1), pad)]
    mids = [((pts[i][0] + pts[i + 1][0]) / 2, (pts[i][1] + pts[i + 1][1]) / 2) for i in range(n - 1)]
    rects: list[QRect] = []
    for i in range(1, n - 1):  # quadratic piece with control point pts[i]
        start = pts[0] if i == 1 else mids[i - 1]
        ctrl, stop = pts[i], mids[i]
        rects.append(_padded((start[0], ctrl[0], stop[0]), (start[1], ctrl[1], stop[1]), pad))
    last_mid, last = mids[-1], pts[-1]  # final straight piece to the end node
    rects.append(_padded((last_mid[0], last[0]), (last_mid[1], last[1]), pad))
    return rects


def charm_bounds(pix: QPixmap, end: PointLike, angle: float, scale: float, pad: float) -> QRect:
    """Axis-aligned bounding rect of the rotated charm (ring + image), padded."""
    rect = charm_transform(end, angle).mapRect(charm_local_rect(pix, scale))
    return rect.adjusted(-pad, -pad, pad, pad).toAlignedRect()
