"""Platform integration tests: autostart round-trips and OS-specific logic.

Every test runs on every OS: native backends are replaced by temp dirs / fakes, and native calls
are only exercised where they are guaranteed not to touch the real system.
"""

from __future__ import annotations

import logging
import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect, Qt

import latkan.platform as platform_pkg
from latkan.platform import get_platform
from latkan.platform import linux as linux_mod
from latkan.platform import macos as macos_mod
from latkan.platform import windows as windows_mod
from latkan.platform.base import PlatformIntegration, windowless_python
from latkan.platform.linux import LinuxPlatform, desktop_exec
from latkan.platform.macos import MacOSPlatform
from latkan.platform.windows import (
    AUTOSTART_VALUE,
    RUN_KEY,
    STARTUP_APPROVED_KEY,
    WindowsPlatform,
)

# --------------------------------------------------------------------------------------------
# helpers


class FakeRegistry:
    """In-memory stand-in for the HKCU registry backend."""

    available = True

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], Any] = {}
        self.fail_get = False
        self.fail_set = False

    def get(self, subkey: str, name: str) -> Any:
        if self.fail_get:
            raise PermissionError("access denied")
        return self.values.get((subkey, name))

    def set(self, subkey: str, name: str, value: str) -> None:
        if self.fail_set:
            raise PermissionError("access denied")
        self.values[(subkey, name)] = value

    def delete(self, subkey: str, name: str) -> None:
        self.values.pop((subkey, name), None)


class FakeScreen:
    def __init__(self, rect: QRect) -> None:
        self._rect = rect

    def geometry(self) -> QRect:
        return QRect(self._rect)


def _parse_desktop(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "[Desktop Entry]"
    return dict(line.split("=", 1) for line in lines[1:] if "=" in line)


@pytest.fixture
def source_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    """Pretend we run from source with an interpreter whose path contains a space."""
    exe_dir = tmp_path / "Py Home"
    exe_dir.mkdir()
    exe = exe_dir / "python3"
    exe.write_text("")
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.delenv("APPIMAGE", raising=False)
    return str(exe)


@pytest.fixture
def clean_platform_cache():
    platform_pkg.clear_cache()
    yield
    platform_pkg.clear_cache()


# --------------------------------------------------------------------------------------------
# base


def test_base_defaults_are_safe(tmp_path: Path) -> None:
    base = PlatformIntegration()
    assert base.name == "generic"
    base.prepare_environment()
    base.configure_application(object())
    base.configure_overlay(object())
    assert base.tray_icon_is_mask() is False
    assert base.autostart_supported() is False
    assert base.autostart_enabled() is False
    base.set_autostart(False)  # disabling an unsupported feature is a no-op
    with pytest.raises(OSError):
        base.set_autostart(True)


def test_base_overlay_flags_and_geometry() -> None:
    flags = PlatformIntegration().overlay_window_flags()
    for flag in (
        Qt.WindowType.FramelessWindowHint,
        Qt.WindowType.WindowStaysOnTopHint,
        Qt.WindowType.Tool,
        Qt.WindowType.WindowDoesNotAcceptFocus,
    ):
        assert flags & flag == flag
    rect = QRect(-1280, 0, 1280, 1024)
    assert PlatformIntegration().overlay_geometry(FakeScreen(rect)) == rect


def test_only_windows_layered_overlays_go_without_a_window_mask(monkeypatch: pytest.MonkeyPatch) -> None:
    assert PlatformIntegration().uses_window_mask() is True
    assert LinuxPlatform().uses_window_mask() is True  # X11 has no per-pixel input test
    assert MacOSPlatform().uses_window_mask() is True
    windows = WindowsPlatform(registry=FakeRegistry())
    monkeypatch.setattr(windows_mod, "_qpa_platform_name", lambda: "offscreen")
    assert windows.uses_window_mask() is True  # tests / CI keep the mask path
    monkeypatch.setattr(windows_mod, "_qpa_platform_name", lambda: "windows")
    assert windows.uses_window_mask() is False  # per-pixel alpha hit-testing (WS_EX_LAYERED)


def test_launch_command_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/Applications/Latkan.app/Contents/MacOS/Latkan")
    assert PlatformIntegration().launch_command() == ["/Applications/Latkan.app/Contents/MacOS/Latkan"]


def test_launch_command_source(source_run: str) -> None:
    assert LinuxPlatform().launch_command() == [source_run, "-m", "latkan"]
    assert MacOSPlatform().launch_command() == [source_run, "-m", "latkan"]


def test_windowless_python_prefers_pythonw(tmp_path: Path) -> None:
    python = tmp_path / "python.exe"
    python.write_text("")
    assert windowless_python(str(python)) == str(python)  # no pythonw.exe yet
    pythonw = tmp_path / "pythonw.exe"
    pythonw.write_text("")
    assert windowless_python(str(python)) == str(pythonw)
    assert windowless_python(str(pythonw)) == str(pythonw)
    assert windowless_python(str(tmp_path / "Latkan.exe")) == str(tmp_path / "Latkan.exe")
    assert windowless_python("") == ""


def test_windows_launch_command_uses_pythonw(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "python.exe").write_text("")
    (tmp_path / "pythonw.exe").write_text("")
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))
    assert WindowsPlatform(registry=FakeRegistry()).launch_command() == [
        str(tmp_path / "pythonw.exe"),
        "-m",
        "latkan",
    ]


# --------------------------------------------------------------------------------------------
# Linux


