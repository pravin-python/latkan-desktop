"""Tests for latkan.charms (artwork, cache, image import) and latkan.rendering (ropes, geometry)."""

from __future__ import annotations

import logging
import math
import os
import re

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QImageReader, QPainter, QPixmap, QTransform  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from latkan import charms, config, rendering  # noqa: E402
from latkan.config import CharmSpec  # noqa: E402

ROPE_STYLES = ("gold", "silver", "neon", "kalava")


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    """One QApplication for the module (QPixmap needs a GUI application)."""
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _fresh_caches():
    charms._clear_caches()
    yield
    charms._clear_caches()


@pytest.fixture
def latkan_home(tmp_path, monkeypatch):
    monkeypatch.setenv("LATKAN_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def _blank(w: int = 300, h: int = 300) -> QImage:
    img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    return img


def _opaque_pixels(img: QImage, step: int = 1) -> int:
    count = 0
    for y in range(0, img.height(), step):
        for x in range(0, img.width(), step):
            if img.pixelColor(x, y).alpha() > 0:
                count += 1
    return count


def _hanging_points(
    anchor_x: float = 150.0, length: float = 180.0, segments: int = 12, sway: float = 40.0
) -> list[tuple[float, float]]:
    pts = []
    for i in range(segments + 1):
        t = i / segments
        pts.append((anchor_x + sway * t * t, length * t))
    return pts


def _save_png(path, w: int, h: int, color: QColor | None = None) -> None:
    img = QImage(w, h, QImage.Format.Format_ARGB32)
    img.fill(color if color is not None else QColor(200, 40, 90, 255))
    assert img.save(str(path), "PNG")


# --------------------------------------------------------------------------- charms: artwork


def test_labels_match_builtin_names():
    assert tuple(charms.BUILTIN_LABELS) == tuple(config.BUILTIN_NAMES)
    assert all(isinstance(v, str) and v for v in charms.BUILTIN_LABELS.values())


@pytest.mark.parametrize("name", list(config.BUILTIN_NAMES))
def test_builtin_renders_with_transparent_corners(name):
    pix = charms.builtin_pixmap(name, 128)
    assert not pix.isNull()
    assert (pix.width(), pix.height()) == (128, 128)
    img = pix.toImage()
    for x, y in ((0, 0), (127, 0), (0, 127), (127, 127)):
        assert img.pixelColor(x, y).alpha() == 0, f"{name}: corner {(x, y)} not transparent"
    painted = _opaque_pixels(img, step=2)
    assert painted > (64 * 64) * 0.15, f"{name}: only {painted} painted samples"


@pytest.mark.parametrize("name", list(config.BUILTIN_NAMES))
def test_builtin_small_sizes(name):
    for size in (16, 32):
        pix = charms.builtin_pixmap(name, size)
        assert (pix.width(), pix.height()) == (size, size)
        assert _opaque_pixels(pix.toImage()) > 0


def test_builtin_pixmap_is_cached_per_name_and_size():
    a = charms.builtin_pixmap("heart", 64)
    b = charms.builtin_pixmap("heart", 64)
    c = charms.builtin_pixmap("heart", 65)
    assert a.cacheKey() == b.cacheKey()
    assert a.cacheKey() != c.cacheKey()


def test_builtin_pixmap_rejects_bad_input():
    with pytest.raises(KeyError):
        charms.builtin_pixmap("unicorn")
    with pytest.raises(ValueError):
        charms.builtin_pixmap("nazar", 0)


def test_app_icon_contains_all_sizes():
    icon = charms.app_icon()
    assert not icon.isNull()
    sizes = {s.width() for s in icon.availableSizes()}
    assert {16, 32, 48, 64, 128, 256} <= sizes


def test_tray_icon_colour_and_template():
    assert not charms.tray_icon().isMask()
    template = charms.tray_icon(mask=True)
    assert template.isMask()
    assert {16, 18, 22, 32, 36, 44} <= {s.width() for s in template.availableSizes()}
    img = template.pixmap(64, 64).toImage()
    # radii in the 100-unit design space: pupil 8.5, iris 21, gap to 33, outer ring to 46 (x0.64 px)
    assert img.pixelColor(32, 32).alpha() == 0  # pupil is a hole
    assert img.pixelColor(32, 32 - 9).alpha() > 200  # iris ring
    assert img.pixelColor(32, 32 - 17).alpha() == 0  # gap between iris and outer ring
    assert img.pixelColor(32, 32 - 25).alpha() > 200  # outer ring
    assert img.pixelColor(1, 1).alpha() == 0  # transparent corners


# --------------------------------------------------------------------------- charms: spec pixmaps


def test_charm_pixmap_builtin_and_cache(latkan_home):
    spec = CharmSpec.new_builtin("star", 0.5, 150.0)
    pix = charms.charm_pixmap(spec)
    assert not pix.isNull()
    assert charms.charm_pixmap(spec).cacheKey() == pix.cacheKey()
    charms.forget(spec.id)
    charms.forget(spec.id)  # idempotent
    assert not charms.charm_pixmap(spec).isNull()


def test_charm_pixmap_missing_image_falls_back_to_nazar(latkan_home, caplog):
    spec = CharmSpec.new_image("0123456789abcdef0123456789abcdef.png", 0.5, 150.0)
    with caplog.at_level(logging.WARNING, logger="latkan.charms"):
        pix = charms.charm_pixmap(spec)
    assert not pix.isNull()
    nazar = charms.builtin_pixmap("nazar", charms.CHARM_PIXMAP_PX)
    assert pix.cacheKey() == nazar.cacheKey()
    assert any("using nazar" in r.getMessage() for r in caplog.records)


def test_charm_pixmap_rejects_path_traversal(latkan_home, caplog):
    spec = CharmSpec(id="a" * 32, kind="image", image="../secret.png")
    with caplog.at_level(logging.WARNING, logger="latkan.charms"):
        pix = charms.charm_pixmap(spec)
    assert pix.cacheKey() == charms.builtin_pixmap("nazar", charms.CHARM_PIXMAP_PX).cacheKey()


def test_charm_pixmap_unknown_builtin_falls_back(latkan_home):
    spec = CharmSpec(id="b" * 32, kind="builtin", name="no-such-charm")
    pix = charms.charm_pixmap(spec)
    assert pix.cacheKey() == charms.builtin_pixmap("nazar", charms.CHARM_PIXMAP_PX).cacheKey()


# --------------------------------------------------------------------------- charms: import


def test_import_image_round_trip(latkan_home, tmp_path):
    src = tmp_path / "small.png"
    _save_png(src, 100, 60)
    name = charms.import_image(src)
    assert re.fullmatch(r"[0-9a-f]{32}\.png", name)
    dest = config.charms_dir() / name
    assert dest.is_file()
    img = QImage(str(dest))
    assert (img.width(), img.height()) == (100, 60)
    assert img.pixelColor(50, 30).red() == 200
    assert not list(config.charms_dir().glob("*.tmp"))  # no temp leftovers

    spec = CharmSpec.new_image(name, 0.3, 120.0)
    pix = charms.charm_pixmap(spec)
    assert (pix.width(), pix.height()) == (100, 60)

    charms.delete_image(name)
    assert not dest.exists()
    charms.delete_image(name)  # already gone: no error


def test_import_image_downscales_long_edge(latkan_home, tmp_path):
    src = tmp_path / "wide.png"
    _save_png(src, 2000, 1000)
    name = charms.import_image(src)
    img = QImage(str(config.charms_dir() / name))
    assert (img.width(), img.height()) == (512, 256)


def test_import_image_downscales_tall_image(latkan_home, tmp_path):
    src = tmp_path / "tall.png"
    _save_png(src, 300, 1200)
    img = QImage(str(config.charms_dir() / charms.import_image(src)))
    assert (img.width(), img.height()) == (128, 512)


def test_import_image_keeps_alpha(latkan_home, tmp_path):
    src = tmp_path / "alpha.png"
    _save_png(src, 40, 40, QColor(0, 0, 255, 0))
    img = QImage(str(config.charms_dir() / charms.import_image(src)))
    assert img.hasAlphaChannel()
    assert img.pixelColor(10, 10).alpha() == 0


@pytest.mark.skipif(
    b"svg" not in [bytes(f.data()) for f in QImageReader.supportedImageFormats()],
    reason="Qt SVG image plugin not available",
)
def test_import_svg_is_rendered_at_charm_resolution(latkan_home, tmp_path):
    src = tmp_path / "drop.svg"
    src.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="40" viewBox="0 0 20 40">'
        '<ellipse cx="10" cy="20" rx="10" ry="20" fill="#3080ff"/></svg>',
        encoding="utf-8",
    )
    img = QImage(str(config.charms_dir() / charms.import_image(src)))
    assert (img.width(), img.height()) == (256, 512)
    assert img.pixelColor(128, 256).alpha() == 255
    assert img.pixelColor(0, 0).alpha() == 0


