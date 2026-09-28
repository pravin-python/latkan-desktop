"""Single-instance guard built on ``QLocalServer`` / ``QLocalSocket``.

The first Latkan process of a user becomes the *primary*: it listens on a local socket (a
named pipe on Windows, a Unix domain socket on macOS/Linux). Any later launch connects to it,
sends ``b"show"`` and exits; the primary then emits :pyattr:`SingleInstance.activated` so the
app can un-hide and nudge its charms.

Platform notes:

* Windows named pipes are machine-global, so the key embeds the user name and the server is
  created with ``UserAccessOption`` (only the owning user may connect).
* macOS keeps sockets in the per-user ``$TMPDIR``.
* Linux uses ``$XDG_RUNTIME_DIR`` (``/run/user/<uid>``, private to the user) when available,
  otherwise Qt's default temp directory.
* A crashed primary leaves a stale socket file on Unix; it is removed before listening.
* Unix socket paths are limited to 104 bytes on macOS (108 on Linux), and with
  ``UserAccessOption`` Qt binds inside a temporary ``<name>.XXXXXX/s`` directory first. macOS'
  ``$TMPDIR`` alone is ~48 characters, so keys are capped at 40 characters; longer keys are
  shortened with a hash suffix so they stay unique.
"""

from __future__ import annotations

import getpass
import hashlib
import logging
import os
import re
import sys

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

log = logging.getLogger(__name__)

__all__ = ["SingleInstance", "default_key", "sanitize_key"]

MESSAGE = b"show"
CONNECT_TIMEOUT_MS = 500
WRITE_TIMEOUT_MS = 500
_MAX_MESSAGE_BYTES = 64
_MAX_KEY_LEN = 40
_DIGEST_LEN = 8
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9_-]")


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "surrogatepass")).hexdigest()[:_DIGEST_LEN]


def sanitize_key(raw: str) -> str:
    """Return *raw* reduced to ``[A-Za-z0-9_-]`` and at most 40 characters (safe for socket paths).

    Over-long keys keep their start and get a hash of the full key appended, so distinct long
    keys stay distinct.
    """
    key = _UNSAFE_CHARS.sub("_", raw)
    if len(key) > _MAX_KEY_LEN:
        key = f"{key[: _MAX_KEY_LEN - _DIGEST_LEN - 1]}-{_digest(raw)}"
    return key or "latkan"


def _current_user() -> str:
    try:
        return getpass.getuser()
    except (KeyError, ImportError, OSError) as exc:  # no login name in env and no passwd entry
        log.debug("Could not determine the user name (%s); using a fallback", exc)
    getuid = getattr(os, "getuid", None)
    return str(getuid()) if getuid is not None else "user"


def default_key() -> str:
    """The per-user key, ``latkan-<user>``, sanitized.

    If sanitizing changed the user name (non-ASCII characters, spaces, ...) a short hash of the
    real name is appended so two different users can never collide on the same key.
    """
    user = _current_user()
    safe_user = _UNSAFE_CHARS.sub("_", user)
    if safe_user != user or not safe_user:
        safe_user = f"{safe_user[:20]}-{_digest(user)}"
    return sanitize_key(f"latkan-{safe_user}")


def _server_name(key: str) -> str:
    """Map a key to the name handed to Qt (a full socket path on Linux when a runtime dir exists)."""
    if sys.platform.startswith("linux"):
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
        if runtime_dir and os.path.isdir(runtime_dir):
            return os.path.join(runtime_dir, key)
    return key


