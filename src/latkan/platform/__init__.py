"""Per-OS integration behind one interface.

``get_platform()`` returns the :class:`PlatformIntegration` for the running OS. OS modules are
imported lazily, and every native dependency inside them is guarded, so this package imports
cleanly on Windows, macOS and Linux alike.
"""

from __future__ import annotations

import logging
import sys
import threading

from .base import PlatformIntegration

__all__ = ["PlatformIntegration", "get_platform"]

logger = logging.getLogger(__name__)

_cache: dict[str, PlatformIntegration] = {}
_lock = threading.Lock()


def _create(platform_id: str) -> PlatformIntegration:
    if platform_id.startswith("win"):
        from .windows import WindowsPlatform

        return WindowsPlatform()
    if platform_id == "darwin":
        from .macos import MacOSPlatform

        return MacOSPlatform()
    from .linux import LinuxPlatform  # Linux, the BSDs and any other X11/XDG desktop

    return LinuxPlatform()


def get_platform() -> PlatformIntegration:
    """The integration for ``sys.platform``; one cached instance per platform id."""
    platform_id = sys.platform
    with _lock:
        integration = _cache.get(platform_id)
        if integration is None:
            integration = _create(platform_id)
            _cache[platform_id] = integration
            logger.debug("Platform integration for %s: %s", platform_id, type(integration).__name__)
        return integration


def clear_cache() -> None:
    """Forget cached integrations (tests that monkeypatch ``sys.platform``)."""
    with _lock:
        _cache.clear()
