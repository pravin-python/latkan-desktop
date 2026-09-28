"""Settings model, per-OS locations and robust JSON persistence.

Pure Python (no Qt) so it is trivially testable.

* ``load_settings`` never raises for bad data: a missing file yields defaults, a structurally
  invalid file is moved aside (``settings.json.bak-<n>``) and replaced by defaults, and every
  field is validated and clamped individually. Unknown keys are ignored so newer versions can
  add fields without breaking older ones. A file that cannot be read (permissions, a Windows
  lock that outlasts a short retry) yields defaults too but is left alone, and the first save
  over it copies it aside first (or refuses), so the defaults never silently replace it.
* ``save_settings`` is atomic (temp file in the same directory, ``fsync``, ``os.replace``), so a
  crash or power loss leaves either the old or the new file, never a truncated one.
* The legacy v0 format written by the single-file prototype is migrated transparently.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "BUILTIN_NAMES",
    "ROPE_STYLES",
    "SCALES",
    "SCHEMA_VERSION",
    "CharmSpec",
    "Settings",
    "backup_settings",
    "charms_dir",
    "config_dir",
    "load_settings",
    "log_dir",
    "save_settings",
    "settings_path",
]

SCHEMA_VERSION = 1
ROPE_STYLES = ("gold", "silver", "neon", "kalava")
SCALES = (0.6, 0.8, 1.0, 1.3, 1.6)
BUILTIN_NAMES = ("nazar", "heart", "star", "moon", "clover", "dreamcatcher")

KIND_BUILTIN = "builtin"
KIND_IMAGE = "image"

SCALE_MIN, SCALE_MAX = 0.3, 3.0
LENGTH_MIN, LENGTH_MAX = 40.0, 3000.0
DEFAULT_LENGTH = 150.0
DEFAULT_X_FRAC = 0.5

ENV_HOME = "LATKAN_HOME"
SETTINGS_FILE = "settings.json"
LEGACY_SETTINGS_FILE = "config.json"  # written by the prototype into ~/.config/latkan
_WINDOWS_MAC_DIR = "Latkan"
_XDG_DIR = "latkan"
_LEGACY_SCREEN_WIDTH = 1920.0  # v0 stored anchor x in px; assume a 1920 px wide screen
_MAX_LEGACY_IMAGE_BYTES = 40 * 1024 * 1024
_MAX_SCREEN_NAME = 256
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SUFFIX_RE = re.compile(r"^\.[A-Za-z0-9]{1,5}$")
_REPLACE_ATTEMPTS = 5
_READ_ATTEMPTS = 6  # 50 + 100 + ... + 250 ms of retries for a file locked on Windows

#: Settings files that existed but could not be read (the app runs on defaults meanwhile). The
#: first save to one of them copies it to ``<name>.bak-<n>`` first, or refuses to overwrite it.
_unreadable: set[Path] = set()


# --------------------------------------------------------------------------- data model


def _new_id() -> str:
    return uuid.uuid4().hex


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass
class CharmSpec:
    """One charm as persisted: what it looks like and where/how long it hangs."""

    id: str  # uuid4 hex, stable identity
    kind: str  # "builtin" | "image"
    name: str | None = None  # builtin name (kind == builtin)
    image: str | None = None  # file name inside charms_dir() (kind == image), never an absolute path
    x_frac: float = DEFAULT_X_FRAC  # anchor x as fraction of its screen width (0..1)
    length: float = DEFAULT_LENGTH  # rope length in logical px
    screen: str | None = None  # QScreen.name() it hangs on; None/unknown -> primary

    @staticmethod
    def new_builtin(name: str, x_frac: float, length: float, screen: str | None = None) -> CharmSpec:
        """Create a built-in charm with a fresh id. Raises ValueError for an unknown ``name``."""
        if name not in BUILTIN_NAMES:
            raise ValueError(f"unknown built-in charm {name!r}")
        return CharmSpec(
            id=_new_id(),
            kind=KIND_BUILTIN,
            name=name,
            x_frac=_valid_float(x_frac, DEFAULT_X_FRAC, 0.0, 1.0),
            length=_valid_float(length, DEFAULT_LENGTH, LENGTH_MIN, LENGTH_MAX),
            screen=_valid_screen(screen),
        )

    @staticmethod
    def new_image(image: str, x_frac: float, length: float, screen: str | None = None) -> CharmSpec:
        """Create an image charm with a fresh id.

        ``image`` must be a bare file name inside :func:`charms_dir` (as returned by
        ``charms.import_image``); a path raises ValueError.
        """
        if _safe_file_name(image) != image:
            raise ValueError(f"image must be a file name inside the charms folder, got {image!r}")
        return CharmSpec(
            id=_new_id(),
            kind=KIND_IMAGE,
            image=image,
            x_frac=_valid_float(x_frac, DEFAULT_X_FRAC, 0.0, 1.0),
            length=_valid_float(length, DEFAULT_LENGTH, LENGTH_MIN, LENGTH_MAX),
            screen=_valid_screen(screen),
        )


@dataclass
class Settings:
    """Everything Latkan persists."""

    version: int = SCHEMA_VERSION
    rope_style: str = "gold"
    scale: float = 1.0
    breeze: bool = False  # gentle idle sway (keeps animation running)
    hover_swing: bool = True  # cursor sweeping through a charm pushes it
    hidden: bool = False
    charms: list[CharmSpec] = field(default_factory=list)

    @staticmethod
    def default() -> Settings:
        """First-run settings: a nazar and a star hanging near the top right."""
        return Settings(
            charms=[
                CharmSpec.new_builtin("nazar", 0.72, 170.0),
                CharmSpec.new_builtin("star", 0.80, 110.0),
            ]
        )


# --------------------------------------------------------------------------- locations


def config_dir() -> Path:
    """Per-user configuration directory for the running OS (not created).

    Windows ``%APPDATA%\\Latkan``; macOS ``~/Library/Application Support/Latkan``; Linux and other
    Unix ``$XDG_CONFIG_HOME/latkan`` or ``~/.config/latkan``. ``LATKAN_HOME`` overrides all.
    """
    override = os.environ.get(ENV_HOME, "").strip()
    if override:
        return Path(override).expanduser().absolute()
    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA", "").strip()
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / _WINDOWS_MAC_DIR
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / _WINDOWS_MAC_DIR
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    # The XDG spec says relative values must be ignored.
    base = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".config"
    return base / _XDG_DIR


def _ensure_dir(path: Path) -> Path:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("Could not create directory %s: %s", path, exc)
    return path


def charms_dir() -> Path:
    """Folder holding imported charm images (created on demand, best effort)."""
    return _ensure_dir(config_dir() / "charms")


def log_dir() -> Path:
    """Folder for log files (created on demand, best effort)."""
    return _ensure_dir(config_dir() / "logs")


def settings_path() -> Path:
    """Location of ``settings.json``."""
    return config_dir() / SETTINGS_FILE


# --------------------------------------------------------------------------- validation helpers


def _valid_float(value: Any, default: float, low: float, high: float) -> float:
    """Finite number clamped into ``[low, high]``; anything else -> ``default``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    try:
        number = float(value)
    except OverflowError:  # a JSON integer too large for a float
        return default
    if not math.isfinite(number):
        return default
    return _clamp(number, low, high)


