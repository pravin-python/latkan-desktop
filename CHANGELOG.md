# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.0.0] - 2026-09-28

### Added
- Charms hang from the top edge of the screen on Verlet-physics ropes: grab, swing, let go.
- One transparent, always-on-top, click-through overlay per monitor; everything outside the
  charms and ropes stays clickable.
- Six original built-in charms: Nazar, Heart, Lucky star, Crescent moon, Four-leaf clover,
  Dream catcher. Custom charms from your own images.
- Rope styles: Golden thread, Silver chain, Neon glow, Red thread (kalava). Charm sizes 60–160 %.
- Gentle breeze, swing-on-hover, drag a rope's pin to move it, per-charm rope length.
- Tray / menu-bar menu, per-charm right-click menu, Start at login on all three OSes.
- Windows, macOS and Linux support; settings stored per OS; single-instance guard.
- PyInstaller packaging: Windows installer + portable zip, macOS `.dmg`, Linux AppImage + tarball.
- CI on Windows, macOS and Ubuntu; tagged releases build and upload all packages.

[Unreleased]: https://github.com/pravin-python/latkan-desktop/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/pravin-python/latkan-desktop/releases/tag/v1.0.0
