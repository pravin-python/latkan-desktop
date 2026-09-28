"""Latkan - charms that hang from the top of your screen on physics-simulated ropes.

This module is deliberately import-light (no Qt) so tooling such as the build scripts and
packaging metadata can read the version without pulling in a GUI toolkit.
"""

__version__ = "1.0.0"
APP_NAME = "Latkan"
APP_ID = "in.latkan.app"

__all__ = ["APP_ID", "APP_NAME", "__version__"]