def _valid_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    return default


def _valid_screen(value: Any) -> str | None:
    if isinstance(value, str) and value.strip() and len(value) <= _MAX_SCREEN_NAME:
        return value
    return None


def _safe_file_name(value: Any) -> str | None:
    """Reduce ``value`` to a plain file name (last path component) or None if unusable.

    Settings only ever reference files *inside* ``charms_dir()``; stripping directories means a
    hand-edited ``"../../x.png"`` can never escape it.
    """
    if not isinstance(value, str):
        return None
    name = re.split(r"[\\/]", value.strip())[-1].strip()
    if not name or name in (".", "..") or ":" in name or "\x00" in name:
        return None
    return name


def _dedupe_ids(charms: list[CharmSpec]) -> None:
    seen: set[str] = set()
    for spec in charms:
        if not _ID_RE.match(spec.id) or spec.id in seen:
            spec.id = _new_id()
        seen.add(spec.id)


def _parse_charm(raw: Any, images_dir: Path) -> CharmSpec | None:
    """Validate one v1 charm entry; None means "drop it" (reason logged)."""
    if not isinstance(raw, dict):
        logger.warning("Dropping charm entry that is not an object: %r", raw)
        return None
    kind = raw.get("kind")
    raw_id = raw.get("id")
    charm_id = raw_id if isinstance(raw_id, str) and _ID_RE.match(raw_id) else _new_id()
    x_frac = _valid_float(raw.get("x_frac"), DEFAULT_X_FRAC, 0.0, 1.0)
    length = _valid_float(raw.get("length"), DEFAULT_LENGTH, LENGTH_MIN, LENGTH_MAX)
    screen = _valid_screen(raw.get("screen"))

    if kind == KIND_BUILTIN:
        name = raw.get("name")
        if name not in BUILTIN_NAMES:
            logger.warning("Dropping charm with unknown built-in name %r", name)
            return None
        return CharmSpec(charm_id, KIND_BUILTIN, name=name, x_frac=x_frac, length=length, screen=screen)
    if kind == KIND_IMAGE:
        image = _safe_file_name(raw.get("image"))
        if image is None:
            logger.warning("Dropping image charm with invalid file name %r", raw.get("image"))
            return None
        if not (images_dir / image).is_file():
            logger.warning("Dropping image charm whose file is missing: %s", images_dir / image)
            return None
        return CharmSpec(charm_id, KIND_IMAGE, image=image, x_frac=x_frac, length=length, screen=screen)
    logger.warning("Dropping charm of unknown kind %r", kind)
    return None