def test_import_image_rejects_non_image(latkan_home, tmp_path):
    bogus = tmp_path / "notes.txt"
    bogus.write_text("definitely not an image", encoding="utf-8")
    with pytest.raises(ValueError, match="notes.txt"):
        charms.import_image(bogus)
    fake_png = tmp_path / "fake.png"
    fake_png.write_bytes(b"\x00\x01\x02 garbage " * 20)
    with pytest.raises(ValueError):
        charms.import_image(fake_png)


def test_import_image_rejects_missing_and_empty(latkan_home, tmp_path):
    with pytest.raises(ValueError):
        charms.import_image(tmp_path / "nope.png")
    empty = tmp_path / "empty.png"
    empty.write_bytes(b"")
    with pytest.raises(ValueError, match="empty"):
        charms.import_image(empty)


def test_import_image_enforces_limits(latkan_home, tmp_path, monkeypatch):
    src = tmp_path / "big.png"
    _save_png(src, 200, 100)
    monkeypatch.setattr(charms, "MAX_IMAGE_DIM", 150)
    with pytest.raises(ValueError, match="200"):
        charms.import_image(src)
    monkeypatch.setattr(charms, "MAX_IMAGE_DIM", 8000)
    monkeypatch.setattr(charms, "MAX_FILE_BYTES", 10)
    with pytest.raises(ValueError, match="too large"):
        charms.import_image(src)
    assert not config.charms_dir().exists() or not list(config.charms_dir().iterdir())


