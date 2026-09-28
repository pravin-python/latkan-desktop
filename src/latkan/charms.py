"""Charm artwork: built-in vector charms, the pixmap cache and custom image import.

All built-in artwork is original vector drawing done with QPainter (no third-party
assets). Every charm is drawn in a ``size x size`` box on a transparent background,
hung from the top centre of that box: the rope's ring (see :mod:`latkan.rendering`)
sits just above ``(size / 2, 0)``.

Drawing functions work in a 100 x 100 unit design space; :func:`builtin_pixmap`
scales the painter so the same code produces any pixel size.
"""

from __future__ import annotations

import logging
import math
import os
import uuid
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QIcon,
    QImage,
    QImageReader,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
    QRadialGradient,
    QTransform,
)

from latkan import config
from latkan.config import BUILTIN_NAMES, CharmSpec

logger = logging.getLogger(__name__)

__all__ = [
    "BUILTIN_LABELS",
    "app_icon",
    "builtin_pixmap",
    "charm_pixmap",
    "delete_image",
    "forget",
    "import_image",
    "tray_icon",
]

#: Human readable labels for the built-in charms (keys == ``config.BUILTIN_NAMES``, same order).
_LABELS: dict[str, str] = {
    "nazar": "Nazar (evil eye)",
    "heart": "Heart",
    "star": "Lucky star",
    "moon": "Crescent moon",
    "clover": "Four-leaf clover",
    "dreamcatcher": "Dream catcher",
}
BUILTIN_LABELS: dict[str, str] = {name: _LABELS.get(name, name.title()) for name in BUILTIN_NAMES}

#: Import limits (module level so tests can tighten them).
MAX_FILE_BYTES = 40 * 1024 * 1024
MAX_IMAGE_DIM = 8000
MAX_CHARM_EDGE = 512

#: Formats without an intrinsic pixel size; rendered at MAX_CHARM_EDGE on import.
_VECTOR_FORMATS = frozenset({b"svg", b"svgz", b"pdf"})

#: Pixel size built-ins are rendered at for hanging charms (covers 160 % scale on 2x displays).
CHARM_PIXMAP_PX = 384

#: Sizes packed into the application icon.
ICON_SIZES = (16, 32, 48, 64, 128, 256)

_UNIT = 100.0  # design space of the drawing functions

_builtin_cache: dict[tuple[str, int], QPixmap] = {}
_spec_cache: dict[str, QPixmap] = {}


# --------------------------------------------------------------------------- drawing helpers