class _InvalidSettings(ValueError):
    """The file parsed as JSON but its structure is unusable."""


def _parse_v1(data: dict[str, Any]) -> Settings:
    version = data.get("version")
    if isinstance(version, int) and not isinstance(version, bool) and version > SCHEMA_VERSION:
        logger.warning(
            "Settings were written by a newer Latkan (schema %s > %s); loading what is understood",
            version,
            SCHEMA_VERSION,
        )
    raw_charms = data.get("charms", [])
    if not isinstance(raw_charms, list):
        raise _InvalidSettings(f"'charms' must be a list, got {type(raw_charms).__name__}")

    rope_style = data.get("rope_style")
    if rope_style not in ROPE_STYLES:
        if rope_style is not None:
            logger.warning("Unknown rope style %r; using gold", rope_style)
        rope_style = "gold"

    images_dir = charms_dir()
    charms = [spec for spec in (_parse_charm(raw, images_dir) for raw in raw_charms) if spec is not None]
    _dedupe_ids(charms)
    return Settings(
        version=SCHEMA_VERSION,
        rope_style=rope_style,
        scale=_valid_float(data.get("scale"), 1.0, SCALE_MIN, SCALE_MAX),
        breeze=_valid_bool(data.get("breeze"), False),
        hover_swing=_valid_bool(data.get("hover_swing"), True),
        hidden=_valid_bool(data.get("hidden"), False),
        charms=charms,
    )


# --------------------------------------------------------------------------- legacy (v0) migration


def _is_legacy(data: dict[str, Any]) -> bool:
    version = data.get("version")
    return version is None or (isinstance(version, int) and not isinstance(version, bool) and version < 1)


