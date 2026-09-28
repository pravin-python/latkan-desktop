"""SingleInstance: primary/secondary detection and activation forwarding (headless)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import re
import uuid
from collections.abc import Callable, Iterator

import pytest
from PySide6.QtCore import QCoreApplication, QElapsedTimer, QEventLoop
from PySide6.QtWidgets import QApplication

from latkan.singleinstance import SingleInstance, default_key, sanitize_key


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    return app if isinstance(app, QApplication) else QApplication([])


@pytest.fixture
def unique_key() -> str:
    return f"latkan-test-{uuid.uuid4().hex}"


@pytest.fixture
def instances() -> Iterator[list[SingleInstance]]:
    created: list[SingleInstance] = []
    yield created
    for inst in created:
        inst.close()


def _wait_until(predicate: Callable[[], bool], timeout_ms: int = 3000) -> bool:
    clock = QElapsedTimer()
    clock.start()
    while not predicate() and clock.elapsed() < timeout_ms:
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
    return predicate()


def test_first_instance_is_primary(
    qapp: QApplication, unique_key: str, instances: list[SingleInstance]
) -> None:
    primary = SingleInstance(unique_key)
    instances.append(primary)
    assert primary.is_primary() is True
    assert primary.listening
    assert primary.is_primary() is True  # cached, does not listen twice


def test_secondary_returns_false_and_activates_primary(
    qapp: QApplication, unique_key: str, instances: list[SingleInstance]
) -> None:
    primary = SingleInstance(unique_key)
    instances.append(primary)
    assert primary.is_primary()
    activations: list[bool] = []
    primary.activated.connect(lambda: activations.append(True))

    secondary = SingleInstance(unique_key)
    instances.append(secondary)
    assert secondary.is_primary() is False
    assert not secondary.listening
    assert _wait_until(lambda: len(activations) == 1)

    another = SingleInstance(unique_key)
    instances.append(another)
    assert another.is_primary() is False
    assert _wait_until(lambda: len(activations) == 2)


def test_new_primary_after_previous_one_closed(
    qapp: QApplication, unique_key: str, instances: list[SingleInstance]
) -> None:
    first = SingleInstance(unique_key)
    instances.append(first)
    assert first.is_primary()
    first.close()
    first.close()  # idempotent

    second = SingleInstance(unique_key)
    instances.append(second)
    assert second.is_primary() is True
    assert second.listening


def test_unrelated_keys_do_not_interfere(qapp: QApplication, instances: list[SingleInstance]) -> None:
    a = SingleInstance(f"latkan-test-{uuid.uuid4().hex}")
    b = SingleInstance(f"latkan-test-{uuid.uuid4().hex}")
    instances.extend([a, b])
    assert a.is_primary() and b.is_primary()


def test_keys_are_sanitized() -> None:
    assert re.fullmatch(r"[A-Za-z0-9_-]+", default_key())
    assert default_key().startswith("latkan-")
    assert sanitize_key("latkan-J. Doe/../x") == "latkan-J__Doe____x"
    assert sanitize_key("") == "latkan"
    long_a, long_b = sanitize_key("x" * 500), sanitize_key("x" * 499 + "y")
    assert len(long_a) <= 40 and len(long_b) <= 40  # short enough for macOS socket paths
    assert long_a != long_b
    assert re.fullmatch(r"[A-Za-z0-9_-]+", long_a)


def test_default_key_disambiguates_sanitized_user_names(monkeypatch: pytest.MonkeyPatch) -> None:
    import latkan.singleinstance as si

    monkeypatch.setattr(si, "_current_user", lambda: "José")
    jose = si.default_key()
    monkeypatch.setattr(si, "_current_user", lambda: "Josè")
    jose_grave = si.default_key()
    assert re.fullmatch(r"[A-Za-z0-9_-]+", jose)
    assert jose != jose_grave  # both sanitize to "Jos_", the hash suffix keeps them apart
    monkeypatch.setattr(si, "_current_user", lambda: "alice")
    assert si.default_key() == "latkan-alice"


def test_constructor_key_is_sanitized(qapp: QApplication) -> None:
    inst = SingleInstance("latkan test/key")
    assert inst.key == "latkan_test_key"


def test_linux_socket_lives_in_private_runtime_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: os.PathLike[str]
) -> None:
    import latkan.singleinstance as si

    monkeypatch.setattr(si.sys, "platform", "linux")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert si._server_name("latkan-alice") == os.path.join(str(tmp_path), "latkan-alice")
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    assert si._server_name("latkan-alice") == "latkan-alice"  # Qt's temp dir fallback
    monkeypatch.setattr(si.sys, "platform", "darwin")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert si._server_name("latkan-alice") == "latkan-alice"  # macOS: per-user $TMPDIR via Qt