class SingleInstance(QObject):
    """Detects an already-running Latkan for the current user and forwards activation to it.

    Usage::

        single = SingleInstance()
        if not single.is_primary():
            return 0                          # the running instance was asked to show itself
        single.activated.connect(on_show)     # later launches land here
    """

    activated = Signal()
    """Emitted in the primary whenever another instance starts."""

    def __init__(self, key: str | None = None, parent: QObject | None = None) -> None:
        """Create the guard.

        :param key: identifier shared by all instances that should exclude each other. Defaults
            to :func:`default_key` (includes the user name). Tests pass a unique key.
        """
        super().__init__(parent)
        self._key = sanitize_key(key) if key is not None else default_key()
        self._server_name = _server_name(self._key)
        self._server: QLocalServer | None = None
        self._client: QLocalSocket | None = None
        self._primary: bool | None = None

    @property
    def key(self) -> str:
        """The sanitized key."""
        return self._key

    @property
    def server_name(self) -> str:
        """The name (or, on Linux, full socket path) the local server listens on."""
        return self._server_name

    @property
    def listening(self) -> bool:
        """True when this (primary) instance is accepting activation requests."""
        return self._server is not None and self._server.isListening()

    def is_primary(self) -> bool:
        """Return True if this process is the primary instance.

        The first call decides and the answer is cached. If another instance is running it is
        sent ``b"show"`` and False is returned. Otherwise this instance starts listening; if that
        fails (unusual permissions, a squatted socket path, ...) the failure is logged and the
        process still runs as primary, just without single-instance protection.
        """
        if self._primary is None:
            if self._notify_running_instance():
                self._primary = False
            else:
                self._listen()
                self._primary = True
        return self._primary

    def close(self) -> None:
        """Stop listening (removes the socket file on Unix). Safe to call more than once."""
        server, self._server = self._server, None
        if server is not None:
            server.close()
            server.deleteLater()

    # ------------------------------------------------------------------ secondary side

    def _notify_running_instance(self) -> bool:
        sock = QLocalSocket(self)
        sock.connectToServer(self._server_name)
        if not sock.waitForConnected(CONNECT_TIMEOUT_MS):
            log.debug("No running instance on %r (%s)", self._server_name, sock.errorString())
            sock.abort()
            sock.deleteLater()
            return False
        # Keep a reference so the socket (and any bytes still in flight) outlives this call.
        self._client = sock
        sock.write(MESSAGE)
        if not sock.waitForBytesWritten(WRITE_TIMEOUT_MS) and sock.bytesToWrite() > 0:
            # The running instance is not reading (busy or hung). Qt keeps flushing on disconnect.
            log.warning(
                "The running instance did not accept the activation request within %d ms", WRITE_TIMEOUT_MS
            )
        sock.disconnectFromServer()
        if sock.state() != QLocalSocket.LocalSocketState.UnconnectedState:
            sock.waitForDisconnected(WRITE_TIMEOUT_MS)
        log.info("Another Latkan instance is already running; asked it to show its charms")
        return True

    # ------------------------------------------------------------------ primary side

    def _listen(self) -> None:
        # A connect failure with a leftover socket file means the previous primary died: remove it.
        QLocalServer.removeServer(self._server_name)
        server = QLocalServer(self)
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        server.newConnection.connect(self._on_new_connection)
        if not server.listen(self._server_name):
            log.warning(
                "Single-instance server could not listen on %r (%s); continuing as the primary "
                "instance without single-instance protection",
                self._server_name,
                server.errorString(),
            )
            server.deleteLater()
            return
        self._server = server
        log.debug("Single-instance server listening on %r", self._server_name)

    def _on_new_connection(self) -> None:
        server = self._server
        if server is None:
            return
        while server.hasPendingConnections():
            sock = server.nextPendingConnection()
            if sock is None:
                break
            self._watch(sock)

    def _watch(self, sock: QLocalSocket) -> None:
        received = bytearray()
        finished = False

        def consume() -> None:
            nonlocal finished
            if finished:
                return
            received.extend(bytes(sock.readAll()))
            if MESSAGE in received:
                finished = True
                log.info("Activation request received from another launch")
                sock.disconnectFromServer()
                self.activated.emit()
            elif len(received) > _MAX_MESSAGE_BYTES:
                finished = True
                log.warning("Ignoring unexpected single-instance message (%d bytes)", len(received))
                sock.abort()

        sock.readyRead.connect(consume)
        sock.disconnected.connect(consume)  # bytes that arrived together with the hang-up
        sock.disconnected.connect(sock.deleteLater)
        consume()  # data may already be buffered