def test_linux_autostart_round_trip(tmp_path: Path, source_run: str) -> None:
    plat = LinuxPlatform(config_home=tmp_path / "cfg")
    path = tmp_path / "cfg" / "autostart" / "latkan.desktop"
    assert plat.autostart_supported()
    assert plat.autostart_path == path
    assert not plat.autostart_enabled()

    plat.set_autostart(True)
    assert path.is_file()
    assert plat.autostart_enabled()
    entry = _parse_desktop(path)
    assert entry["Type"] == "Application"
    assert entry["Name"] == "Latkan"
    assert entry["Icon"] == "latkan"
    assert entry["Terminal"] == "false"
    assert entry["X-GNOME-Autostart-enabled"] == "true"
    assert entry["Exec"] == desktop_exec(plat.launch_command())
    assert entry["Exec"].startswith('"') and entry["Exec"].endswith(" -m latkan")

    plat.set_autostart(True)  # idempotent rewrite
    assert plat.autostart_enabled()
    assert [p.name for p in path.parent.iterdir()] == ["latkan.desktop"]  # no temp files left

    plat.set_autostart(False)
    assert not path.exists()
    assert not plat.autostart_enabled()
    plat.set_autostart(False)  # disabling twice is fine


@pytest.mark.parametrize(
    "extra",
    ["X-GNOME-Autostart-enabled=false", "Hidden=true", "Hidden=True"],
)
def test_linux_autostart_disabled_by_session_manager(tmp_path: Path, extra: str) -> None:
    plat = LinuxPlatform(config_home=tmp_path)
    plat.autostart_path.parent.mkdir(parents=True)
    plat.autostart_path.write_text(f"[Desktop Entry]\nType=Application\nExec=latkan\n{extra}\n", "utf-8")
    assert not plat.autostart_enabled()


def test_linux_appimage_is_the_command_when_frozen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    appimage = "/home/me/Apps/Latkan x86_64.AppImage"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/tmp/.mount_LatkanXYZ/usr/bin/latkan")
    monkeypatch.setenv("APPIMAGE", appimage)
    plat = LinuxPlatform(config_home=tmp_path)
    assert plat.launch_command() == [appimage]
    plat.set_autostart(True)
    assert _parse_desktop(plat.autostart_path)["Exec"] == '"/home/me/Apps/Latkan x86_64.AppImage"'


def test_linux_inherited_appimage_ignored_for_source_runs(
    monkeypatch: pytest.MonkeyPatch, source_run: str
) -> None:
    monkeypatch.setenv("APPIMAGE", "/opt/SomeIDE.AppImage")
    assert LinuxPlatform().launch_command() == [source_run, "-m", "latkan"]


def test_desktop_exec_escaping() -> None:
    assert desktop_exec(["/usr/bin/python3", "-m", "latkan"]) == "/usr/bin/python3 -m latkan"
    assert desktop_exec(["/opt/My Apps/latkan"]) == '"/opt/My Apps/latkan"'
    assert desktop_exec(["a$b", 'say "hi"', "`x`"]) == '"a\\\\$b" "say \\\\"hi\\\\"" "\\\\`x\\\\`"'
    # literal backslash -> four backslashes (quote escape, then string escape)
    assert desktop_exec(["C:\\x"]) == '"C:\\\\\\\\x"'
    assert desktop_exec(["100%"]) == "100%%"
    assert desktop_exec([""]) == '""'
    assert desktop_exec(["it's"]) == '"it\'s"'