def _import_legacy_image(source: Any, images_dir: Path) -> str | None:
    """Copy a v0 absolute image path into ``charms_dir()``; return the new file name or None."""
    if not isinstance(source, str) or not source.strip():
        return None
    src = Path(source).expanduser()
    try:
        if not src.is_file():
            logger.warning("Legacy charm image %s is missing; skipping it", src)
            return None
        if src.stat().st_size > _MAX_LEGACY_IMAGE_BYTES:
            logger.warning("Legacy charm image %s is larger than 40 MB; skipping it", src)
            return None
        # The Linux prototype already kept its images in ~/.config/latkan/charms == charms_dir().
        if src.resolve().parent == images_dir.resolve():
            return src.name
        suffix = src.suffix.lower() if _SUFFIX_RE.match(src.suffix) else ""
        name = f"{_new_id()}{suffix}"
        images_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, images_dir / name)
    except OSError as exc:
        logger.warning("Could not import legacy charm image %s: %s", src, exc)
        return None
    logger.info("Imported legacy charm image %s as %s", src, name)
    return name


def _migrate_v0(data: dict[str, Any]) -> Settings:
    """Convert the prototype format ``{"rope","scale","charms":[{kind,name|path,x,len}]}``."""
    raw_charms = data.get("charms", [])
    if not isinstance(raw_charms, list):
        raise _InvalidSettings("legacy 'charms' must be a list")
    rope = data.get("rope")
    images_dir = charms_dir()
    charms: list[CharmSpec] = []
    for raw in raw_charms:
        if not isinstance(raw, dict):
            logger.warning("Dropping legacy charm entry that is not an object: %r", raw)
            continue
        kind = raw.get("kind")
        if kind is None:
            kind = KIND_IMAGE if "path" in raw else KIND_BUILTIN
        x_px = _valid_float(raw.get("x"), DEFAULT_X_FRAC * _LEGACY_SCREEN_WIDTH, -math.inf, math.inf)
        x_frac = _clamp(x_px / _LEGACY_SCREEN_WIDTH, 0.0, 1.0)
        length = _valid_float(raw.get("len"), DEFAULT_LENGTH, LENGTH_MIN, LENGTH_MAX)
        if kind == KIND_BUILTIN:
            name = raw.get("name")
            if name not in BUILTIN_NAMES:
                logger.warning("Dropping legacy charm with unknown built-in name %r", name)
                continue
            charms.append(CharmSpec(_new_id(), KIND_BUILTIN, name=name, x_frac=x_frac, length=length))
        elif kind == KIND_IMAGE:
            image = _import_legacy_image(raw.get("path"), images_dir)
            if image is not None:
                charms.append(CharmSpec(_new_id(), KIND_IMAGE, image=image, x_frac=x_frac, length=length))
        else:
            logger.warning("Dropping legacy charm of unknown kind %r", kind)
    logger.info("Migrated legacy (v0) settings with %d charm(s)", len(charms))
    return Settings(
        rope_style=rope if rope in ROPE_STYLES else "gold",
        scale=_valid_float(data.get("scale"), 1.0, SCALE_MIN, SCALE_MAX),
        charms=charms,
    )


# --------------------------------------------------------------------------- load / save


def _next_backup_path(path: Path) -> Path:
    n = 1
    while True:
        candidate = path.with_name(f"{path.name}.bak-{n}")
        if not candidate.exists():
            return candidate
        n += 1


def _backup(path: Path, *, move: bool) -> Path | None:
    """Move (or copy) ``path`` to the first free ``<name>.bak-<n>``; None if that failed."""
    target = _next_backup_path(path)
    try:
        if move:
            os.rename(path, target)
        else:
            shutil.copy2(path, target)
    except OSError as exc:
        logger.error("Could not back up %s to %s: %s", path, target, exc)
        return None
    return target


def backup_settings(path: Path | None = None) -> Path | None:
    """Move the settings file aside to ``settings.json.bak-<n>`` (e.g. for ``--reset``).

    Returns the backup path, or None when there was no file (or it could not be moved).
    """
    target = Path(path) if path is not None else settings_path()
    if not target.exists():
        return None
    backup = _backup(target, move=True)
    if backup is not None:
        logger.info("Backed up %s to %s", target, backup)
    return backup


