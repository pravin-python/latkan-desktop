#!/usr/bin/env python3
"""Render Latkan's application icons from the built-in nazar charm artwork.

Writes into ``assets/`` (or ``--out DIR``):

* ``icon.png``      1024x1024 master app icon: the nazar hanging from a gold thread on a soft
                    rounded-square background (macOS icon grid: 824 px body, 100 px transparent margin).
* ``icon-256.png``  256x256 render of the same design (Linux hicolor theme, AppImage ``.DirIcon``).
* ``icon.ico``      Windows icon; every size (16..256 px) is rendered natively, not downscaled.
* ``icon.icns``     macOS icon; every retina size (32..1024 px) is rendered natively.
* ``tray.png``      64x64 plain nazar on a transparent background.

``icon.ico`` / ``icon.icns`` need Pillow (``pip install pillow``); ``--png-only`` skips them, which is
what ``packaging/linux/install-from-source.sh`` uses.

The script is idempotent: output is deterministic and a file is only rewritten when its bytes change.
It runs headless (``QT_QPA_PLATFORM=offscreen`` unless the caller set another platform).

Usage::

    python scripts/make_icons.py [--out DIR] [--png-only] [-q]
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT / "src"
DEFAULT_OUT = ROOT / "assets"

# Must happen before any Qt / latkan import.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import QApplication

log = logging.getLogger("make_icons")

MASTER_SIZE = 1024
LINUX_SIZE = 256
TRAY_SIZE = 64
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
ICNS_SIZES = (32, 64, 128, 256, 512, 1024)  # sizes Pillow's ICNS writer stores
DETAILED_MIN = 64  # below this the thread + ring would be sub-pixel, so a compact layout is used

NazarRenderer = Callable[[int], QImage]


# ------------------------------------------------------------------------------------------ helpers


def _to_rgba(img: QImage) -> QImage:
    """Return a byte-ordered RGBA copy with device pixel ratio 1 (endian independent pixel access)."""
    out = img.convertToFormat(QImage.Format.Format_RGBA8888)
    out.setDevicePixelRatio(1.0)
    return out


def _opaque_bounds(img: QImage, threshold: int = 24) -> QRectF:
    """Bounding box (pixels) of the pixels whose alpha exceeds ``threshold``; full rect if none."""
    rgba = _to_rgba(img)
    width, height, stride = rgba.width(), rgba.height(), rgba.bytesPerLine()
    data = bytes(rgba.constBits())
    top = bottom = left = right = -1
    for y in range(height):
        alpha = data[y * stride : y * stride + width * 4][3::4]
        if max(alpha, default=0) <= threshold:
            continue
        xs = [x for x, a in enumerate(alpha) if a > threshold]
        if top < 0:
            top = y
        bottom = y
        left = xs[0] if left < 0 else min(left, xs[0])
        right = max(right, xs[-1])
    if top < 0:
        return QRectF(0, 0, width, height)
    return QRectF(left, top, right - left + 1, bottom - top + 1)


def _png_bytes(img: QImage) -> bytes:
    """Encode ``img`` as PNG in memory (Qt's encoder is deterministic: no timestamps)."""
    array = QByteArray()
    buffer = QBuffer(array)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    try:
        if not img.save(buffer, "PNG"):
            raise RuntimeError("Qt failed to encode PNG")
    finally:
        buffer.close()
    return bytes(array.data())


def _shown(path: Path) -> str:
    """``path`` relative to the repository when it lives inside it (shorter log lines)."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _write_if_changed(path: Path, data: bytes) -> None:
    """Atomically write ``data`` to ``path`` unless the file already holds exactly these bytes."""
    if path.is_file() and path.read_bytes() == data:
        log.info("unchanged  %s", _shown(path))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    log.info("wrote      %s (%d bytes)", _shown(path), len(data))


# ---------------------------------------------------------------------------------------- rendering


class NazarArt:
    """Renders the nazar charm at any pixel size and knows where its visible pixels are.

    Placement uses the artwork's opaque bounding box, so transparent padding inside the charm
    pixmap (whatever ``latkan.charms`` chooses) does not affect the icon layout.
    """

    PROBE_SIZE = 256

    def __init__(self, render: NazarRenderer) -> None:
        self._render = render
        box = _opaque_bounds(render(self.PROBE_SIZE))
        n = float(self.PROBE_SIZE)
        self.frac = QRectF(box.x() / n, box.y() / n, box.width() / n, box.height() / n)

    def image(self, size: int) -> QImage:
        return self._render(size)

    @property
    def aspect(self) -> float:
        """Visible height / visible width."""
        return self.frac.height() / max(self.frac.width(), 1e-3)

    def draw(self, p: QPainter, width: float, center_x: float, top: float) -> QRectF:
        """Draw so the visible art is ``width`` wide, centred on ``center_x``, starting at ``top``.

        The artwork is re-rendered at the exact pixel size needed (crisp vector output rather than a
        resample). Returns the rectangle the visible artwork occupies.
        """
        f = self.frac
        size = max(8, round(width / max(f.width(), 1e-3)))
        x = round(center_x - (f.x() + f.width() / 2) * size)
        y = round(top - f.y() * size)
        p.drawImage(QRectF(x, y, size, size), self._render(size))
        return QRectF(x + f.x() * size, y + f.y() * size, f.width() * size, f.height() * size)


def render_app_icon(size: int, art: NazarArt) -> QImage:
    """Compose the application icon at ``size`` x ``size`` pixels."""
    s = float(size)
    detailed = size >= DETAILED_MIN
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)

    p = QPainter(img)
    try:
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        margin = s * (100 / 1024) if detailed else max(0.5, s * 0.03)
        body = QRectF(margin, margin, s - 2 * margin, s - 2 * margin)
        radius = body.width() * 0.225
        tile = QPainterPath()
        tile.addRoundedRect(body, radius, radius)

        if detailed:  # soft drop shadow faked with stacked translucent tiles (no blur filter needed)
            steps = 10
            for i in range(steps, 0, -1):
                grow = i * s / 700
                shadow = QPainterPath()
                drop = s * 0.012
                shadow.addRoundedRect(
                    body.adjusted(-grow, -grow + drop, grow, grow + drop), radius + grow, radius + grow
                )
                p.fillPath(shadow, QColor(70, 35, 0, int(26 * (1 - i / (steps + 1)))))

        background = QLinearGradient(body.topLeft(), body.bottomLeft())
        background.setColorAt(0.0, QColor("#fffaf1"))
        background.setColorAt(1.0, QColor("#ffd7a3"))
        p.fillPath(tile, background)

        glow = QRadialGradient(QPointF(s / 2, s * (0.58 if detailed else 0.5)), body.width() * 0.48)
        glow.setColorAt(0.0, QColor(255, 255, 255, 170))
        glow.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillPath(tile, glow)

        p.setPen(QPen(QColor(150, 95, 25, 70), max(1.0, s / 400)))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(tile)

        if not detailed:  # compact layout: just the eye, as large as the tile allows
            art_w = body.width() * 0.84
            art.draw(p, art_w, s / 2, (s - art_w * art.aspect) / 2)
            return img

        # Hanging layout: thread from the top of the tile -> small gold ring -> nazar.
        art_w = s * 0.54
        art_top = body.bottom() - s * 0.07 - art_w * art.aspect
        ring_r = max(1.5, s * 0.022)
        ring_w = max(1.5, s * 0.011)
        ring_c = QPointF(s / 2, art_top - ring_r * 0.45)

        thread_top = QPointF(s / 2, body.top() + ring_w / 2)
        thread_end = QPointF(s / 2, ring_c.y() - ring_r)
        cap = Qt.PenCapStyle.FlatCap
        p.setPen(QPen(QColor(120, 82, 18, 150), max(1.5, s * 0.017), Qt.PenStyle.SolidLine, cap))
        p.drawLine(thread_top, thread_end)
        p.setPen(QPen(QColor("#e3b64a"), max(1.0, s * 0.010), Qt.PenStyle.SolidLine, cap))
        p.drawLine(thread_top, thread_end)

        art.draw(p, art_w, s / 2, art_top)

        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(120, 82, 18, 160), ring_w * 1.6))
        p.drawEllipse(ring_c, ring_r, ring_r)
        p.setPen(QPen(QColor("#d4a93a"), ring_w))
        p.drawEllipse(ring_c, ring_r, ring_r)
    finally:
        p.end()
    return img


# ------------------------------------------------------------------------------------------- output


def _pillow_outputs(out_dir: Path, art: NazarArt) -> None:
    """Write icon.ico and icon.icns using Pillow, feeding it natively rendered frames."""
    from PIL import Image  # imported lazily so --png-only works without Pillow

    def pil(size: int) -> Image.Image:
        with Image.open(io.BytesIO(_png_bytes(render_app_icon(size, art)))) as im:
            return im.convert("RGBA")

    frames = {size: pil(size) for size in sorted(set(ICO_SIZES) | set(ICNS_SIZES))}

    ico = io.BytesIO()
    frames[256].save(
        ico,
        format="ICO",
        sizes=[(n, n) for n in ICO_SIZES],
        append_images=[frames[n] for n in ICO_SIZES if n != 256],
    )
    _write_if_changed(out_dir / "icon.ico", ico.getvalue())

    icns = io.BytesIO()
    frames[MASTER_SIZE].save(
        icns, format="ICNS", append_images=[frames[n] for n in ICNS_SIZES if n != MASTER_SIZE]
    )
    _write_if_changed(out_dir / "icon.icns", icns.getvalue())

    with Image.open(out_dir / "icon.ico") as check:
        log.info("icon.ico sizes: %s", sorted(check.info.get("sizes", ())))


def build_icons(out_dir: Path, png_only: bool = False) -> None:
    """Render every icon into ``out_dir``. Raises ImportError / RuntimeError on failure."""
    from latkan import charms  # imported late: needs sys.path and a QApplication first

    def render(size: int) -> QImage:
        pix = charms.builtin_pixmap("nazar", size)
        if pix.isNull():
            raise RuntimeError(f"charms.builtin_pixmap('nazar', {size}) returned a null pixmap")
        img = pix.toImage()
        img.setDevicePixelRatio(1.0)
        return img

    art = NazarArt(render)
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_if_changed(out_dir / "icon.png", _png_bytes(render_app_icon(MASTER_SIZE, art)))
    _write_if_changed(out_dir / "icon-256.png", _png_bytes(render_app_icon(LINUX_SIZE, art)))
    _write_if_changed(out_dir / "tray.png", _png_bytes(render(TRAY_SIZE)))
    if not png_only:
        _pillow_outputs(out_dir, art)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render Latkan's app icons from the nazar charm.")
    parser.add_argument(
        "--out", type=Path, default=DEFAULT_OUT, help=f"output directory (default: {DEFAULT_OUT})"
    )
    parser.add_argument(
        "--png-only", action="store_true", help="skip icon.ico / icon.icns (no Pillow needed)"
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="only log warnings and errors")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO, format="%(message)s", force=True
    )

    _app = QApplication.instance() or QApplication([sys.argv[0]])  # keep a reference for the whole run
    try:
        build_icons(args.out.resolve(), png_only=args.png_only)
    except ImportError as exc:
        name = getattr(exc, "name", "") or ""
        if name == "PIL":
            log.error(
                "Pillow is required for icon.ico / icon.icns: python -m pip install pillow "
                "(or pass --png-only)"
            )
        else:
            log.error(
                "Cannot import the Latkan artwork (%s). Run from a source checkout with src/latkan "
                "present and PySide6 installed.",
                exc,
            )
        return 1
    except (OSError, RuntimeError, ValueError) as exc:
        log.error("Icon generation failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