def test_delete_image_refuses_paths(latkan_home, tmp_path):
    victim = tmp_path / "home" / "victim.png"
    victim.parent.mkdir(parents=True, exist_ok=True)
    victim.write_bytes(b"x")
    charms.delete_image("../victim.png")
    assert victim.exists()


# --------------------------------------------------------------------------- rendering: geometry


def test_charm_size_keeps_aspect():
    square = charms.builtin_pixmap("nazar", 128)
    assert rendering.charm_size(square, 1.0) == pytest.approx((96.0, 96.0))
    assert rendering.charm_size(square, 1.6) == pytest.approx((153.6, 153.6))
    wide = QPixmap(200, 100)
    assert rendering.charm_size(wide, 1.0) == pytest.approx((96.0, 48.0))


def test_charm_size_with_device_pixel_ratio():
    pix = QPixmap(400, 100)
    pix.fill(Qt.GlobalColor.red)
    pix.setDevicePixelRatio(2.0)
    assert rendering.charm_size(pix, 1.0) == pytest.approx((96.0, 24.0))
    assert rendering.charm_size(QPixmap(), 1.0) == pytest.approx((96.0, 96.0))


def test_charm_transform_convention():
    t = rendering.charm_transform((100.0, 50.0), 0.0)
    p = t.map(QPointF(0, 10))
    assert (p.x(), p.y()) == pytest.approx((100.0, 60.0))
    # positive angle: end is to +x of the node above -> local +y points towards +x
    t = rendering.charm_transform((100.0, 50.0), math.pi / 2)
    p = t.map(QPointF(0, 10))
    assert (p.x(), p.y()) == pytest.approx((110.0, 50.0), abs=1e-9)