def _read_bytes(path: Path) -> bytes:
    """``path.read_bytes()`` with a short retry on Windows for a file another program has locked.

    Backup, sync and antivirus tools may hold the file open without read sharing for a moment
    (at login, just when autostart runs Latkan). Python reports that sharing violation as a
    plain PermissionError (the C runtime maps it to EACCES, ``winerror`` is not set), so every
    PermissionError except the one for a directory is retried, like writes in :func:`_replace`.
    """
    attempt = 0
    while True:
        try:
            return path.read_bytes()
        except PermissionError:
            attempt += 1
            if not sys.platform.startswith("win") or attempt >= _READ_ATTEMPTS or path.is_dir():
                raise
            time.sleep(0.05 * attempt)


def _read_json_object(path: Path) -> dict[str, Any]:
    """Read ``path`` as a JSON object. Raises OSError, or ValueError for corrupt content."""
    raw = _read_bytes(path)
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except RecursionError as exc:  # absurdly nested JSON
        raise ValueError("settings JSON is nested too deeply") from exc
    if not isinstance(data, dict):
        raise _InvalidSettings(f"top level must be an object, got {type(data).__name__}")
    return data


def _persist_migration(settings: Settings, destination: Path, source: Path) -> None:
    """Save migrated settings right away so legacy images are imported exactly once."""
    if destination == source and destination.exists():
        backup = _backup(source, move=False)
        if backup is None:
            logger.warning("Not persisting migrated settings: original %s could not be backed up", source)
            return
        logger.info("Kept legacy settings as %s", backup)
    try:
        save_settings(settings, destination)
    except OSError as exc:
        logger.warning("Could not persist migrated settings to %s: %s", destination, exc)


def _load_legacy_fallback() -> Settings | None:
    """Migrate the prototype's ``config.json`` found next to where settings.json belongs."""
    legacy = config_dir() / LEGACY_SETTINGS_FILE
    try:
        data = _read_json_object(legacy)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("Ignoring unreadable legacy settings %s: %s", legacy, exc)
        return None
    if not _is_legacy(data):
        return None
    try:
        settings = _migrate_v0(data)
    except _InvalidSettings as exc:
        logger.warning("Ignoring invalid legacy settings %s: %s", legacy, exc)
        return None
    _persist_migration(settings, settings_path(), legacy)
    return settings


def load_settings(path: Path | None = None) -> Settings:
    """Load settings from ``path`` (default :func:`settings_path`). Never raises for bad data.

    * missing file -> :meth:`Settings.default` (with the default path, the prototype's legacy
      ``config.json`` in the same folder is migrated instead, if present);
    * undecodable / invalid JSON -> file renamed to ``settings.json.bak-<n>``, defaults returned;
    * every field validated and clamped, unknown keys ignored, unusable charms dropped;
    * legacy v0 content is migrated (images copied into :func:`charms_dir`) and saved back
      immediately (the original is kept as a ``.bak-<n>`` copy);
    * unreadable file (permissions; on Windows a lock held by another program is retried for
      under a second first) -> defaults, and the file is left alone: the first
      :func:`save_settings` to it copies it to ``settings.json.bak-<n>`` before replacing it,
      and refuses to replace it while that copy fails.
    """
    target = Path(path) if path is not None else settings_path()
    key = _path_key(target)
    try:
        data = _read_json_object(target)
    except FileNotFoundError:
        _unreadable.discard(key)
        if path is None:
            migrated = _load_legacy_fallback()
            if migrated is not None:
                return migrated
        logger.info("No settings at %s; using defaults", target)
        return Settings.default()
    except OSError as exc:
        # Unreadable (permissions, a lock, a directory, ...): not corrupt, so it is not moved
        # aside here; save_settings() copies it aside before the first save replaces it.
        _unreadable.add(key)
        logger.error("Could not read settings %s: %s; using defaults", target, exc)
        return Settings.default()
    except ValueError as exc:  # JSONDecodeError, UnicodeDecodeError, _InvalidSettings
        _unreadable.discard(key)
        return _recover_corrupt(target, exc)
    _unreadable.discard(key)

    try:
        if _is_legacy(data):
            settings = _migrate_v0(data)
            _persist_migration(settings, target, target)
            return settings
        return _parse_v1(data)
    except _InvalidSettings as exc:
        return _recover_corrupt(target, exc)