def _pen(color: QColor | str, width: float) -> QPen:
    pen = QPen(QColor(color), width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def _radial(
    cx: float, cy: float, radius: float, fx: float, fy: float, stops: list[tuple[float, str | QColor]]
) -> QRadialGradient:
    grad = QRadialGradient(QPointF(cx, cy), radius, QPointF(fx, fy))
    for pos, color in stops:
        grad.setColorAt(pos, QColor(color))
    return grad


def _gloss(
    p: QPainter, rect: QRectF, angle: float, alpha: int = 150, clip: QPainterPath | None = None
) -> None:
    """Soft elliptical glossy highlight (white fading to transparent), optionally clipped."""
    p.save()
    if clip is not None:
        p.setClipPath(clip, Qt.ClipOperation.IntersectClip)
    centre = rect.center()
    p.translate(centre)
    p.rotate(angle)
    local = QRectF(-rect.width() / 2, -rect.height() / 2, rect.width(), rect.height())
    grad = QRadialGradient(QPointF(0, -local.height() * 0.15), max(local.width(), local.height()) / 2)
    grad.setColorAt(0.0, QColor(255, 255, 255, alpha))
    grad.setColorAt(0.6, QColor(255, 255, 255, alpha // 3))
    grad.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(grad)
    p.drawEllipse(local)
    p.restore()


def _sparkle(p: QPainter, x: float, y: float, r: float, alpha: int = 230) -> None:
    """Tiny specular dot."""
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(255, 255, 255, alpha))
    p.drawEllipse(QPointF(x, y), r, r)


def _bail(p: QPainter, attach_y: float) -> None:
    """Short gold wire from the top centre down to the artwork plus a bead at the joint.

    Used by charms whose silhouette does not reach the top centre of the box, so the
    rope's ring visibly connects to the body.
    """
    top = QPointF(50.0, 0.8)
    bottom = QPointF(50.0, attach_y)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(_pen("#7a5a12", 3.0))
    p.drawLine(top, bottom)
    p.setPen(_pen("#e8c65e", 1.6))
    p.drawLine(top, bottom)
    p.setPen(_pen("#7a5a12", 0.9))
    p.setBrush(
        _radial(50, attach_y, 3.6, 49, attach_y - 1.2, [(0.0, "#fff1b8"), (0.5, "#e8c65e"), (1.0, "#a67c1a")])
    )
    p.drawEllipse(QPointF(50.0, attach_y), 3.4, 3.4)


# --------------------------------------------------------------------------- built-in artwork


def _draw_nazar(p: QPainter) -> None:
    """Glass evil-eye bead: dark blue, white, light blue, black rings with a glassy sheen."""
    c = QPointF(50, 50)
    # outer cobalt disc
    p.setPen(_pen("#071b52", 1.6))
    p.setBrush(_radial(50, 50, 48, 36, 32, [(0.0, "#4a7fee"), (0.55, "#1847b4"), (1.0, "#0a2a78")]))
    p.drawEllipse(c, 46.5, 46.5)
    # white ring
    p.setPen(_pen("#b7c3da", 0.7))
    p.setBrush(_radial(50, 50, 36, 42, 40, [(0.0, "#ffffff"), (0.8, "#f1f4fa"), (1.0, "#d5deee")]))
    p.drawEllipse(c, 33.5, 33.5)
    # light blue iris
    p.setPen(_pen("#0f5ea8", 0.7))
    p.setBrush(_radial(50, 50, 23, 44, 43, [(0.0, "#a5dcff"), (0.55, "#39a6f5"), (1.0, "#1670c4")]))
    p.drawEllipse(c, 21.5, 21.5)
    # black pupil
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(_radial(50, 50, 11, 47, 46, [(0.0, "#3b3f47"), (1.0, "#040506")]))
    p.drawEllipse(c, 10.5, 10.5)
    # glass sheen across the upper-left and specular dots
    disc = QPainterPath()
    disc.addEllipse(c, 45.5, 45.5)
    _gloss(p, QRectF(18, 10, 46, 28), -28, alpha=120, clip=disc)
    _sparkle(p, 45.0, 45.0, 3.0, 235)
    _sparkle(p, 30.0, 27.0, 1.8, 170)


def _heart_path() -> QPainterPath:
    path = QPainterPath(QPointF(50, 21))
    path.cubicTo(48, 9, 32, 2, 19, 5)
    path.cubicTo(6, 8, 0, 18, 2, 33)
    path.cubicTo(4, 51, 30, 71, 50, 95)
    path.cubicTo(70, 71, 96, 51, 98, 33)
    path.cubicTo(100, 18, 94, 8, 81, 5)
    path.cubicTo(68, 2, 52, 9, 50, 21)
    path.closeSubpath()
    return path


def _draw_heart(p: QPainter) -> None:
    """Glossy red heart hung from its dip."""
    path = _heart_path()
    p.setPen(_pen("#6e0514", 1.8))
    p.setBrush(_radial(42, 42, 62, 32, 28, [(0.0, "#ff9aa9"), (0.45, "#f0385a"), (1.0, "#9e0a24")]))
    p.drawPath(path)
    # inner rim light along the lower right edge
    p.save()
    p.setClipPath(path)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(_pen(QColor(255, 190, 200, 70), 3.0))
    p.drawPath(QTransform().translate(50, 52).scale(0.9, 0.9).translate(-50, -52).map(path))
    p.restore()
    _gloss(p, QRectF(12, 12, 30, 17), -32, alpha=170, clip=path)
    _sparkle(p, 22.0, 20.0, 2.2, 220)
    _bail(p, 19.0)


def _draw_star(p: QPainter) -> None:
    """Bevelled golden five-point star."""
    cx, cy, outer, inner = 50.0, 51.5, 48.0, 20.0
    tips: list[QPointF] = []
    valleys: list[QPointF] = []
    for i in range(5):
        a = math.radians(-90 + i * 72)
        b = math.radians(-90 + i * 72 + 36)
        tips.append(QPointF(cx + outer * math.cos(a), cy + outer * math.sin(a)))
        valleys.append(QPointF(cx + inner * math.cos(b), cy + inner * math.sin(b)))
    outline = QPolygonF()
    for tip, valley in zip(tips, valleys):
        outline.append(tip)
        outline.append(valley)
    centre = QPointF(cx, cy)

    p.setPen(_pen("#8f6400", 1.8))
    p.setBrush(_radial(46, 40, 56, 42, 30, [(0.0, "#fff7c4"), (0.45, "#ffd23a"), (1.0, "#d08e00")]))
    p.drawPolygon(outline)
    # bevel: every point has a lit half and a shaded half
    p.setPen(Qt.PenStyle.NoPen)
    for i, tip in enumerate(tips):
        right = valleys[i]
        left = valleys[i - 1]
        p.setBrush(QColor(255, 255, 255, 70))
        p.drawPolygon(QPolygonF([centre, left, tip]))
        p.setBrush(QColor(120, 70, 0, 45))
        p.drawPolygon(QPolygonF([centre, tip, right]))
    # crisp outline on top of the facets
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(_pen("#8f6400", 1.8))
    p.drawPolygon(outline)
    star = QPainterPath()
    star.addPolygon(outline)
    star.closeSubpath()
    _gloss(p, QRectF(32, 24, 22, 13), -20, alpha=150, clip=star)
    _sparkle(p, 45.5, 30.0, 1.9, 230)


def _moon_path() -> QPainterPath:
    disc = QPainterPath()
    disc.addEllipse(QPointF(50, 52), 44, 44)
    bite = QPainterPath()
    bite.addEllipse(QPointF(75, 42), 38, 38)
    return disc.subtracted(bite)


def _draw_moon(p: QPainter) -> None:
    """Pale golden crescent with soft craters."""
    path = _moon_path()
    p.setPen(_pen("#9c7a22", 1.6))
    p.setBrush(_radial(34, 54, 50, 22, 44, [(0.0, "#fff8cf"), (0.5, "#ffe37a"), (1.0, "#d9aa34")]))
    p.drawPath(path)
    p.save()
    p.setClipPath(path)
    p.setPen(Qt.PenStyle.NoPen)
    for x, y, r in ((22, 46, 5.0), (30, 72, 3.6), (16, 62, 2.6), (42, 86, 2.8)):
        p.setBrush(
            _radial(
                x,
                y,
                r,
                x + r * 0.3,
                y + r * 0.3,
                [(0.0, QColor(190, 140, 30, 40)), (1.0, QColor(170, 120, 20, 95))],
            )
        )
        p.drawEllipse(QPointF(x, y), r, r)
    # light along the outer (left) limb
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(_pen(QColor(255, 255, 240, 110), 3.0))
    p.drawArc(QRectF(9, 11, 82, 82), 100 * 16, 110 * 16)
    p.restore()
    _sparkle(p, 20.0, 34.0, 1.8, 200)
    _bail(p, 9.0)


def _leaf_path() -> QPainterPath:
    """Heart-shaped leaf with its tip at the origin, lobes towards -y."""
    path = QPainterPath(QPointF(0, 0))
    path.cubicTo(-8, -6, -18, -12, -17, -22)
    path.cubicTo(-16, -31, -5, -33, 0, -25)
    path.cubicTo(5, -33, 16, -31, 17, -22)
    path.cubicTo(18, -12, 8, -6, 0, 0)
    path.closeSubpath()
    return path


def _draw_clover(p: QPainter) -> None:
    """Four heart-shaped leaves with a curved stem."""
    cx, cy = 50.0, 45.0
    _bail(p, 24.0)
    # stem
    stem = QPainterPath(QPointF(cx, cy))
    stem.quadTo(QPointF(56, 72), QPointF(70, 95))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(_pen("#1d5e22", 5.2))
    p.drawPath(stem)
    p.setPen(_pen("#5cae4f", 2.2))
    p.drawPath(stem)
    # leaves
    leaf = _leaf_path()
    rib = QPainterPath(QPointF(0, -2))
    rib.quadTo(QPointF(0.8, -12), QPointF(0, -22))
    for angle in (45, 135, 225, 315):
        t = QTransform().translate(cx, cy).rotate(angle).scale(1.28, 1.28)
        p.setPen(_pen("#14531f", 1.3))
        p.setBrush(_radial(0, -18, 24, -5, -24, [(0.0, "#9be58e"), (0.5, "#43b24c"), (1.0, "#1d7a2e")]))
        p.save()
        p.setTransform(t, True)
        p.drawPath(leaf)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(_pen(QColor(225, 255, 215, 120), 1.1))
        p.drawPath(rib)
        p.restore()
    # small highlight in the middle
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(_radial(cx, cy, 5, cx - 1, cy - 1, [(0.0, "#c8f5b5"), (1.0, QColor(90, 170, 80, 0))]))
    p.drawEllipse(QPointF(cx, cy), 5, 5)
    _sparkle(p, 30.0, 26.0, 1.8, 170)


def _feather_path(length: float, width: float) -> QPainterPath:
    """Feather outline with its quill end at the origin, pointing towards +y."""
    path = QPainterPath(QPointF(0, 0))
    path.cubicTo(width * 0.95, length * 0.18, width * 0.85, length * 0.78, 0, length)
    path.cubicTo(-width * 0.85, length * 0.78, -width * 0.95, length * 0.18, 0, 0)
    path.closeSubpath()
    return path


def _draw_feather(p: QPainter, x: float, y: float, length: float, width: float, tip: str) -> None:
    path = _feather_path(length, width)
    p.save()
    p.translate(x, y)
    grad = QLinearGradient(QPointF(0, 0), QPointF(0, length))
    grad.setColorAt(0.0, QColor("#fffaf0"))
    grad.setColorAt(0.62, QColor("#e4dac8"))
    grad.setColorAt(0.70, QColor(tip))
    grad.setColorAt(1.0, QColor(tip).darker(135))
    p.setPen(_pen("#8f816a", 0.8))
    p.setBrush(QBrush(grad))
    p.drawPath(path)
    p.setPen(_pen("#8b7a5e", 0.8))
    p.drawLine(QPointF(0, 0.5), QPointF(0, length * 0.94))
    p.restore()


def _draw_dreamcatcher(p: QPainter) -> None:
    """Leather-wrapped hoop with a spiral web, beads and three feathers."""
    cx, cy, r = 50.0, 33.0, 29.0
    n = 8
    # web: rings of points, each level rotated half a step and pulled inwards
    levels = (1.0, 0.7, 0.47, 0.29)
    rings: list[list[QPointF]] = []
    for k, f in enumerate(levels):
        ring = []
        for i in range(n):
            a = math.radians(-90 + (i + 0.5 * k) * 360 / n)
            ring.append(QPointF(cx + r * f * math.cos(a), cy + r * f * math.sin(a)))
        rings.append(ring)
    web = QPainterPath()
    for k in range(len(rings) - 1):
        outer, inner = rings[k], rings[k + 1]
        web.moveTo(outer[0])
        for i in range(n):
            web.lineTo(inner[i])
            web.lineTo(outer[(i + 1) % n])
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(_pen(QColor(90, 60, 30, 120), 1.6))
    p.drawPath(web)
    p.setPen(_pen("#efe4cf", 0.8))
    p.drawPath(web)
    # centre bead
    p.setPen(_pen("#0d5f63", 0.7))
    p.setBrush(_radial(cx, cy, 4.2, cx - 1, cy - 1, [(0.0, "#a8fff2"), (0.6, "#23b5a8"), (1.0, "#0e6e6a")]))
    p.drawEllipse(QPointF(cx, cy), 3.8, 3.8)

    # hanging strings, beads and feathers (drawn before the hoop so the hoop covers the knots)
    strands = (
        (130.0, 11.0, 22.0, "#8c5a2b", "#c0392b"),
        (90.0, 10.0, 21.5, "#5b3a1e", "#23b5a8"),
        (50.0, 11.0, 22.0, "#8c5a2b", "#c0392b"),
    )
    for deg, drop, flen, tip, bead in strands:
        a = math.radians(deg)
        sx, sy = cx + r * math.cos(a), cy + r * math.sin(a)
        by = sy + drop
        p.setPen(_pen("#6b4423", 1.1))
        p.drawLine(QPointF(sx, sy), QPointF(sx, by + flen * 0.2))
        _draw_feather(p, sx, by + 2.6, flen, 5.8, tip)
        p.setPen(_pen(QColor(bead).darker(160), 0.6))
        p.setBrush(
            _radial(sx, by, 3.0, sx - 0.8, by - 0.8, [(0.0, QColor(bead).lighter(160)), (1.0, QColor(bead))])
        )
        p.drawEllipse(QPointF(sx, by), 2.8, 2.8)
        p.setBrush(Qt.BrushStyle.NoBrush)

    # hoop: dark leather with lighter wrapping
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(_pen("#3d2410", 6.4))
    p.drawEllipse(QPointF(cx, cy), r, r)
    p.setPen(_pen("#7a4a21", 4.6))
    p.drawEllipse(QPointF(cx, cy), r, r)
    wrap = QPen(QColor("#b57b45"), 4.6)
    wrap.setCapStyle(Qt.PenCapStyle.FlatCap)
    wrap.setDashPattern([0.45, 0.55])
    p.setPen(wrap)
    p.drawEllipse(QPointF(cx, cy), r, r)
    p.setPen(_pen(QColor(255, 235, 200, 90), 1.0))
    p.drawArc(QRectF(cx - r - 1.2, cy - r - 1.2, 2 * r + 2.4, 2 * r + 2.4), 95 * 16, 80 * 16)


_ARTWORK: dict[str, Callable[[QPainter], None]] = {
    "nazar": _draw_nazar,
    "heart": _draw_heart,
    "star": _draw_star,
    "moon": _draw_moon,
    "clover": _draw_clover,
    "dreamcatcher": _draw_dreamcatcher,
}


# --------------------------------------------------------------------------- public API


def builtin_pixmap(name: str, size: int = 256) -> QPixmap:
    """Render the built-in charm ``name`` into a transparent ``size x size`` pixmap.

    Results are cached per ``(name, size)``. Raises ``KeyError`` for an unknown name and
    ``ValueError`` for a non-positive size.
    """
    size = int(size)
    if size <= 0:
        raise ValueError(f"charm size must be positive, got {size}")
    key = (name, size)
    cached = _builtin_cache.get(key)
    if cached is not None:
        return cached
    try:
        draw = _ARTWORK[name]
    except KeyError:
        raise KeyError(f"unknown built-in charm {name!r}") from None

    # Paint into a QImage (raster, thread-agnostic) and convert once.
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    p = QPainter(image)
    try:
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        p.scale(size / _UNIT, size / _UNIT)
        draw(p)
    finally:
        p.end()
    pix = QPixmap.fromImage(image)
    _builtin_cache[key] = pix
    return pix


def _fallback_pixmap() -> QPixmap:
    return builtin_pixmap("nazar", CHARM_PIXMAP_PX)


def _is_plain_file_name(name: str | None) -> bool:
    """True when ``name`` is a bare file name (no directories, drive or traversal)."""
    if not name or name in (".", ".."):
        return False
    return Path(name).name == name and "/" not in name and "\\" not in name


def _read_image(path: Path) -> QImage:
    """Read and validate an image file; raises ``ValueError`` with a user-readable message."""
    display = path.name or str(path)
    try:
        if not path.is_file():
            raise ValueError(f"“{display}” does not exist or is not a file.")
        nbytes = path.stat().st_size
    except OSError as exc:
        raise ValueError(f"“{display}” cannot be accessed: {exc.strerror or exc}.") from exc
    if nbytes == 0:
        raise ValueError(f"“{display}” is empty.")
    if nbytes > MAX_FILE_BYTES:
        raise ValueError(
            f"“{display}” is too large ({nbytes / (1024 * 1024):.1f} MB). "
            f"Please choose an image smaller than {MAX_FILE_BYTES // (1024 * 1024)} MB."
        )

    reader = QImageReader(str(path))
    reader.setAutoTransform(True)  # honour EXIF orientation
    reader.setDecideFormatFromContent(True)  # don't trust the extension
    if not reader.canRead():
        raise ValueError(f"“{display}” is not an image Latkan can read. Try a PNG, JPEG or WebP file.")
    reported = reader.size()
    fmt = bytes(reader.format().data()).lower()
    if fmt in _VECTOR_FORMATS and reported.isValid() and not reported.isEmpty():
        # Vector images have no intrinsic pixel size: render them sharp at the charm resolution.
        factor = MAX_CHARM_EDGE / max(reported.width(), reported.height())
        reader.setScaledSize(
            QSize(max(1, round(reported.width() * factor)), max(1, round(reported.height() * factor)))
        )
    elif reported.isValid() and (reported.width() > MAX_IMAGE_DIM or reported.height() > MAX_IMAGE_DIM):
        raise ValueError(
            f"“{display}” is {reported.width()} × {reported.height()} pixels. "
            f"Please choose an image no larger than {MAX_IMAGE_DIM} × {MAX_IMAGE_DIM}."
        )
    image = reader.read()
    if image.isNull():
        reason = reader.errorString() or "unknown error"
        raise ValueError(f"“{display}” could not be read as an image ({reason}).")
    if image.width() > MAX_IMAGE_DIM or image.height() > MAX_IMAGE_DIM:
        raise ValueError(
            f"“{display}” is {image.width()} × {image.height()} pixels. "
            f"Please choose an image no larger than {MAX_IMAGE_DIM} × {MAX_IMAGE_DIM}."
        )
    return image


def charm_pixmap(spec: CharmSpec) -> QPixmap:
    """Pixmap for a charm spec: built-in artwork or the imported image.

    Falls back to the nazar (and logs a warning) when the built-in name is unknown or
    the image is missing/unreadable. Cached by ``spec.id``; call :func:`forget` when a
    charm is removed.
    """
    cached = _spec_cache.get(spec.id)
    if cached is not None:
        return cached
    pix = _load_spec_pixmap(spec)
    _spec_cache[spec.id] = pix
    return pix


def _load_spec_pixmap(spec: CharmSpec) -> QPixmap:
    if spec.kind == "builtin":
        if spec.name in _ARTWORK:
            return builtin_pixmap(spec.name, CHARM_PIXMAP_PX)
        logger.warning("Charm %s: unknown built-in %r, using nazar", spec.id, spec.name)
        return _fallback_pixmap()
    if spec.kind == "image":
        if not _is_plain_file_name(spec.image):
            logger.warning("Charm %s: invalid image file name %r, using nazar", spec.id, spec.image)
            return _fallback_pixmap()
        path = config.charms_dir() / spec.image
        try:
            image = _read_image(path)
        except ValueError as exc:
            logger.warning("Charm %s: cannot load image %s (%s), using nazar", spec.id, path, exc)
            return _fallback_pixmap()
        pix = QPixmap.fromImage(image)
        if pix.isNull():
            logger.warning("Charm %s: image %s converted to a null pixmap, using nazar", spec.id, path)
            return _fallback_pixmap()
        return pix
    logger.warning("Charm %s: unknown kind %r, using nazar", spec.id, spec.kind)
    return _fallback_pixmap()


def forget(spec_id: str) -> None:
    """Drop the cached pixmap for ``spec_id`` (no-op when not cached)."""
    _spec_cache.pop(spec_id, None)


def import_image(src: Path) -> str:
    """Import a user image as a charm and return its file name inside ``charms_dir()``.

    The image is validated (<= 40 MB file, <= 8000 x 8000 px), EXIF-rotated, converted
    to ARGB32, downscaled so its long edge is at most 512 px and saved as
    ``<uuid4hex>.png``. Raises ``ValueError`` with a user-readable message on failure.
    """
    src = Path(src)
    image = _read_image(src)
    width, height = image.width(), image.height()
    long_edge = max(width, height)
    if long_edge > MAX_CHARM_EDGE:
        factor = MAX_CHARM_EDGE / long_edge
        target = QSize(max(1, round(width * factor)), max(1, round(height * factor)))
        image = image.scaled(
            target, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
        if image.isNull():
            raise ValueError(f"“{src.name}” could not be resized (out of memory?).")
    # Downscale first (cheaper on huge inputs), then normalise the pixel format.
    image = image.convertToFormat(QImage.Format.Format_ARGB32)
    if image.isNull():
        raise ValueError(f"“{src.name}” could not be converted (out of memory?).")

    dest_dir = config.charms_dir()
    name = f"{uuid.uuid4().hex}.png"
    dest = dest_dir / name
    tmp = dest_dir / f".{name}.tmp"
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        if not image.save(str(tmp), "PNG"):
            raise ValueError(f"Could not save the charm image to “{dest_dir}”.")
        os.replace(tmp, dest)
    except OSError as exc:
        raise ValueError(f"Could not save the charm image to “{dest_dir}”: {exc.strerror or exc}.") from exc
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            logger.debug("Could not remove temporary file %s", tmp, exc_info=True)
    logger.info("Imported charm image %s -> %s (%dx%d)", src, name, image.width(), image.height())
    return name


def delete_image(image: str) -> None:
    """Remove an imported image from ``charms_dir()`` if present (never raises)."""
    if not _is_plain_file_name(image):
        logger.warning("Refusing to delete invalid charm image name %r", image)
        return
    path = config.charms_dir() / image
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not delete charm image %s", path, exc_info=True)


def app_icon() -> QIcon:
    """Application / tray icon: the nazar rendered at 16..256 px."""
    icon = QIcon()
    for size in ICON_SIZES:
        icon.addPixmap(builtin_pixmap("nazar", size))
    return icon


def _template_eye_pixmap(size: int) -> QPixmap:
    """Monochrome nazar glyph (outer ring + iris with a pupil hole) for template tray icons.

    A template image is drawn by macOS from its alpha channel only, so the colourful nazar would
    turn into a featureless disc; this keeps the "eye" readable at menu-bar size.
    """
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    p = QPainter(image)
    try:
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.scale(size / _UNIT, size / _UNIT)
        glyph = QPainterPath()
        glyph.addEllipse(QPointF(50, 50), 46, 46)
        hole = QPainterPath()
        hole.addEllipse(QPointF(50, 50), 33, 33)
        glyph = glyph.subtracted(hole)
        iris = QPainterPath()
        iris.addEllipse(QPointF(50, 50), 21, 21)
        pupil = QPainterPath()
        pupil.addEllipse(QPointF(50, 50), 8.5, 8.5)
        glyph.addPath(iris.subtracted(pupil))
        p.fillPath(glyph, QColor(0, 0, 0))
    finally:
        p.end()
    return QPixmap.fromImage(image)


def tray_icon(mask: bool = False) -> QIcon:
    """Icon for the system tray: :func:`app_icon`, or a monochrome template glyph when ``mask``.

    With ``mask`` the returned icon already has ``setIsMask(True)`` (macOS menu-bar template image,
    tinted by the system for light/dark menu bars).
    """
    if not mask:
        return app_icon()
    icon = QIcon()
    for size in (16, 18, 22, 32, 36, 44, 64):
        icon.addPixmap(_template_eye_pixmap(size))
    icon.setIsMask(True)
    return icon


def _clear_caches() -> None:
    """Drop every cached pixmap (tests)."""
    _builtin_cache.clear()
    _spec_cache.clear()