def test_charm_local_rect():
    pix = QPixmap(200, 100)
    rect = rendering.charm_local_rect(pix, 1.0)
    assert rect == QRectF(-48.0, 0.0, 96.0, 48.0 + rendering.HOOK_PX)


@pytest.mark.parametrize("angle", [0.0, 0.6, -1.1])
def test_charm_hit_centre_and_far_away(angle):
    pix = charms.builtin_pixmap("nazar", 128)
    end = (200.0, 100.0)
    _, h = rendering.charm_size(pix, 1.0)
    centre = rendering.charm_transform(end, angle).map(QPointF(0.0, rendering.HOOK_PX + h / 2))
    assert rendering.charm_hit(centre, pix, end, angle, 1.0)
    assert not rendering.charm_hit(QPointF(600.0, 600.0), pix, end, angle, 1.0)
    assert not rendering.charm_hit(QPointF(200.0, 60.0), pix, end, angle, 1.0)  # above the rope end


def test_charm_hit_rotation_moves_the_body():
    pix = charms.builtin_pixmap("nazar", 128)
    end = (200.0, 100.0)
    below = QPointF(200.0, 100.0 + rendering.HOOK_PX + 48.0)
    right = QPointF(200.0 + rendering.HOOK_PX + 48.0, 100.0)
    assert rendering.charm_hit(below, pix, end, 0.0, 1.0)
    assert not rendering.charm_hit(right, pix, end, 0.0, 1.0)
    assert rendering.charm_hit(right, pix, end, math.pi / 2, 1.0)
    assert not rendering.charm_hit(below, pix, end, math.pi / 2, 1.0)
    # accepts QPoint-like input too
    assert rendering.charm_hit(below.toPoint(), pix, end, 0.0, 1.0)


def test_charm_bounds_unrotated_and_rotated():
    pix = charms.builtin_pixmap("nazar", 128)
    end = (200.0, 100.0)
    pad = 6
    bounds = rendering.charm_bounds(pix, end, 0.0, 1.0, pad)
    expected = QRectF(200 - 48 - pad, 100 - pad, 96 + 2 * pad, 96 + rendering.HOOK_PX + 2 * pad)
    assert bounds == expected.toAlignedRect()
    for angle in (0.4, -0.9, math.pi / 2):
        b = rendering.charm_bounds(pix, end, angle, 1.0, pad)
        local = rendering.charm_local_rect(pix, 1.0)
        t = rendering.charm_transform(end, angle)
        for corner in (local.topLeft(), local.topRight(), local.bottomLeft(), local.bottomRight()):
            assert QRectF(b).contains(t.map(corner))


def test_rope_path_endpoints():
    pts = _hanging_points()
    path = rendering.rope_path(pts)
    start, stop = path.pointAtPercent(0.0), path.pointAtPercent(1.0)
    assert (start.x(), start.y()) == pytest.approx(pts[0])
    assert (stop.x(), stop.y()) == pytest.approx(pts[-1])
    assert rendering.rope_path([]).isEmpty()
    assert rendering.rope_path([(1.0, 2.0)]).elementCount() == 1
    line = rendering.rope_path([(0.0, 0.0), (0.0, 50.0)])
    assert line.length() == pytest.approx(50.0)
    assert rendering.rope_path([QPointF(0, 0), QPointF(3, 4)]).length() == pytest.approx(5.0)


@pytest.mark.parametrize("segments", [1, 2, 3, 12, 30])
def test_rope_bounds_one_rect_per_segment(segments):
    pts = _hanging_points(segments=segments)
    rects = rendering.rope_bounds(pts, 9)
    assert len(rects) == segments
    assert all(not r.isEmpty() for r in rects)


