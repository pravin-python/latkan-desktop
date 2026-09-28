# Contributing to Latkan

Thanks for helping! Latkan is a small Python + PySide6 app, so getting started is quick.

## Set up

```bash
git clone https://github.com/pravin-python/latkan-desktop.git
cd latkan-desktop
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
python -m pip install -e ".[dev]"
python -m latkan --debug
```

On Ubuntu/Debian you also need Qt's runtime libraries:
`sudo apt install libxcb-cursor0 libxkbcommon-x11-0 libegl1 libgl1 libdbus-1-3 libfontconfig1`.

## Before you open a pull request

```bash
python -m ruff check .
python -m ruff format --check src tests scripts
QT_QPA_PLATFORM=offscreen python -m pytest -q      # PowerShell: $env:QT_QPA_PLATFORM="offscreen"
```

- Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) first: it defines the module boundaries.
  `physics.py` and `config.py` must stay free of Qt imports.
- Anything OS-specific belongs in `src/latkan/platform/<os>.py`, behind the `PlatformIntegration`
  interface, with native imports guarded so every module imports on every OS.
- If you change behaviour on one OS, say in the PR which OSes you tested on. CI runs the tests on all
  three, but it can't check how the overlay looks or feels.
- Add tests for logic you change; keep the idle state at 0 % CPU (the animation timer must stop when
  every charm is at rest).
- Add new shell scripts with `git add --chmod=+x <script>.sh`. On a Windows checkout
  (`core.filemode=false`) git otherwise records them as non-executable, and a Linux or macOS clone
  cannot run them. `tests/test_packaging.py` checks this.

## Charm artwork

New built-in charms must be your own original work (drawn with `QPainter` in `charms.py` or an image
you have the rights to). Please don't submit logos, characters or other third-party IP.

## Releases

Maintainers bump `__version__` in `src/latkan/__init__.py`, update `CHANGELOG.md`, and push a
`vX.Y.Z` tag; the release workflow builds and uploads every package.
