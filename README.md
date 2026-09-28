# Latkan

**Latkan** (लटकन, "something that hangs") puts little charms on your desktop. They hang from
the top edge of the screen on ropes simulated with real physics: grab one and it swings, let go and
it sways back to rest. Everything else on the screen stays clickable.

It runs on **Windows 10/11**, **macOS 12+** (Apple Silicon and Intel) and **Linux** (Ubuntu 22.04+
and other modern distributions, X11 or Wayland via XWayland), from one Python + Qt codebase.

<!-- Screenshots: add docs/screenshot-windows.png, docs/screenshot-macos.png, docs/screenshot-linux.png -->
> _Screenshots coming soon._

## Features

- **Real rope physics**: Verlet simulation with a fixed timestep, so it behaves the same at 60 Hz
  and 144 Hz.
- **Six built-in charms**: nazar (evil eye), heart, lucky star, crescent moon, four-leaf clover,
  dream catcher. All are original vector artwork.
- **Your own charms**: turn any PNG/JPEG/WebP/BMP/SVG image into a charm.
- **Four rope styles**: golden thread, silver chain, neon glow, red-and-gold kalava thread.
- **Five charm sizes**, from 60 % to 160 %.
- **Gentle breeze** (optional idle sway) and **swing on hover** (brush past a charm with the cursor
  and it moves).
- **Multi-monitor and HiDPI aware**: charms remember which screen they hang on and come back when
  the monitor is reconnected.
- **Clicks go through**: only the ropes and charms catch the mouse.
- **0 % CPU when idle**: the animation stops as soon as everything has settled.
- **Start at login** (optional), single instance, settings saved automatically.

## Install

Download the package for your system from the project's **GitHub Releases** page.

| OS | File | How |
|---|---|---|
| Windows 10/11 (x64) | `Latkan-<version>-windows-x64-setup.exe` | Run it. It installs for your user only (no admin rights) into `%LOCALAPPDATA%\Programs\Latkan`, and can add a desktop icon and start at login (upgrades leave both boxes unticked and keep your current choice). A portable `...-windows-x64.zip` is also available. |
| macOS 12+ (Apple Silicon) | `Latkan-<version>-macos-arm64.dmg` | Open the DMG and drag **Latkan** onto **Applications**, then start it from there (not from the DMG or Downloads, or *Start at login* cannot be turned on). |
| macOS 12+ (Intel) | `Latkan-<version>-macos-x86_64.dmg` | Same as above. |
| Linux x86_64 | `Latkan-<version>-x86_64.AppImage` | `chmod +x Latkan-*.AppImage` then run it. Needs `libfuse2` (Ubuntu 24.04: `libfuse2t64`) or run it with `--appimage-extract-and-run`. |
| Linux x86_64 | `Latkan-<version>-linux-x86_64.tar.gz` | Extract anywhere and run `Latkan/Latkan`. |

Each release also includes `SHA256SUMS.txt`.

**macOS Gatekeeper.** Release builds are notarised when the maintainers have set up an Apple
Developer ID. If a build is only ad-hoc signed, macOS says the app "cannot be opened because the
developer cannot be verified". **Right-click (or Control-click) Latkan in Applications > Open >
Open.** You only need to do this once. On macOS 15 and later: try to open it once, then go to
**System Settings > Privacy & Security** and click **Open Anyway**.

Latkan has no Dock icon. Look for it in the menu bar. On macOS the charms hang from just below the
menu bar, so they never cover menus, menu-bar icons or the notch.

**Ubuntu / GNOME tray icon.** Latkan's menu lives in the system tray. Ubuntu ships the
*Ubuntu AppIndicators* extension, which provides it. Stock GNOME (Fedora, Debian) needs the
AppIndicator extension:

```bash
sudo apt install gnome-shell-extension-appindicator   # then log out and back in
```

Without a tray you can still **right-click any charm** to get the full menu.

**Wayland.** Wayland does not let normal apps position windows or keep them on top, so Latkan
runs through **XWayland** automatically. This is invisible on Ubuntu, Fedora and KDE, where
XWayland is enabled by default. If XWayland is turned off, log in with an X11 session
("Ubuntu on Xorg").

## Run from source