def test_rope_bounds_cover_curve_even_when_zigzag():
    pts = [(100.0 + (40.0 if i % 2 else -40.0) * (i > 0), i * 30.0) for i in range(13)]
    rects = [QRectF(r) for r in rendering.rope_bounds(pts, 1)]
    path = rendering.rope_path(pts)
    for k in range(401):
        pt = path.pointAtPercent(k / 400)
        assert any(r.contains(pt) for r in rects), f"curve point {pt} not covered"
    assert rendering.rope_bounds([], 9) == []
    assert rendering.rope_bounds([(1.0, 1.0)], 9) == []


# --------------------------------------------------------------------------- rendering: painting


@pytest.mark.parametrize("style", ROPE_STYLES + ("unknown-style",))
def test_draw_rope_paints_pixels(style):
    img = _blank()
    p = QPainter(img)
    rendering.draw_rope(p, _hanging_points(), style)
    assert p.transform() == QTransform()
    p.end()
    assert _opaque_pixels(img) > 100
    # nothing painted far from the rope
    assert img.pixelColor(10, 290).alpha() == 0


def test_draw_rope_degenerate_inputs():
    img = _blank()
    p = QPainter(img)
    rendering.draw_rope(p, [], "gold")
    rendering.draw_rope(p, [(5.0, 5.0)], "silver")
    rendering.draw_rope(p, [(5.0, 5.0), (5.0, 5.0)], "silver")  # zero-length chain
    p.end()


@pytest.mark.parametrize("style", ROPE_STYLES)
def test_draw_charm_paints_body_and_ring(style):
    img = _blank()
    pix = charms.builtin_pixmap("nazar", charms.CHARM_PIXMAP_PX)
    end = (150.0, 60.0)
    p = QPainter(img)
    rendering.draw_charm(p, pix, end, 0.0, 1.0, style)
    assert p.transform() == QTransform()
    p.end()
    # the nazar's pupil is opaque at the image centre
    assert img.pixelColor(150, 60 + rendering.HOOK_PX + 48).alpha() == 255
    # the ring is painted just below the rope end
    ring = [img.pixelColor(150 + dx, 60 + dy).alpha() for dx in range(-4, 5) for dy in range(0, 7)]
    assert max(ring) > 0
    # nothing painted outside the charm bounds
    bounds = rendering.charm_bounds(pix, end, 0.0, 1.0, 2)
    assert img.pixelColor(bounds.left() - 3, bounds.top() + 20).alpha() == 0
    assert img.pixelColor(150, bounds.bottom() + 3).alpha() == 0


def test_draw_charm_rotated_and_hidpi_pixmap():
    img = _blank()
    pix = QPixmap(400, 200)
    pix.fill(QColor(20, 200, 20))
    pix.setDevicePixelRatio(2.0)
    end = (150.0, 150.0)
    p = QPainter(img)
    rendering.draw_charm(p, pix, end, math.pi / 2, 1.0, "gold")
    p.end()
    # rotated +90 degrees: the body extends to +x of the rope end
    centre = rendering.charm_transform(end, math.pi / 2).map(QPointF(0, rendering.HOOK_PX + 24))
    c = img.pixelColor(round(centre.x()), round(centre.y()))
    assert c.alpha() == 255 and c.green() > 150
    assert img.pixelColor(150, 150 + rendering.HOOK_PX + 24).alpha() == 0


def test_draw_charm_on_hidpi_device():
    img = QImage(600, 600, QImage.Format.Format_ARGB32_Premultiplied)
    img.setDevicePixelRatio(2.0)
    img.fill(Qt.GlobalColor.transparent)
    pix = charms.builtin_pixmap("heart", charms.CHARM_PIXMAP_PX)
    p = QPainter(img)
    rendering.draw_charm(p, pix, (150.0, 50.0), 0.2, 1.3, "kalava")
    p.end()
    assert _opaque_pixels(img, step=3) > 200