def _recover_corrupt(path: Path, error: Exception) -> Settings:
    backup = _backup(path, move=True)
    if backup is None:
        _unreadable.add(_path_key(path))  # still there: save_settings() copies it aside first
    logger.error(
        "Settings file %s is invalid (%s); %s; using defaults",
        path,
        error,
        f"moved it to {backup.name}" if backup is not None else "could not back it up",
    )
    return Settings.default()


def _path_key(path: Path) -> Path:
    """Normalized absolute form of ``path`` for :data:`_unreadable`."""
    return Path(os.path.abspath(path))


def _keep_unreadable(target: Path) -> None:
    """Before the first write over a file :func:`load_settings` could not use, copy it aside.

    Latkan then runs on defaults, and without this the next save (every change, and quitting
    after one) would silently replace the user's charms with them. A lock that has gone away
    by now (Windows) leaves the user's file as ``settings.json.bak-<n>``; a file that still
    cannot be read (e.g. a root-owned 0600 file on POSIX, where replacing it only needs write
    access to the folder) raises OSError, so it is never overwritten.
    """
    key = _path_key(target)
    if key not in _unreadable:
        return
    if os.path.lexists(target):
        backup = _backup(target, move=False)
        if backup is None:
            raise OSError(
                f"Not overwriting {target}: it could not be loaded at start-up and cannot be backed up now"
            )
        logger.warning("Kept the settings file that could not be loaded as %s", backup)
    _unreadable.discard(key)


def _serialize(settings: Settings) -> dict[str, Any]:
    data = asdict(settings)
    data["version"] = SCHEMA_VERSION
    # Keep the file strictly valid JSON (no NaN/Infinity) even if in-memory values went bad.
    data["scale"] = _valid_float(data.get("scale"), 1.0, SCALE_MIN, SCALE_MAX)
    for charm in data["charms"]:
        charm["x_frac"] = _valid_float(charm.get("x_frac"), DEFAULT_X_FRAC, 0.0, 1.0)
        charm["length"] = _valid_float(charm.get("length"), DEFAULT_LENGTH, LENGTH_MIN, LENGTH_MAX)
    return data


def _replace(src: str, dst: Path) -> None:
    """``os.replace`` with a short retry on Windows, where scanners/indexers briefly lock files."""
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if not sys.platform.startswith("win") or attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


def _fsync_dir(directory: Path) -> None:
    """Persist the rename itself (POSIX only; Windows has no directory handles for this)."""
    if os.name != "posix":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError as exc:
        logger.debug("Could not open %s to fsync it: %s", directory, exc)
        return
    try:
        os.fsync(fd)
    except OSError as exc:
        logger.debug("Could not fsync directory %s: %s", directory, exc)
    finally:
        os.close(fd)


def save_settings(settings: Settings, path: Path | None = None) -> None:
    """Atomically write ``settings`` as JSON to ``path`` (default :func:`settings_path`).

    Creates missing directories. Raises OSError if the file cannot be written; the previous file
    (if any) is then left untouched. A file :func:`load_settings` could not read is first copied
    to ``<name>.bak-<n>``; while that fails, OSError is raised and the file is not replaced.
    """
    target = Path(path) if path is not None else settings_path()
    payload = json.dumps(_serialize(settings), indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    _keep_unreadable(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    committed = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _replace(tmp_name, target)
        committed = True
    finally:
        if not committed:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
    _fsync_dir(target.parent)
    logger.debug("Saved settings to %s", target)