You need Python **3.10 or newer**. On Debian or Ubuntu, first install the venv module and the
X11 libraries Qt needs:

```bash
sudo apt install python3-venv libxcb-cursor0 libxkbcommon-x11-0 libegl1 libgl1 libdbus-1-3 libfontconfig1
```

Then, on every OS:

```bash
git clone https://github.com/pravin-python/latkan-desktop.git
cd latkan-desktop
python3 -m venv .venv            # Windows: py -m venv .venv
```

| OS | Activate the venv, install, run |
|---|---|
| Windows (PowerShell) | `.venv\Scripts\Activate.ps1`<br>`python -m pip install -e .`<br>`latkan` (or `python -m latkan`) |
| macOS / Linux | `source .venv/bin/activate`<br>`python -m pip install -e .`<br>`latkan` (or `python -m latkan`) |

**Linux one-step install** from a checkout. This sets up a private virtualenv, a `latkan`
launcher, a menu entry and an icon:

```bash
bash packaging/linux/install-from-source.sh              # add --autostart to start at login
bash packaging/linux/install-from-source.sh --uninstall  # remove it again (settings are kept)
```

Command-line options: `latkan --version`, `latkan --reset` (back up the settings and start
again with the defaults), `latkan --debug` (verbose logging).

**Dependencies.** Latkan depends on **`PySide6-Essentials`**, not the full `PySide6`
meta-package. Essentials contains every Qt module Latkan uses: QtCore, QtGui, QtWidgets,
QtNetwork, plus the image-format plugins. The full package also pulls in `PySide6-Addons`
(WebEngine, 3D, Multimedia, Charts and more), which adds several hundred megabytes that Latkan
never loads, and it makes the frozen builds larger. If you already have the full `PySide6`
installed, it satisfies this requirement. On macOS, `pyobjc-framework-Cocoa` is installed
automatically. It keeps charms on every Space, lets you click them without taking focus from
the app you are typing in, makes the rest of the screen reliably click-through and lets charms
swing on hover. Without it, Latkan still runs but with those limits.

## Usage

| Action | What happens |
|---|---|
| **Drag a charm** | Grab it and swing it around. Let go and it keeps its momentum. |
| **Drag the top of a rope** (the pin at the screen edge) | Moves the charm along the top of the screen. The position is saved. |
| **Double-click a charm** | Gives it a nudge. |
| **Right-click a charm** | Charm menu: Nudge, Longer rope, Shorter rope, Move to screen (with several monitors), Remove this charm. The full main menu follows. |
| **Tray / menu-bar icon** | Main menu: Add charm (built-ins or *From your image…*), Rope style, Charm size, Gentle breeze, Swing on hover, Nudge all, Show/Hide charms, Start at login, Remove all charms…, About, Quit. |
| **Left-click the tray icon** (Windows, Linux) | Nudges all charms. |
| **Start Latkan again** while it runs | The running copy shows its charms and nudges them. No second copy starts. On macOS, opening Latkan from Applications, Launchpad or Spotlight brings hidden (or all removed) charms back, and starting Latkan by hand always shows them. |

### Where settings are stored

