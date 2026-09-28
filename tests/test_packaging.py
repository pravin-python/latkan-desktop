"""Packaging checks that run without building anything.

They cover the Linux prune rules in packaging/latkan.spec (run with stub PyInstaller globals), the
post-build GTK check in scripts/build.py, and the git file modes of the shell scripts.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "packaging" / "latkan.spec"
QT = "PySide6/Qt"

# Destinations as PyInstaller's Linux analysis lays them out: files from the PySide6 wheel keep their
# package path, system libraries resolved through ldd go to the top level.
GTK_THEME_STACK = [
    f"{QT}/plugins/platformthemes/libqgtk3.so",
    "libgtk-3.so.0",
    "libgdk-3.so.0",
    "libgdk_pixbuf-2.0.so.0",
    "libatk-1.0.so.0",
    "libatk-bridge-2.0.so.0",
    "libatspi.so.0",
    "libpango-1.0.so.0",
    "libpangocairo-1.0.so.0",
    "libpangoft2-1.0.so.0",
    "libcairo.so.2",
    "libcairo-gobject.so.2",
    "libepoxy.so.0",
]
KEPT = [
    f"{QT}/lib/libQt6Core.so.6",
    f"{QT}/lib/libQt6Gui.so.6",
    f"{QT}/lib/libQt6DBus.so.6",
    f"{QT}/plugins/platforms/libqxcb.so",
    f"{QT}/plugins/platformthemes/libqxdgdesktopportal.so",
    f"{QT}/plugins/platforminputcontexts/libibusplatforminputcontextplugin.so",
    "libglib-2.0.so.0",
    "libgthread-2.0.so.0",
    "libgio-2.0.so.0",
    "libgobject-2.0.so.0",
    "libharfbuzz.so.0",
    "libfontconfig.so.1",
    "libfreetype.so.6",
]
# What the kept binaries link (the dropped ones are not checked).
KEPT_LINKS = {
    f"{QT}/lib/libQt6Core.so.6": ["libglib-2.0.so.0", "libgthread-2.0.so.0"],
    f"{QT}/lib/libQt6Gui.so.6": ["libQt6Core.so.6", "libQt6DBus.so.6", "libfontconfig.so.1"],
    f"{QT}/plugins/platformthemes/libqxdgdesktopportal.so": ["libQt6DBus.so.6", "libQt6Gui.so.6"],
    "libgio-2.0.so.0": ["libgobject-2.0.so.0", "libglib-2.0.so.0"],
    "libharfbuzz.so.0": ["libglib-2.0.so.0", "libfreetype.so.6"],
}


def _run_spec(
    monkeypatch: pytest.MonkeyPatch, platform: str, binaries: list[str], links: dict[str, list[str]]
) -> dict[str, Any]:
    """Execute the spec as PyInstaller would, with stub build targets and a fake bindepend."""
    bindepend = ModuleType("PyInstaller.depend.bindepend")
    bindepend.get_imports = lambda src, search_paths=None: {  # type: ignore[attr-defined]
        (name, f"/usr/lib/{name}") for name in links.get(src.removeprefix("/build/"), ())
    }
    depend = ModuleType("PyInstaller.depend")
    depend.bindepend = bindepend  # type: ignore[attr-defined]
    pyinstaller = ModuleType("PyInstaller")
    pyinstaller.depend = depend  # type: ignore[attr-defined]
    for name, module in (
        ("PyInstaller", pyinstaller),
        ("PyInstaller.depend", depend),
        ("PyInstaller.depend.bindepend", bindepend),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(sys, "platform", platform)

    toc = [(dest, f"/build/{dest}", "BINARY") for dest in binaries]

    def target(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace()

    namespace: dict[str, Any] = {
        "SPECPATH": str(SPEC.parent),
        "Analysis": lambda *_a, **_k: SimpleNamespace(binaries=toc, datas=[], pure=[], scripts=[]),
        "PYZ": target,
        "EXE": target,
        "COLLECT": target,
        "BUNDLE": target,
    }
    exec(compile(SPEC.read_text(encoding="utf-8"), str(SPEC), "exec"), namespace)
    return namespace


def test_linux_spec_prunes_the_gtk3_theme_and_the_libraries_only_it_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = _run_spec(monkeypatch, "linux", GTK_THEME_STACK + KEPT, KEPT_LINKS)
    assert [entry[0] for entry in namespace["a"].binaries] == KEPT
    assert [entry[0] for entry in namespace["_dropped_binaries"]] == GTK_THEME_STACK


def test_linux_spec_fails_when_a_kept_library_links_a_pruned_gtk_library(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    links = {**KEPT_LINKS, "libharfbuzz.so.0": ["libglib-2.0.so.0", "libcairo.so.2"]}
    with pytest.raises(SystemExit, match=r"libharfbuzz\.so\.0 links against pruned \['libcairo\.so\.2'\]"):
        _run_spec(monkeypatch, "linux", GTK_THEME_STACK + KEPT, links)


def _load_build_script() -> ModuleType:
    # Loaded under its own name: "build" would clash with the PyPA build package.
    spec = importlib.util.spec_from_file_location("latkan_build_script", ROOT / "scripts" / "build.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "leftover",
    [f"{QT}/plugins/platformthemes/libqgtk3.so", "libgtk-3.so.0", "libgdk-3.so.0"],
)
def test_build_refuses_a_linux_bundle_that_still_contains_gtk3(tmp_path: Path, leftover: str) -> None:
    build = _load_build_script()
    internal = tmp_path / "Latkan" / "_internal"
    for name in KEPT:
        (internal / name).parent.mkdir(parents=True, exist_ok=True)
        (internal / name).touch()
    build.check_linux_bundle(tmp_path / "Latkan")  # the pruned bundle passes

    (internal / leftover).parent.mkdir(parents=True, exist_ok=True)
    (internal / leftover).touch()
    with pytest.raises(build.BuildError, match=re.escape(f"_internal/{leftover}")):
        build.check_linux_bundle(tmp_path / "Latkan")


def test_shell_scripts_are_executable_in_git() -> None:
    """A Windows checkout (core.filemode=false) records new files as 100644; Linux/macOS clones then
    cannot run packaging/linux/install-from-source.sh and friends directly."""
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed")
    result = subprocess.run(
        [git, "ls-files", "--stage", "--", "*.sh"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        pytest.skip(f"not a usable git checkout: {result.stderr.strip()}")
    modes = {}
    for line in result.stdout.splitlines():
        mode, _object, _stage, path = line.split(maxsplit=3)
        modes[path] = mode
    if not modes:
        pytest.skip("the shell scripts are not tracked by git yet")
    wrong = sorted(path for path, mode in modes.items() if mode != "100755")
    assert not wrong, f"not executable in git; run: git update-index --chmod=+x {' '.join(wrong)}"
