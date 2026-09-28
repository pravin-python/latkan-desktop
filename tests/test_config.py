"""Tests for settings paths, validation, atomic persistence and legacy migration (no Qt)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from latkan import config
from latkan.config import (
    BUILTIN_NAMES,
    ROPE_STYLES,
    SCHEMA_VERSION,
    CharmSpec,
    Settings,
    backup_settings,
    charms_dir,
    config_dir,
    load_settings,
    log_dir,
    save_settings,
    settings_path,
)


@pytest.fixture(autouse=True)
def latkan_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test gets its own config home; nothing touches the real user profile."""
    home = tmp_path / "latkan-home"
    monkeypatch.setenv("LATKAN_HOME", str(home))
    monkeypatch.setattr(config, "_unreadable", set())
    return home


def write_json(path: Path, data: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def make_image(name: str = "charm.png", content: bytes = b"\x89PNG fake") -> str:
    (charms_dir() / name).write_bytes(content)
    return name


def v1_payload(**overrides: object) -> dict:
    data = {
        "version": SCHEMA_VERSION,
        "rope_style": "neon",
        "scale": 1.3,
        "breeze": True,
        "hover_swing": False,
        "hidden": True,
        "charms": [
            {
                "id": "a" * 32,
                "kind": "builtin",
                "name": "heart",
                "image": None,
                "x_frac": 0.25,
                "length": 200.0,
                "screen": "DP-1",
            }
        ],
    }
    data.update(overrides)
    return data


# --------------------------------------------------------------------------- paths


def test_latkan_home_overrides_everything(latkan_home: Path):
    assert config_dir() == latkan_home
    assert settings_path() == latkan_home / "settings.json"
    assert charms_dir() == latkan_home / "charms"
    assert log_dir() == latkan_home / "logs"
    assert charms_dir().is_dir() and log_dir().is_dir()  # created on demand


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.delenv("LATKAN_HOME", raising=False)
    monkeypatch.setattr(config.Path, "home", staticmethod(lambda: home))
    return home


def test_config_dir_windows(fake_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "platform", "win32")
    appdata = tmp_path / "Roaming"
    monkeypatch.setenv("APPDATA", str(appdata))
    assert config_dir() == appdata / "Latkan"
    monkeypatch.delenv("APPDATA")
    assert config_dir() == fake_home / "AppData" / "Roaming" / "Latkan"


def test_config_dir_macos(fake_home: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/ignored/on/macos")
    assert config_dir() == fake_home / "Library" / "Application Support" / "Latkan"


def test_config_dir_linux(fake_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "platform", "linux")
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    assert config_dir() == xdg / "latkan"
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/dir")  # invalid per XDG spec -> ignored
    assert config_dir() == fake_home / ".config" / "latkan"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert config_dir() == fake_home / ".config" / "latkan"


def test_config_dir_does_not_create_anything(fake_home: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    config_dir()
    settings_path()
    assert not fake_home.exists()


# --------------------------------------------------------------------------- model


def test_default_settings():
    s = Settings.default()
    assert s.version == SCHEMA_VERSION
    assert (s.rope_style, s.scale, s.breeze, s.hover_swing, s.hidden) == ("gold", 1.0, False, True, False)
    assert [(c.kind, c.name, c.x_frac, c.length) for c in s.charms] == [
        ("builtin", "nazar", 0.72, 170.0),
        ("builtin", "star", 0.80, 110.0),
    ]
    assert len({c.id for c in s.charms}) == 2
    assert Settings.default().charms[0].id != s.charms[0].id  # fresh ids each time


def test_charm_factories():
    b = CharmSpec.new_builtin("moon", 0.3, 120.0, screen="HDMI-1")
    assert (b.kind, b.name, b.image, b.screen) == ("builtin", "moon", None, "HDMI-1")
    assert len(b.id) == 32 and int(b.id, 16) >= 0
    img = CharmSpec.new_image("abc.png", 2.0, 1.0)
    assert (img.kind, img.name, img.image) == ("image", None, "abc.png")
    assert (img.x_frac, img.length) == (1.0, 40.0)  # clamped
    assert img.id != b.id
    with pytest.raises(ValueError):
        CharmSpec.new_builtin("unicorn", 0.5, 100.0)
    with pytest.raises(ValueError):
        CharmSpec.new_image("/abs/path/abc.png", 0.5, 100.0)
    with pytest.raises(ValueError):
        CharmSpec.new_image("../abc.png", 0.5, 100.0)


def test_constants_match_contract():
    assert ROPE_STYLES == ("gold", "silver", "neon", "kalava")
    assert config.SCALES == (0.6, 0.8, 1.0, 1.3, 1.6)
    assert BUILTIN_NAMES == ("nazar", "heart", "star", "moon", "clover", "dreamcatcher")


# --------------------------------------------------------------------------- load / save


def test_missing_file_returns_defaults_without_writing():
    s = load_settings()
    assert [c.name for c in s.charms] == ["nazar", "star"]
    assert not settings_path().exists()


def test_round_trip():
    image = make_image("mine.png")
    s = Settings(
        rope_style="kalava",
        scale=1.6,
        breeze=True,
        hover_swing=False,
        hidden=True,
        charms=[
            CharmSpec.new_builtin("clover", 0.1, 90.0, screen="eDP-1"),
            CharmSpec.new_image(image, 0.9, 420.5),
        ],
    )
    save_settings(s)
    assert settings_path().is_file()
    assert load_settings() == s
    on_disk = json.loads(settings_path().read_text(encoding="utf-8"))
    assert on_disk["version"] == SCHEMA_VERSION
    assert on_disk["charms"][1]["image"] == "mine.png"


def test_save_to_explicit_path_creates_directories(tmp_path: Path):
    target = tmp_path / "a" / "b" / "custom.json"
    s = Settings.default()
    save_settings(s, target)
    assert load_settings(target) == s


def test_save_is_atomic_and_leaves_no_temp_files():
    save_settings(Settings.default())
    save_settings(Settings(rope_style="silver"))
    assert load_settings().rope_style == "silver"
    assert sorted(p.name for p in config_dir().iterdir() if p.is_file()) == ["settings.json"]


def test_failed_save_keeps_previous_file(monkeypatch: pytest.MonkeyPatch):
    save_settings(Settings(rope_style="silver", charms=[]))
    before = settings_path().read_bytes()

    def broken_replace(src, dst):
        raise OSError("disk on fire")

    monkeypatch.setattr(config.os, "replace", broken_replace)
    with pytest.raises(OSError):
        save_settings(Settings(rope_style="neon"))
    assert settings_path().read_bytes() == before
    assert sorted(p.name for p in config_dir().iterdir() if p.is_file()) == ["settings.json"]


def test_save_writes_strict_json_even_with_bad_floats():
    s = Settings(scale=float("nan"), charms=[CharmSpec.new_builtin("star", 0.5, 100.0)])
    s.charms[0].x_frac = float("inf")
    save_settings(s)
    data = json.loads(settings_path().read_text(encoding="utf-8"), parse_constant=pytest.fail)
    assert data["scale"] == 1.0
    assert data["charms"][0]["x_frac"] == 0.5


@pytest.mark.parametrize(
    "content",
    [
        b"{not json",
        b"",
        b"[1, 2, 3]",
        b'"just a string"',
        b"\xff\xfe\x00garbage",
        b'{"version": 1, "charms": "oops"}',
        b"[" * 100_000,
    ],
    ids=["syntax", "empty", "list", "string", "binary", "bad-charms", "deep-nesting"],
)
def test_corrupt_file_is_backed_up_and_defaults_returned(content: bytes):
    settings_path().parent.mkdir(parents=True, exist_ok=True)
    settings_path().write_bytes(content)
    s = load_settings()
    assert [c.name for c in s.charms] == ["nazar", "star"]
    backup = settings_path().with_name("settings.json.bak-1")
    assert backup.read_bytes() == content
    assert not settings_path().exists()


def test_backups_are_numbered():
    for n in (1, 2, 3):
        settings_path().parent.mkdir(parents=True, exist_ok=True)
        settings_path().write_text(f"broken {n}", encoding="utf-8")
        load_settings()
        backup = settings_path().with_name(f"settings.json.bak-{n}")
        assert backup.read_text(encoding="utf-8") == f"broken {n}"


def test_utf8_bom_is_accepted():
    settings_path().parent.mkdir(parents=True, exist_ok=True)
    settings_path().write_bytes(b"\xef\xbb\xbf" + json.dumps(v1_payload()).encode("utf-8"))
    assert load_settings().rope_style == "neon"


def test_unreadable_path_returns_defaults_without_backup():
    settings_path().mkdir(parents=True)  # a directory where the file should be
    s = load_settings()
    assert [c.name for c in s.charms] == ["nazar", "star"]
    assert settings_path().is_dir()
    assert not settings_path().with_name("settings.json.bak-1").exists()


def _six_charms() -> Settings:
    charms = [CharmSpec.new_builtin(n, 0.1 * (i + 1), 200.0) for i, n in enumerate(BUILTIN_NAMES)]
    return Settings(rope_style="neon", charms=charms)


def raw(path: Path) -> bytes:
    """The file's bytes, read without ``Path.read_bytes`` (which the tests below make fail)."""
    with open(path, "rb") as handle:
        return handle.read()


def _deny_reads(monkeypatch: pytest.MonkeyPatch, path: Path, times: int | None = None) -> list[int]:
    """Make ``path.read_bytes()`` raise PermissionError (``times`` times, or always)."""
    original = Path.read_bytes
    denied: list[int] = []

    def read_bytes(self: Path) -> bytes:
        if self == path and (times is None or len(denied) < times):
            denied.append(1)
            raise PermissionError(13, "Permission denied", str(self))
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    return denied


def test_unreadable_file_is_kept_as_a_backup_before_the_first_save(monkeypatch: pytest.MonkeyPatch):
    """A file that fails to read (lock, permissions) must never be silently replaced by defaults."""
    save_settings(_six_charms())
    original = settings_path().read_bytes()
    monkeypatch.setattr(config.time, "sleep", lambda _s: None)  # Windows retries the read first
    _deny_reads(monkeypatch, settings_path())

    loaded = load_settings()
    assert [c.name for c in loaded.charms] == ["nazar", "star"]  # runs on defaults meanwhile
    assert raw(settings_path()) == original  # left alone, not moved aside

    save_settings(loaded)  # e.g. the first change in that session (or quitting after one)
    backup = settings_path().with_name("settings.json.bak-1")
    assert backup.read_bytes() == original  # the user's charms survive
    assert json.loads(settings_path().read_text(encoding="utf-8"))["rope_style"] == "gold"
    save_settings(loaded)  # only the first save keeps a copy
    assert not settings_path().with_name("settings.json.bak-2").exists()


def test_file_that_still_cannot_be_read_is_not_overwritten(monkeypatch: pytest.MonkeyPatch):
    """E.g. a root-owned 0600 file on POSIX: replacing it needs only write access to the folder."""
    save_settings(_six_charms())
    original = settings_path().read_bytes()
    monkeypatch.setattr(config.time, "sleep", lambda _s: None)
    _deny_reads(monkeypatch, settings_path())
    loaded = load_settings()

    def copy_denied(src, dst, **_kwargs):
        raise PermissionError(13, "Permission denied", str(src))

    with monkeypatch.context() as patch:
        patch.setattr(config.shutil, "copy2", copy_denied)
        with pytest.raises(OSError, match="Not overwriting"):
            save_settings(loaded)
    assert raw(settings_path()) == original
    assert sorted(p.name for p in config_dir().iterdir() if p.is_file()) == ["settings.json"]

    save_settings(loaded)  # readable again (the lock is gone): backed up, then replaced
    assert settings_path().with_name("settings.json.bak-1").read_bytes() == original


def test_windows_retries_a_locked_read(monkeypatch: pytest.MonkeyPatch):
    save_settings(_six_charms())
    sleeps: list[float] = []
    monkeypatch.setattr(config.time, "sleep", sleeps.append)
    monkeypatch.setattr(config.sys, "platform", "win32")
    denied = _deny_reads(monkeypatch, settings_path(), times=2)  # a scanner lets go after ~150 ms
    loaded = load_settings()
    assert len(loaded.charms) == 6 and loaded.rope_style == "neon"
    assert len(denied) == 2 and sleeps == pytest.approx([0.05, 0.1])
    save_settings(loaded)
    assert not settings_path().with_name("settings.json.bak-1").exists()  # nothing to protect


def test_posix_permission_errors_are_not_retried(monkeypatch: pytest.MonkeyPatch):
    save_settings(_six_charms())
    sleeps: list[float] = []
    monkeypatch.setattr(config.time, "sleep", sleeps.append)
    monkeypatch.setattr(config.sys, "platform", "linux")
    denied = _deny_reads(monkeypatch, settings_path())
    assert len(load_settings().charms) == 2
    assert len(denied) == 1 and sleeps == []


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows sharing violation")
def test_windows_file_locked_by_another_program(monkeypatch: pytest.MonkeyPatch):
    """A backup/sync/AV tool holding settings.json open with share mode 0 at login."""
    import ctypes
    import threading
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    def lock() -> int:
        # GENERIC_READ, share mode 0, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL
        handle = kernel32.CreateFileW(str(settings_path()), 0x80000000, 0, None, 3, 0x80, None)
        assert handle not in (None, wintypes.HANDLE(-1).value)
        return handle

    save_settings(_six_charms())
    original = settings_path().read_bytes()

    handle = lock()  # released while load_settings() is still retrying
    timer = threading.Timer(0.12, kernel32.CloseHandle, (handle,))
    timer.start()
    try:
        assert len(load_settings().charms) == 6
    finally:
        timer.join()

    handle = lock()  # held for longer than the retries
    try:
        loaded = load_settings()
        assert len(loaded.charms) == 2
        with pytest.raises(OSError):
            save_settings(loaded)  # the copy fails too: nothing is overwritten
    finally:
        kernel32.CloseHandle(handle)
    assert settings_path().read_bytes() == original
    save_settings(loaded)
    assert settings_path().with_name("settings.json.bak-1").read_bytes() == original


def test_backup_settings_helper():
    assert backup_settings() is None
    save_settings(Settings.default())
    backup = backup_settings()
    assert backup == settings_path().with_name("settings.json.bak-1")
    assert backup.is_file() and not settings_path().exists()


def test_valid_file_loads_all_fields():
    write_json(settings_path(), v1_payload())
    s = load_settings()
    assert (s.rope_style, s.scale, s.breeze, s.hover_swing, s.hidden) == ("neon", 1.3, True, False, True)
    (c,) = s.charms
    assert (c.id, c.kind, c.name, c.image, c.x_frac, c.length, c.screen) == (
        "a" * 32,
        "builtin",
        "heart",
        None,
        0.25,
        200.0,
        "DP-1",
    )


def test_unknown_keys_are_ignored():
    data = v1_payload(future_feature={"x": 1}, theme="dark")
    data["charms"][0]["sparkle"] = True
    write_json(settings_path(), data)
    s = load_settings()
    assert s.rope_style == "neon"
    assert len(s.charms) == 1
    assert not hasattr(s, "theme")


def test_newer_schema_version_loads_best_effort():
    write_json(settings_path(), v1_payload(version=SCHEMA_VERSION + 5))
    s = load_settings()
    assert s.version == SCHEMA_VERSION
    assert s.rope_style == "neon"


def test_fields_are_validated_and_clamped():
    image = make_image("ok.png")
    data = {
        "version": 1,
        "rope_style": "rainbow",
        "scale": 10,
        "breeze": "yes",
        "hover_swing": 0,
        "hidden": None,
        "charms": [
            {"id": "b" * 32, "kind": "builtin", "name": "star", "x_frac": -3, "length": 1},
            {"id": "c" * 32, "kind": "builtin", "name": "moon", "x_frac": 7.5, "length": 1e9},
            {"kind": "builtin", "name": "star", "x_frac": "left", "length": None, "screen": 42},
            {"kind": "builtin", "name": "unicorn"},
            {"kind": "image", "image": "missing.png"},
            {"kind": "image", "image": image, "x_frac": 0.4},
            {"kind": "image", "image": 123},
            {"kind": "gif", "name": "star"},
            "not a charm",
            None,
        ],
    }
    write_json(settings_path(), data)
    s = load_settings()
    assert s.rope_style == "gold"
    assert s.scale == 3.0
    assert (s.breeze, s.hover_swing, s.hidden) == (False, False, False)
    assert [(c.kind, c.name, c.image) for c in s.charms] == [
        ("builtin", "star", None),
        ("builtin", "moon", None),
        ("builtin", "star", None),
        ("image", None, "ok.png"),
    ]
    first, second, third, fourth = s.charms
    assert (first.x_frac, first.length) == (0.0, 40.0)
    assert (second.x_frac, second.length) == (1.0, 3000.0)
    assert (third.x_frac, third.length, third.screen) == (0.5, 150.0, None)
    assert fourth.x_frac == 0.4
    assert len(third.id) == 32  # missing id was generated


@pytest.mark.parametrize(("raw", "expected"), [(0.01, 0.3), (0.3, 0.3), (2.2, 2.2), (99, 3.0), ("big", 1.0)])
def test_scale_is_clamped_into_range(raw: object, expected: float):
    write_json(settings_path(), v1_payload(scale=raw))
    assert load_settings().scale == expected


def test_non_finite_numbers_fall_back_to_defaults():
    settings_path().parent.mkdir(parents=True, exist_ok=True)
    settings_path().write_text(
        '{"version": 1, "scale": NaN, "charms": [{"kind": "builtin", "name": "star", '
        '"x_frac": Infinity, "length": -Infinity}]}',
        encoding="utf-8",
    )
    s = load_settings()
    assert s.scale == 1.0
    assert (s.charms[0].x_frac, s.charms[0].length) == (0.5, 150.0)


def test_huge_integers_do_not_crash():
    write_json(settings_path(), v1_payload(scale=10**400))
    assert load_settings().scale == 1.0


def test_duplicate_and_malformed_ids_are_regenerated():
    charm = {"kind": "builtin", "name": "star"}
    write_json(
        settings_path(),
        v1_payload(
            charms=[
                {**charm, "id": "d" * 32},
                {**charm, "id": "d" * 32},
                {**charm, "id": "../../etc"},
                {**charm, "id": 17},
            ]
        ),
    )
    ids = [c.id for c in load_settings().charms]
    assert ids[0] == "d" * 32
    assert len(set(ids)) == 4


def test_image_names_cannot_escape_charms_dir(tmp_path: Path):
    (tmp_path / "secret.png").write_bytes(b"outside")
    inside = make_image("inside.png")
    write_json(
        settings_path(),
        v1_payload(
            charms=[
                {"kind": "image", "image": "../../secret.png"},
                {"kind": "image", "image": str(tmp_path / "secret.png")},
                {"kind": "image", "image": f"..\\..\\{inside}"},
                {"kind": "image", "image": ".."},
            ]
        ),
    )
    s = load_settings()
    assert [c.image for c in s.charms] == ["inside.png"]


# --------------------------------------------------------------------------- legacy (v0) migration


def legacy_payload(image_path: Path, missing_path: Path) -> dict:
    return {
        "rope": "silver",
        "scale": 0.8,
        "charms": [
            {"kind": "builtin", "name": "nazar", "x": 1344.0, "len": 160},
            {"kind": "image", "path": str(image_path), "x": 480.0, "len": 20},
            {"kind": "image", "path": str(missing_path), "x": 100.0, "len": 150},
            {"kind": "builtin", "name": "unicorn", "x": 10.0, "len": 150},
            {"kind": "builtin", "name": "star", "x": 5000.0, "len": 99999},
        ],
    }


def test_legacy_v0_migration_copies_images(tmp_path: Path):
    source = tmp_path / "Pictures" / "Photo.JPG"
    source.parent.mkdir()
    source.write_bytes(b"jpeg bytes")
    original = legacy_payload(source, tmp_path / "gone.png")
    write_json(settings_path(), original)

    s = load_settings()
    assert s.version == SCHEMA_VERSION
    assert (s.rope_style, s.scale) == ("silver", 0.8)
    kinds = [(c.kind, c.name) for c in s.charms]
    assert kinds == [("builtin", "nazar"), ("image", None), ("builtin", "star")]
    nazar, photo, star = s.charms
    assert nazar.x_frac == pytest.approx(1344.0 / 1920.0)
    assert nazar.length == 160.0
    assert photo.x_frac == pytest.approx(0.25)
    assert photo.length == 40.0
    assert (star.x_frac, star.length) == (1.0, 3000.0)

    # image copied into charms_dir() under a uuid name, keeping the (lower-cased) extension
    assert photo.image is not None and photo.image.endswith(".jpg")
    assert len(Path(photo.image).stem) == 32
    assert (charms_dir() / photo.image).read_bytes() == b"jpeg bytes"
    assert source.exists()  # the original is never touched

    # migration is persisted (so images are imported once) and the legacy file is kept as backup
    assert json.loads(settings_path().read_text(encoding="utf-8"))["version"] == SCHEMA_VERSION
    backup = settings_path().with_name("settings.json.bak-1")
    assert json.loads(backup.read_text(encoding="utf-8")) == original
    again = load_settings()
    assert again == s
    assert len(list(charms_dir().iterdir())) == 1


def test_legacy_image_already_inside_charms_dir_is_reused():
    existing = charms_dir() / "0123456789.png"
    existing.write_bytes(b"png")
    write_json(settings_path(), {"charms": [{"kind": "image", "path": str(existing), "x": 960, "len": 100}]})
    (charm,) = load_settings().charms
    assert charm.image == "0123456789.png"
    assert list(charms_dir().iterdir()) == [existing]


def test_legacy_defaults_and_kind_inference(tmp_path: Path):
    image = tmp_path / "pic.png"
    image.write_bytes(b"png")
    write_json(
        settings_path(),
        {"rope": "velvet", "charms": [{"name": "heart"}, {"path": str(image)}, {"kind": "builtin"}, 7]},
    )
    s = load_settings()
    assert (s.rope_style, s.scale) == ("gold", 1.0)
    heart, pic = s.charms
    assert (heart.kind, heart.name, heart.x_frac, heart.length) == ("builtin", "heart", 0.5, 150.0)
    assert pic.kind == "image" and (charms_dir() / pic.image).is_file()


def test_legacy_prototype_config_json_is_migrated(tmp_path: Path):
    """The Linux prototype wrote ``config.json`` in the same folder; pick it up on first run."""
    moon = {"kind": "builtin", "name": "moon", "x": 960, "len": 130}
    legacy = write_json(config_dir() / "config.json", {"rope": "neon", "charms": [moon]})
    s = load_settings()
    assert s.rope_style == "neon"
    assert [(c.name, c.x_frac, c.length) for c in s.charms] == [("moon", 0.5, 130.0)]
    assert settings_path().is_file()  # persisted as v1
    assert legacy.is_file()  # left in place
    assert load_settings() == s


def test_explicit_path_does_not_use_legacy_fallback(tmp_path: Path):
    write_json(config_dir() / "config.json", {"rope": "neon", "charms": []})
    s = load_settings(tmp_path / "elsewhere.json")
    assert s.rope_style == "gold"
    assert [c.name for c in s.charms] == ["nazar", "star"]


def test_settings_file_permissions_are_private_on_posix():
    save_settings(Settings.default())
    if os.name == "posix":
        assert settings_path().stat().st_mode & 0o077 == 0