| OS | Folder |
|---|---|
| Windows | `%APPDATA%\Latkan\` |
| macOS | `~/Library/Application Support/Latkan/` |
| Linux | `$XDG_CONFIG_HOME/latkan/` (usually `~/.config/latkan/`) |

The folder holds `settings.json`, `charms/` (copies of your imported images) and `logs/`
(`latkan.log`, rotated). Set the `LATKAN_HOME` environment variable to use a different folder.
A corrupt `settings.json` is renamed to `settings.json.bak-<n>` and Latkan starts with the
defaults. If the file cannot be read at all (its permissions, or another program keeping it
locked for longer than a moment), Latkan also starts with the defaults but leaves the file
alone. Before it saves over the file, it copies it to `settings.json.bak-<n>`. If it cannot
make that copy, it does not save. Uninstalling keeps this folder. Delete it by hand to remove
everything.

## Build the packages

Build on the OS you are targeting. PyInstaller does not cross-compile.

```bash
python -m pip install -e ".[dev]"     # pytest, ruff, PyInstaller, Pillow
python scripts/build.py
```

`scripts/build.py` renders the icons (`scripts/make_icons.py` → `assets/`), runs PyInstaller with
`packaging/latkan.spec` (windowed, onedir), then creates the native package in `dist/`:

| OS | Output | Extra requirements |
|---|---|---|
| Windows | `Latkan-<ver>-windows-x64-setup.exe` (Inno Setup found) or `Latkan-<ver>-windows-x64.zip`; `--portable-zip` builds both | [Inno Setup 6.3+](https://jrsoftware.org/isinfo.php) (`choco install innosetup`), found on PATH, in its default folder, or through `ISCC=` |
| macOS | `Latkan-<ver>-macos-<arm64\|x86_64>.dmg` | Xcode command-line tools. The app is ad-hoc signed unless `MACOS_SIGN_IDENTITY="Developer ID Application: …"` is set (then it gets the hardened runtime and a timestamp) |
| Linux | `Latkan-<ver>-linux-<arch>.tar.gz`, plus `Latkan-<ver>-<arch>.AppImage` when `appimagetool` is on PATH or `APPIMAGE=1` | `APPIMAGE=1 ALLOW_DOWNLOAD=1` lets the script download appimagetool |

Useful flags: `--skip-icons`, `--skip-pyinstaller` (repackage an existing `dist/`) and
`--no-package`.

**macOS 12 support.** Qt 6.10 and later need macOS 13. The spec sets
`LSMinimumSystemVersion` from the Qt version it bundles. To build for macOS 12, install
`"PySide6-Essentials<6.10"` first, as the release workflow does.

**Linux compatibility.** An AppImage runs on distributions whose glibc is at least as new as the
build machine's. Release builds use Ubuntu 22.04. The Linux packages leave out Qt's GTK 3 platform
theme. With it, the app would run the build machine's GTK and GLib, which then load the newer GTK
and GIO modules of your system, and the file chooser could crash Latkan. The AppImage and tarball
therefore open Qt's own file dialog for *From your image…*. When you run from source, GNOME-style
desktops still show the GTK dialog, because the system's own GTK is used.

**Windows: keep the checkout path short.** The frozen app contains paths such as
`dist\Latkan\_internal\PySide6\plugins\imageformats\...`. If the repository lives deeper than about
150 characters, PyInstaller fails with *No such file or directory* (the 260-character `MAX_PATH`
limit). Clone to something like `C:\src\latkan`, or build through a directory junction
(`mklink /J C:\lk <repository>`, then run `python scripts/build.py` from `C:\lk`). The spec and
build script keep using the short path you started from.

**Bundle size.** The spec drops Qt parts Latkan never loads (the virtual keyboard with the
QtQuick/QML stack it drags in, TLS backends, software OpenGL, Qt translations, and on Linux the
GTK 3 theme with the GTK, Pango, Cairo and ATK libraries it links). A Windows build is about 58 MB
unpacked, or 24 MB as a zip. A dependency check fails the build if a remaining library still links
against something that was dropped. On Linux, `scripts/build.py` also refuses to package a build
that still contains GTK 3.

### Continuous integration and releases

- `.github/workflows/ci.yml`: ruff and pytest on Ubuntu, Windows and macOS with Python 3.10 and
  3.12 (Qt runs headless with `QT_QPA_PLATFORM=offscreen`), then a package build on each OS. The
  packages are uploaded as workflow artifacts.
- `.github/workflows/release.yml`: runs when you push a tag `vX.Y.Z` that matches `__version__` in
  `src/latkan/__init__.py`. It builds the Windows installer and zip, the macOS arm64 and x86_64
  DMGs, and the Linux AppImage and tarball, then publishes them with checksums as a GitHub
  release.

  macOS signing and notarisation are optional. Add the repository secrets `MACOS_CERT_P12`
  (base64 .p12), `MACOS_CERT_PASSWORD`, `APPLE_ID`, `APPLE_TEAM_ID` and `APPLE_APP_PASSWORD` to
  turn them on.

Release checklist: bump `__version__`, commit, `git tag v1.2.3 && git push --tags`.

## Architecture

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the module contracts, per-OS behaviour and
the platform risk register. In short:

- `physics.py` and `config.py` are pure Python with no Qt, so they are easy to test.
- `overlay.py` runs one transparent, click-through window per screen.
- `platform/` holds the Windows, macOS and Linux specifics, behind one interface.

Run the checks with `python -m ruff check .` and `python -m pytest`. Set
`QT_QPA_PLATFORM=offscreen` on headless machines.

## Troubleshooting

**Linux: nothing appears, and the log or terminal says**
`qt.qpa.plugin: From 6.5.0, xcb-cursor0 or libxcb-cursor0 is needed to load the Qt xcb platform plugin.`
Install the missing X11 libraries:

```bash
sudo apt install libxcb-cursor0 libxkbcommon-x11-0 libegl1 libgl1 libdbus-1-3 libfontconfig1
```

On Fedora, use `sudo dnf install xcb-util-cursor libxkbcommon-x11`. For more detail, run
`QT_DEBUG_PLUGINS=1 latkan`.

**Linux: no tray icon on GNOME.** Install and enable the AppIndicator extension (see
[Install](#install)), then log out and back in. Until then, right-click a charm for the menu.

**Linux: charms do not show on Wayland.** Check that XWayland is available (`echo $DISPLAY`
should print something like `:0`). If it prints nothing, pick "Ubuntu on Xorg" on the login screen.

**Linux: charms have hard, jagged edges, or black boxes around them.** Transparent windows need a
*compositing* window manager. GNOME, KDE Plasma and Cinnamon have one built in, but LXQt/Openbox
(Lubuntu) ships without one, Xfce and MATE let you turn it off, and VNC or Xvfb displays have none.
Latkan notices this (the log says "No compositing window manager is running") and then cuts each
window to the exact shape of the ropes and charms: edges look harder and only the drawn pixels are
clickable, so grab a rope's top right on the thread. For smooth, translucent charms, turn the
compositor on (Xfce: *Window Manager Tweaks > Compositor*; MATE: *Windows > General > Enable software
compositing window manager*) or start one such as `picom`. KDE Plasma pauses compositing for some
full-screen games; Latkan switches over by itself when that happens.

**Linux: "Ubuntu on Xorg": the top of a rope opens the Activities or calendar instead of moving.**
On X11, GNOME's top bar receives the clicks in its strip even though the charms are drawn over it.
Grab the rope just below the bar: there Latkan keeps the full grab zone for moving the charm sideways.

**macOS: charms do not show over full-screen apps.** Charms appear on every Space and above
normal and full-screen windows. But apps that take over the display exclusively (some games,
presentation mode) draw above everything, and that is expected. If charms stay on a single Space
or clicking one takes focus from the app you were typing in, PyObjC is missing (source installs
only): `python -m pip install pyobjc-framework-Cocoa`.

**macOS: the Latkan menu-bar icon is missing.** macOS does not draw menu-bar icons that do not fit
beside the notch, or that you hid in *System Settings > Menu Bar* or with a menu-bar manager. Open
Latkan again from Applications (or Spotlight): it shows its charms, and right-clicking a charm gives
the full menu.

**macOS: "Could not turn on Start at login … Move Latkan to the Applications folder first".** You
started Latkan from the DMG or from Downloads, a place that is gone after a restart. Drag it to
Applications, start it from there and turn the option on again. If you move Latkan later, turn it
on again too.

**macOS: "Latkan can't be opened".** See the Gatekeeper note under [Install](#install)
(right-click > Open).

**Windows: "Windows protected your PC" (SmartScreen).** The installer is not code-signed, so
SmartScreen warns the first few times a new version is downloaded. Click **More info > Run
anyway**. If your antivirus flags the app, that is a known false positive with PyInstaller apps.
Please report it to the vendor.

**Charms are gone or off-screen.** Use *Show charms* in the tray menu (on macOS you can also just
open Latkan again). If that does not help,
run `latkan --reset` (or delete `settings.json`) to go back to the defaults.

**Filing a bug.** Please attach `logs/latkan.log` from the settings folder, and tell us your OS,
desktop environment and `latkan --version`.

## License

[MIT](LICENSE). Copyright (c) 2026 Latkan contributors.

Latkan uses Qt through PySide6, which is available under the LGPLv3.