def test_linux_config_home_resolution(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert LinuxPlatform().autostart_path == tmp_path / "xdg" / "autostart" / "latkan.desktop"
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/ignored")  # spec: relative paths are invalid
    assert LinuxPlatform().config_home == tmp_path / "home" / ".config"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert LinuxPlatform().config_home == tmp_path / "home" / ".config"
    assert LinuxPlatform(config_home=tmp_path / "inj").config_home == tmp_path / "inj"


def test_linux_overlay_flags_bypass_wm() -> None:
    flags = LinuxPlatform().overlay_window_flags()
    assert flags & Qt.WindowType.X11BypassWindowManagerHint == Qt.WindowType.X11BypassWindowManagerHint
    assert flags & Qt.WindowType.WindowStaysOnTopHint == Qt.WindowType.WindowStaysOnTopHint
    assert flags & Qt.WindowType.Tool == Qt.WindowType.Tool


def test_linux_overlay_geometry_is_one_pixel_short() -> None:
    # Mutter 50 treats a monitor-sized override-redirect window on top as full-screen and then
    # hides GNOME's top bar and dock; one row short never matches.
    plat = LinuxPlatform()
    assert plat.overlay_geometry(FakeScreen(QRect(0, 0, 1920, 1080))) == QRect(0, 0, 1920, 1079)
    assert plat.overlay_geometry(FakeScreen(QRect(1920, -200, 2560, 1440))) == QRect(1920, -200, 2560, 1439)
    assert plat.overlay_geometry(FakeScreen(QRect(0, 0, 800, 1))) == QRect(0, 0, 800, 1)


class PanelScreen(FakeScreen):
    """A screen whose available area starts ``top`` px lower (a top panel's strut)."""

    def __init__(self, rect: QRect, top: int) -> None:
        super().__init__(rect)
        self._top = top

    def availableGeometry(self) -> QRect:  # noqa: N802 (Qt name)
        return self._rect.adjusted(0, self._top, 0, 0)


@pytest.mark.parametrize(
    ("session", "desktop", "expected"),
    [
        ("x11", "ubuntu:GNOME", 32),  # "Ubuntu on Xorg": the Shell takes presses in the top bar
        ("x11", "GNOME", 32),
        ("x11", "GNOME-Classic:GNOME", 32),
        ("wayland", "ubuntu:GNOME", 0),  # Clutter picks by stacking: the overlay gets the press
        ("x11", "GNOME-Flashback:GNOME", 0),  # no Shell: the panel is a normal dock window
        ("x11", "KDE", 0),
        ("x11", "XFCE", 0),
        ("", "GNOME", 0),
    ],
)
def test_linux_anchor_top_inset_only_for_gnome_shell_on_xorg(
    monkeypatch: pytest.MonkeyPatch, session: str, desktop: str, expected: int
) -> None:
    monkeypatch.setenv("XDG_SESSION_TYPE", session)
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", desktop)
    assert LinuxPlatform().anchor_top_inset(PanelScreen(QRect(0, 0, 1920, 1080), 32)) == expected


def test_linux_anchor_top_inset_is_sane(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "ubuntu:GNOME")
    plat = LinuxPlatform()
    assert plat.anchor_top_inset(PanelScreen(QRect(1920, 0, 1920, 1080), 0)) == 0  # no panel here
    assert plat.anchor_top_inset(PanelScreen(QRect(0, 0, 1920, 1080), -5)) == 0
    assert plat.anchor_top_inset(PanelScreen(QRect(0, 0, 1920, 1080), 900)) == 270  # capped: height/4
    assert PlatformIntegration().anchor_top_inset(PanelScreen(QRect(0, 0, 1920, 1080), 32)) == 0


# --------------------------------------------------------------------------------------------
# Linux: X11 compositor detection, dialog focus


def test_linux_compositing_needs_no_probe_off_x11(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> None:
        raise AssertionError("no X11 probe expected")

    monkeypatch.setattr(linux_mod, "_X11Compositor", boom)
    plat = LinuxPlatform()
    monkeypatch.setattr(linux_mod, "_qpa_platform_name", lambda: "offscreen")
    assert plat.compositing_active() is True
    plat.watch_compositing(lambda: None)
    monkeypatch.setattr(linux_mod, "_qpa_platform_name", lambda: "xcb")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")  # XWayland: the Wayland compositor composites
    assert plat.compositing_active() is True
    plat.watch_compositing(lambda: None)
    plat.watch_compositing(None)
    assert PlatformIntegration().compositing_active() is True
    PlatformIntegration().watch_compositing(lambda: None)


class FakeCompositor:
    def __init__(self, answer: bool | None) -> None:
        self.answer = answer
        self.watched: list[Any] = []

    def active(self) -> bool | None:
        return self.answer

    def watch(self, callback: Any) -> None:
        self.watched.append(callback)


def test_linux_compositing_on_x11_follows_the_probe(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    for var in ("WAYLAND_DISPLAY", "XDG_SESSION_TYPE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(linux_mod, "_qpa_platform_name", lambda: "xcb")
    probe = FakeCompositor(True)
    monkeypatch.setattr(linux_mod, "_X11Compositor", lambda: probe)
    plat = LinuxPlatform()
    assert plat.compositing_active() is True
    probe.answer = None  # cannot tell: keep the translucent mode
    assert plat.compositing_active() is True
    probe.answer = False
    with caplog.at_level(logging.WARNING, logger="latkan.platform.linux"):
        assert plat.compositing_active() is False
        assert plat.compositing_active() is False
    assert sum("compositing window manager" in r.getMessage() for r in caplog.records) == 1  # once

    def callback() -> None:
        pass

    plat.watch_compositing(callback)
    plat.watch_compositing(None)
    assert probe.watched == [callback, None]


class FakeXlib:
    """Just enough of libX11 for _X11Compositor's selection query and event draining."""

    def __init__(self, owners: list[int], queued_after: list[int]) -> None:
        self.owners, self.queued_after = owners, queued_after
        self.pending = 0
        self.queued = 0
        self.log: list[str] = []

    def XPending(self, _display: int) -> int:  # noqa: N802 (Xlib names)
        return self.pending

    def XNextEvent(self, _display: int, _event: Any) -> int:  # noqa: N802
        self.pending -= 1
        self.log.append("event")
        return 0

    def XGetSelectionOwner(self, _display: int, atom: int) -> int:  # noqa: N802
        assert atom == 99
        self.log.append("query")
        self.queued = self.pending = self.queued_after.pop(0)  # read along with the reply
        return self.owners.pop(0)

    def XEventsQueued(self, _display: int, mode: int) -> int:  # noqa: N802
        assert mode == 0  # QueuedAlready: no I/O
        return self.queued


def _probe_with(xlib: FakeXlib) -> Any:
    probe = linux_mod._X11Compositor()
    probe._xlib, probe._display, probe._atom = xlib, 1, 99
    return probe


def test_x11_compositor_probe_asks_again_when_events_arrived_with_the_reply() -> None:
    xlib = FakeXlib(owners=[0x400001, 0], queued_after=[1, 0])
    assert _probe_with(xlib).active() is False  # the second answer wins
    assert xlib.log == ["query", "event", "query"]

    assert _probe_with(FakeXlib(owners=[0x400001], queued_after=[0])).active() is True

    class Broken(FakeXlib):
        def XGetSelectionOwner(self, _display: int, _atom: int) -> int:  # noqa: N802
            raise OSError("connection lost")

    assert _probe_with(Broken([], [])).active() is None


def test_x11_compositor_probe_unavailable_is_cannot_tell(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_library(_ctypes: Any, _name: str, soname: str) -> Any:
        raise OSError(f"{soname}: cannot open shared object file")

    monkeypatch.setattr(linux_mod, "_load_library", no_library)
    probe = linux_mod._X11Compositor()
    assert probe.active() is None
    probe.watch(lambda: None)  # nothing to watch with; never raises
    probe.watch(None)


def test_x11_compositor_events_are_drained_and_reported(caplog: pytest.LogCaptureFixture) -> None:
    xlib = FakeXlib(owners=[], queued_after=[])
    xlib.pending = 2
    probe = _probe_with(xlib)
    calls: list[int] = []
    probe._callback = lambda: calls.append(xlib.pending)
    probe._on_readable()
    assert calls == [0] and xlib.log == ["event", "event"]

    def broken() -> None:
        raise RuntimeError("handler bug")

    probe._callback = broken
    with caplog.at_level(logging.ERROR, logger="latkan.platform.linux"):
        probe._on_readable()  # runs from a QSocketNotifier: must never raise into Qt
    assert "handler failed" in caplog.text


@pytest.fixture
def qt_gui_app() -> Any:
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_linux_activate_app_refreshes_the_x_server_time(
    monkeypatch: pytest.MonkeyPatch, qt_gui_app: Any
) -> None:
    from PySide6.QtGui import QGuiApplication

    plat = LinuxPlatform()
    monkeypatch.setattr(linux_mod, "_qpa_platform_name", lambda: "offscreen")
    plat.activate_app()
    assert plat._time_window is None  # only X11 needs it

    syncs: list[str] = []
    monkeypatch.setattr(linux_mod, "_qpa_platform_name", lambda: "xcb")
    monkeypatch.setattr(QGuiApplication, "sync", staticmethod(lambda: syncs.append("sync")))
    plat.activate_app()
    window = plat._time_window
    assert window is not None and window.winId()  # a native window ...
    assert not window.isVisible()  # ... that is never shown
    first = window.title()
    plat.activate_app()
    assert plat._time_window is window
    assert window.title() != first  # every call is a real property change (PropertyNotify)
    assert syncs == ["sync"] * 4  # round trips so Qt handles the event before the dialog maps
    assert not window.isVisible()


def test_linux_activate_app_never_raises(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from PySide6.QtGui import QGuiApplication

    def broken() -> None:
        raise RuntimeError("no connection")

    monkeypatch.setattr(linux_mod, "_qpa_platform_name", lambda: "xcb")
    monkeypatch.setattr(QGuiApplication, "sync", staticmethod(broken))
    plat = LinuxPlatform()
    plat._time_window = SimpleNamespace(setTitle=lambda _title: None)
    with caplog.at_level(logging.WARNING, logger="latkan.platform.linux"):
        plat.activate_app()
    assert "timestamp" in caplog.text


def test_dialogs_stay_on_top_only_on_x11(monkeypatch: pytest.MonkeyPatch) -> None:
    assert PlatformIntegration().dialogs_stay_on_top() is False
    assert WindowsPlatform(registry=FakeRegistry()).dialogs_stay_on_top() is False
    assert MacOSPlatform().dialogs_stay_on_top() is False
    monkeypatch.setattr(linux_mod, "_qpa_platform_name", lambda: "xcb")
    assert LinuxPlatform().dialogs_stay_on_top() is True
    monkeypatch.setattr(linux_mod, "_qpa_platform_name", lambda: "wayland")
    assert LinuxPlatform().dialogs_stay_on_top() is False


# --------------------------------------------------------------------------------------------
# Linux prepare_environment


@pytest.fixture
def clean_display_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for var in ("WAYLAND_DISPLAY", "DISPLAY", "QT_QPA_PLATFORM"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def test_prepare_env_wayland_with_xwayland_forces_xcb(clean_display_env: pytest.MonkeyPatch) -> None:
    clean_display_env.setenv("WAYLAND_DISPLAY", "wayland-0")
    clean_display_env.setenv("DISPLAY", ":0")
    LinuxPlatform().prepare_environment()
    assert os.environ["QT_QPA_PLATFORM"].split(";")[0] == "xcb"


def test_prepare_env_respects_explicit_platform(clean_display_env: pytest.MonkeyPatch) -> None:
    clean_display_env.setenv("WAYLAND_DISPLAY", "wayland-0")
    clean_display_env.setenv("DISPLAY", ":0")
    clean_display_env.setenv("QT_QPA_PLATFORM", "wayland")
    LinuxPlatform().prepare_environment()
    assert os.environ["QT_QPA_PLATFORM"] == "wayland"


def test_prepare_env_wayland_without_display_warns(
    clean_display_env: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    clean_display_env.setenv("WAYLAND_DISPLAY", "wayland-0")
    with caplog.at_level(logging.WARNING, logger="latkan.platform.linux"):
        LinuxPlatform().prepare_environment()
    assert "QT_QPA_PLATFORM" not in os.environ
    assert any("XWayland" in rec.getMessage() for rec in caplog.records)


def test_prepare_env_plain_x11_untouched(clean_display_env: pytest.MonkeyPatch) -> None:
    clean_display_env.setenv("DISPLAY", ":0")
    LinuxPlatform().prepare_environment()
    assert "QT_QPA_PLATFORM" not in os.environ


# --------------------------------------------------------------------------------------------
# macOS


def test_macos_autostart_round_trip(tmp_path: Path, source_run: str) -> None:
    plat = MacOSPlatform(home=tmp_path)
    path = tmp_path / "Library" / "LaunchAgents" / "in.latkan.app.plist"
    assert plat.autostart_supported()
    assert plat.launch_agent_path == path
    assert not plat.autostart_enabled()

    plat.set_autostart(True)
    assert plat.autostart_enabled()
    with path.open("rb") as fh:
        agent = plistlib.load(fh)
    assert agent["Label"] == "in.latkan.app"
    # --autostart: a login start keeps hidden charms hidden (a manual macOS start shows them)
    assert plat.launch_command() == [source_run, "-m", "latkan"]
    assert agent["ProgramArguments"] == [source_run, "-m", "latkan", "--autostart"]
    assert agent["RunAtLoad"] is True
    assert agent["KeepAlive"] is False
    assert agent["ProcessType"] == "Interactive"

    plat.set_autostart(False)
    assert not path.exists()
    assert not plat.autostart_enabled()
    plat.set_autostart(False)


def test_macos_frozen_app_program_arguments(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    exe = "/Applications/Latkan.app/Contents/MacOS/Latkan"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", exe)
    plat = MacOSPlatform(home=tmp_path)
    plat.set_autostart(True)
    with plat.launch_agent_path.open("rb") as fh:
        assert plistlib.load(fh)["ProgramArguments"] == [exe, "--autostart"]


@pytest.mark.parametrize(
    "exe",
    [
        "/private/var/folders/xy/T/AppTranslocation/0A1B2C3D/d/Latkan.app/Contents/MacOS/Latkan",
        "/Volumes/Latkan/Latkan.app/Contents/MacOS/Latkan",  # a mounted DMG (read-only volume)
    ],
)
def test_macos_autostart_refuses_paths_gone_after_reboot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, exe: str
) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", exe)
    read_only = exe.startswith("/Volumes/Latkan/")
    monkeypatch.setattr(
        os, "statvfs", lambda _path: SimpleNamespace(f_flag=1 if read_only else 0), raising=False
    )
    monkeypatch.setattr(os, "ST_RDONLY", 1, raising=False)
    plat = MacOSPlatform(home=tmp_path)
    with pytest.raises(OSError, match="Move Latkan to the Applications folder first"):
        plat.set_autostart(True)
    assert not plat.launch_agent_path.exists()
    assert not plat.autostart_enabled()


def test_macos_autostart_allows_external_drives_and_source_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source_run: str
) -> None:
    monkeypatch.setattr(os, "statvfs", lambda _path: SimpleNamespace(f_flag=1), raising=False)
    monkeypatch.setattr(os, "ST_RDONLY", 1, raising=False)
    plat = MacOSPlatform(home=tmp_path)
    plat.set_autostart(True)  # source runs are never refused (there is no bundle to move)
    assert plat.autostart_enabled()

    exe = "/Volumes/External/Apps/Latkan.app/Contents/MacOS/Latkan"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", exe)
    monkeypatch.setattr(os, "statvfs", lambda _path: SimpleNamespace(f_flag=0), raising=False)
    plat.set_autostart(True)  # a writable external drive is a legitimate place for an app
    with plat.launch_agent_path.open("rb") as fh:
        assert plistlib.load(fh)["ProgramArguments"] == [exe, "--autostart"]


def test_macos_autostart_is_off_when_the_program_is_gone(tmp_path: Path, source_run: str) -> None:
    plat = MacOSPlatform(home=tmp_path)
    plat.set_autostart(True)
    assert plat.autostart_enabled()
    Path(source_run).unlink()  # the app was moved, renamed, or the DMG it ran from was ejected
    assert not plat.autostart_enabled()
    agent = {"Label": "in.latkan.app", "ProgramArguments": [str(tmp_path / "gone" / "Latkan")]}
    plat.launch_agent_path.write_bytes(plistlib.dumps(agent))
    assert not plat.autostart_enabled()


def test_macos_corrupt_plist_is_off_and_repairable(tmp_path: Path, source_run: str) -> None:
    plat = MacOSPlatform(home=tmp_path)
    plat.launch_agent_path.parent.mkdir(parents=True)
    plat.launch_agent_path.write_bytes(b"\x00 definitely not a plist")
    assert not plat.autostart_enabled()
    plat.set_autostart(True)
    assert plat.autostart_enabled()


def test_macos_tray_mask_and_flags() -> None:
    plat = MacOSPlatform()
    assert plat.tray_icon_is_mask() is True
    assert plat.overlay_window_flags() == PlatformIntegration().overlay_window_flags()


class _RecordingWidget:
    def __init__(self, win_id: int = 0x1234) -> None:
        self.attributes: list[tuple[Any, bool]] = []
        self._win_id = win_id

    def setAttribute(self, attr: Any, on: bool) -> None:
        self.attributes.append((attr, on))

    def winId(self) -> int:
        return self._win_id


def test_macos_configure_without_pyobjc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(macos_mod, "_load_pyobjc", lambda: None)
    plat = MacOSPlatform()
    widget = _RecordingWidget()
    plat.configure_application(object())
    plat.configure_overlay(widget)
    assert widget.attributes == [(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow, True)]


class FakeNSPanel:
    """Stands in for AppKit.NSPanel (only used for isKindOfClass_)."""


class FakeNSWindow:
    """Records the native calls the overlay configuration makes."""

    def __init__(self, *, panel: bool = True, private_setter: bool = True, number: int = 7) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.panel = panel
        self.private_setter = private_setter
        self.style = 0x1 << 4  # NSWindowStyleMaskUtilityWindow, say
        self.number = number
        self.ignores = False

    def setLevel_(self, value: int) -> None:
        self.calls.append(("setLevel_", value))

    def setCollectionBehavior_(self, value: int) -> None:
        self.calls.append(("setCollectionBehavior_", value))

    def setHidesOnDeactivate_(self, value: bool) -> None:
        self.calls.append(("setHidesOnDeactivate_", value))

    def setHasShadow_(self, value: bool) -> None:
        self.calls.append(("setHasShadow_", value))

    def isKindOfClass_(self, cls: Any) -> bool:
        return self.panel and cls is FakeNSPanel

    def styleMask(self) -> int:
        return self.style

    def setStyleMask_(self, value: int) -> None:
        self.calls.append(("setStyleMask_", value))
        self.style = value

    def respondsToSelector_(self, selector: str) -> bool:
        return self.private_setter and selector == "_setPreventsActivation:"

    def _setPreventsActivation_(self, value: bool) -> None:
        self.calls.append(("_setPreventsActivation_", value))

    def windowNumber(self) -> int:
        return self.number

    def ignoresMouseEvents(self) -> bool:
        return self.ignores

    def setIgnoresMouseEvents_(self, value: bool) -> None:
        self.calls.append(("setIgnoresMouseEvents_", value))
        self.ignores = value


def _fake_pyobjc(window: FakeNSWindow, calls: list[tuple[str, Any]]) -> macos_mod._PyObjC:
    class FakeNSView:
        def window(self) -> FakeNSWindow:
            return window

    def objc_object(*, c_void_p: int) -> FakeNSView:
        calls.append(("objc_object", c_void_p))
        return FakeNSView()

    ns_app = SimpleNamespace(setActivationPolicy_=lambda policy: calls.append(("policy", policy)))
    return macos_mod._PyObjC(
        AppKit=SimpleNamespace(
            NSApplication=SimpleNamespace(sharedApplication=lambda: ns_app), NSPanel=FakeNSPanel
        ),
        objc=SimpleNamespace(objc_object=objc_object),
    )


def test_macos_configure_with_fake_pyobjc(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, Any]] = []
    window = FakeNSWindow()
    monkeypatch.setattr(macos_mod, "_load_pyobjc", lambda: _fake_pyobjc(window, calls))
    monkeypatch.setattr(macos_mod, "_qpa_platform_name", lambda: "cocoa")

    plat = MacOSPlatform()
    plat.configure_application(object())
    plat.configure_overlay(_RecordingWidget(win_id=0xBEEF))
    assert calls == [("policy", 1), ("objc_object", 0xBEEF)]
    assert window.calls == [
        ("setLevel_", 25),
        ("setCollectionBehavior_", (1 << 0) | (1 << 4) | (1 << 6) | (1 << 8)),
        ("setHidesOnDeactivate_", False),
        ("setHasShadow_", False),
        # non-activating panel: clicking a charm must not take focus from the frontmost app
        ("setStyleMask_", (1 << 4) | (1 << 7)),
        ("_setPreventsActivation_", True),
    ]
    # This widget cannot report click targets (no track_pointer), so input is never made
    # explicit: ignoresMouseEvents = NO would make the full-screen overlay swallow every click.
    assert plat._tracker is None


def test_macos_non_activating_panel_is_best_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, Any]] = []
    monkeypatch.setattr(macos_mod, "_qpa_platform_name", lambda: "cocoa")

    old_appkit = FakeNSWindow(private_setter=False)  # no private selector: style mask only
    monkeypatch.setattr(macos_mod, "_load_pyobjc", lambda: _fake_pyobjc(old_appkit, calls))
    MacOSPlatform().configure_overlay(_RecordingWidget())
    assert ("setStyleMask_", (1 << 4) | (1 << 7)) in old_appkit.calls
    assert not any(name == "_setPreventsActivation_" for name, _ in old_appkit.calls)

    not_a_panel = FakeNSWindow(panel=False)  # the bit is only valid on an NSPanel
    monkeypatch.setattr(macos_mod, "_load_pyobjc", lambda: _fake_pyobjc(not_a_panel, calls))
    MacOSPlatform().configure_overlay(_RecordingWidget())
    assert [name for name, _ in not_a_panel.calls] == [
        "setLevel_",
        "setCollectionBehavior_",
        "setHidesOnDeactivate_",
        "setHasShadow_",
    ]


class FakeMacScreen:
    def __init__(self, geometry: QRect, available: QRect) -> None:
        self._geometry, self._available = geometry, available

    def geometry(self) -> QRect:
        return QRect(self._geometry)

    def availableGeometry(self) -> QRect:
        return QRect(self._available)


def test_macos_overlay_starts_below_the_menu_bar() -> None:
    plat = MacOSPlatform()
    # notched MacBook: 37 pt menu bar, Dock at the bottom (excluded from availableGeometry)
    screen = FakeMacScreen(QRect(0, 0, 1512, 982), QRect(0, 37, 1512, 880))
    assert plat.overlay_geometry(screen) == QRect(0, 37, 1512, 945)  # still down to the bottom
    # a secondary display left of the main one, Dock on its left side
    screen = FakeMacScreen(QRect(-1920, 0, 1920, 1080), QRect(-1856, 25, 1856, 1055))
    assert plat.overlay_geometry(screen) == QRect(-1920, 25, 1920, 1055)
    # auto-hiding menu bar: nothing to leave out
    screen = FakeMacScreen(QRect(0, 0, 1440, 900), QRect(0, 0, 1440, 900))
    assert plat.overlay_geometry(screen) == QRect(0, 0, 1440, 900)
    # nonsense from the OS never produces an empty overlay
    screen = FakeMacScreen(QRect(0, 0, 1440, 900), QRect(0, 900, 1440, 0))
    assert plat.overlay_geometry(screen) == QRect(0, 0, 1440, 900)


def test_macos_configure_overlay_native_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(**_kwargs: Any) -> None:
        raise ValueError("bad pointer")

    fake = macos_mod._PyObjC(AppKit=SimpleNamespace(), objc=SimpleNamespace(objc_object=boom))
    monkeypatch.setattr(macos_mod, "_load_pyobjc", lambda: fake)
    monkeypatch.setattr(macos_mod, "_qpa_platform_name", lambda: "cocoa")
    MacOSPlatform().configure_overlay(_RecordingWidget())
    MacOSPlatform().configure_application(object())  # AppKit.NSApplication missing -> logged


def test_macos_configure_overlay_skips_native_calls_on_headless_qpa(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(**_kwargs: Any) -> None:
        raise AssertionError("fake offscreen window ids must never be wrapped as NSViews")

    fake = macos_mod._PyObjC(AppKit=SimpleNamespace(), objc=SimpleNamespace(objc_object=forbidden))
    monkeypatch.setattr(macos_mod, "_load_pyobjc", lambda: fake)
    monkeypatch.setattr(macos_mod, "_qpa_platform_name", lambda: "offscreen")
    widget = _RecordingWidget()
    MacOSPlatform().configure_overlay(widget)
    assert widget.attributes == [(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow, True)]


# --------------------------------------------------------------------------------------------
# Windows


def test_windows_autostart_round_trip(source_run: str) -> None:
    reg = FakeRegistry()
    plat = WindowsPlatform(registry=reg)
    assert plat.autostart_supported()
    assert not plat.autostart_enabled()

    plat.set_autostart(True)
    assert plat.autostart_enabled()
    value = reg.values[(RUN_KEY, "Latkan")]
    assert value == subprocess.list2cmdline(plat.launch_command())
    assert value.startswith('"') and value.endswith(" -m latkan")  # path with a space is quoted

    plat.set_autostart(False)
    assert (RUN_KEY, "Latkan") not in reg.values
    assert not plat.autostart_enabled()
    plat.set_autostart(False)


def test_windows_frozen_command_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Program Files\Latkan\Latkan.exe")
    reg = FakeRegistry()
    WindowsPlatform(registry=reg).set_autostart(True)
    assert reg.values[(RUN_KEY, "Latkan")] == '"C:\\Program Files\\Latkan\\Latkan.exe"'


def test_windows_task_manager_disabled_entry(source_run: str) -> None:
    reg = FakeRegistry()
    plat = WindowsPlatform(registry=reg)
    plat.set_autostart(True)
    reg.values[(STARTUP_APPROVED_KEY, "Latkan")] = b"\x03\x00\x00\x00" + bytes(8)  # Task Manager: off
    assert not plat.autostart_enabled()
    plat.set_autostart(True)  # re-enabling from Latkan clears the override
    assert (STARTUP_APPROVED_KEY, "Latkan") not in reg.values
    assert plat.autostart_enabled()
    reg.values[(STARTUP_APPROVED_KEY, "Latkan")] = b"\x02\x00\x00\x00" + bytes(8)  # Task Manager: on
    assert plat.autostart_enabled()


def test_windows_registry_errors(source_run: str) -> None:
    reg = FakeRegistry()
    plat = WindowsPlatform(registry=reg)
    reg.fail_set = True
    with pytest.raises(OSError):
        plat.set_autostart(True)
    reg.fail_set = False
    plat.set_autostart(True)
    reg.fail_get = True
    assert plat.autostart_enabled() is False  # read failure never raises


def _iss_sections(text: str) -> dict[str, list[str]]:
    """Directive/entry lines of an Inno Setup script per lower-cased section (comments and
    preprocessor lines dropped). Stops at [Code], which is Pascal, not entries."""
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith((";", "#")):
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip().lower()
            if name == "code":
                break
            current = sections.setdefault(name, [])
        elif current is not None:
            current.append(line)
    return sections


def test_windows_installer_upgrade_keeps_the_users_autostart_choice() -> None:
    iss = Path(__file__).resolve().parents[1] / "packaging" / "windows" / "latkan.iss"
    text = iss.read_text(encoding="utf-8")
    sections = _iss_sections(text)

    # Tasks must not be pre-selected from the previous install: a silent upgrade would otherwise
    # rewrite Run\Latkan and delete the Task Manager "disabled" flag the user set since.
    setup = {
        key.strip().lower(): value.strip().lower()
        for key, value in (line.split("=", 1) for line in sections["setup"] if "=" in line)
    }
    assert setup.get("useprevioustasks") in {"no", "false", "0"}

    # Every registry write is tied to the opt-in task, and targets what windows.py manages.
    assert sections["registry"]
    assert all("Tasks: autostart" in line for line in sections["registry"])
    defines = dict(re.findall(r'^#define\s+(\w+)\s+"([^"]*)"', text, flags=re.MULTILINE))
    assert defines["RunKey"] == RUN_KEY
    assert defines["ApprovedKey"] == STARTUP_APPROVED_KEY
    assert defines["RunValue"] == AUTOSTART_VALUE


def test_windows_overlay_geometry_is_one_pixel_short() -> None:
    plat = WindowsPlatform(registry=FakeRegistry())
    assert plat.overlay_geometry(FakeScreen(QRect(0, 0, 1920, 1080))) == QRect(0, 0, 1920, 1079)
    assert plat.overlay_geometry(FakeScreen(QRect(1920, -200, 2560, 1440))) == QRect(1920, -200, 2560, 1439)


@pytest.mark.parametrize("win_id", [0, 0x7FFFFFF0])
def test_windows_configure_overlay_never_raises(win_id: int) -> None:
    # 0 -> rejected up front; 0x7FFFFFF0 -> an invalid HWND the real user32 calls reject (Windows).
    WindowsPlatform(registry=FakeRegistry()).configure_overlay(_RecordingWidget(win_id=win_id))

    class Broken:
        def winId(self) -> int:
            raise RuntimeError("widget already deleted")

    WindowsPlatform(registry=FakeRegistry()).configure_overlay(Broken())


@pytest.mark.parametrize("win_id", [0, 0x7FFFFFF0])
def test_windows_configure_overlay_native_path_never_raises(
    monkeypatch: pytest.MonkeyPatch, win_id: int
) -> None:
    # Force the native branch (the suite runs on the offscreen QPA, which it normally skips).
    monkeypatch.setattr(windows_mod, "_qpa_platform_name", lambda: "windows")
    WindowsPlatform(registry=FakeRegistry()).configure_overlay(_RecordingWidget(win_id=win_id))


def test_windows_configure_overlay_skips_non_native_qpa(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(windows_mod, "_qpa_platform_name", lambda: "offscreen")
    plat = WindowsPlatform(registry=FakeRegistry())
    applied: list[int] = []
    monkeypatch.setattr(plat, "_apply_ex_style", lambda hwnd, add: applied.append(hwnd))
    plat.configure_overlay(_RecordingWidget(win_id=0x1))  # offscreen hands out fake window ids
    assert applied == []
    monkeypatch.setattr(windows_mod, "_qpa_platform_name", lambda: "windows")
    plat.configure_overlay(_RecordingWidget(win_id=0x1))
    assert applied == [0x1]


def test_foreground_activates_and_flags_the_block() -> None:
    class Recording(PlatformIntegration):
        def __init__(self) -> None:
            self.seen: list[bool] = []

        def activate_app(self) -> None:
            self.seen.append(self.in_foreground)  # already flagged while activating

    plat = Recording()
    assert not plat.in_foreground
    with plat.foreground():
        assert plat.in_foreground
        with plat.foreground():  # a dialog opened from a menu
            assert plat.in_foreground
        assert plat.in_foreground
    assert not plat.in_foreground
    assert plat.seen == [True, True]
    assert not PlatformIntegration().in_foreground  # the count is per instance

    with pytest.raises(RuntimeError), plat.foreground():
        raise RuntimeError("dialog failed")
    assert not plat.in_foreground


def test_foreground_survives_a_failing_activation(caplog: pytest.LogCaptureFixture) -> None:
    class Broken(PlatformIntegration):
        def activate_app(self) -> None:
            raise ValueError("no NSApp")

    plat = Broken()
    ran = []
    with caplog.at_level(logging.WARNING), plat.foreground():
        ran.append(plat.in_foreground)
    assert ran == [True] and not plat.in_foreground
    assert "front" in caplog.text


def test_activate_app_is_safe_everywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    PlatformIntegration().activate_app()
    WindowsPlatform(registry=FakeRegistry()).activate_app()
    LinuxPlatform().activate_app()
    monkeypatch.setattr(macos_mod, "_load_pyobjc", lambda: None)
    MacOSPlatform().activate_app()  # no PyObjC: warns once, does nothing


def test_macos_activate_app_with_fake_pyobjc(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    ns_app = SimpleNamespace(activateIgnoringOtherApps_=calls.append)
    fake = macos_mod._PyObjC(
        AppKit=SimpleNamespace(NSApplication=SimpleNamespace(sharedApplication=lambda: ns_app)),
        objc=SimpleNamespace(),
    )
    monkeypatch.setattr(macos_mod, "_load_pyobjc", lambda: fake)
    MacOSPlatform().activate_app()
    assert calls == [True]

    broken = macos_mod._PyObjC(AppKit=SimpleNamespace(), objc=SimpleNamespace())
    monkeypatch.setattr(macos_mod, "_load_pyobjc", lambda: broken)
    MacOSPlatform().activate_app()  # AppKit.NSApplication missing -> logged, never raised


def test_windows_real_registry_backend_is_importable_everywhere() -> None:
    plat = WindowsPlatform()
    assert plat.autostart_supported() == sys.platform.startswith("win")


# --------------------------------------------------------------------------------------------
# get_platform


@pytest.mark.parametrize(
    ("platform_id", "cls", "name"),
    [
        ("win32", WindowsPlatform, "windows"),
        ("darwin", MacOSPlatform, "macos"),
        ("linux", LinuxPlatform, "linux"),
        ("freebsd14", LinuxPlatform, "linux"),
    ],
)
def test_get_platform_dispatch(
    monkeypatch: pytest.MonkeyPatch, clean_platform_cache: None, platform_id: str, cls: type, name: str
) -> None:
    monkeypatch.setattr(sys, "platform", platform_id)
    plat = get_platform()
    assert type(plat) is cls
    assert plat.name == name
    assert get_platform() is plat  # cached singleton


def test_get_platform_cache_clear(monkeypatch: pytest.MonkeyPatch, clean_platform_cache: None) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    first = get_platform()
    platform_pkg.clear_cache()
    assert get_platform() is not first
