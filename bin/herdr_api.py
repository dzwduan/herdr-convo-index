"""Direct access to the herdr socket API.

The server speaks newline-delimited JSON over a unix socket and closes the
connection after answering a one-shot request, so requests get a fresh socket
while `events.subscribe` keeps one open and streams events on it.

Talking to the socket directly instead of shelling out to the `herdr` CLI keeps
the pane from forking a process on every poll, and lets the focus stream drive
updates rather than a timer.
"""

import json
import os
import socket

SOCKET_PATH = os.environ.get("HERDR_SOCKET_PATH") or ""
REQUEST_TIMEOUT_SEC = 3.0

# Events that can change which pane the index should be following.
FOCUS_EVENTS = ("pane.focused", "pane.agent_detected", "pane.closed")


def available():
    return bool(SOCKET_PATH) and os.path.exists(SOCKET_PATH)


def request(method, params=None):
    """One-shot call; returns the result object or None on any failure."""
    if not available():
        return None
    payload = json.dumps({"id": "convo-index", "method": method, "params": params or {}})
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(REQUEST_TIMEOUT_SEC)
            sock.connect(SOCKET_PATH)
            sock.sendall(payload.encode() + b"\n")
            buf = b""
            while b"\n" not in buf:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
    except (OSError, socket.timeout):
        return None
    line = buf.split(b"\n", 1)[0]
    try:
        return json.loads(line.decode("utf-8", "replace")).get("result")
    except ValueError:
        return None


class EventStream:
    """Long-lived subscription; `drain()` returns the event names that arrived.

    A dead socket is reported by `connected` so callers can fall back to polling
    instead of silently going stale.
    """

    def __init__(self, events=FOCUS_EVENTS):
        self.events = tuple(events)
        self.sock = None
        self.buf = b""
        self.connect()

    @property
    def connected(self):
        return self.sock is not None

    def connect(self):
        self.close()
        if not available():
            return False
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(REQUEST_TIMEOUT_SEC)
            sock.connect(SOCKET_PATH)
            sock.sendall(json.dumps({
                "id": "convo-index-sub",
                "method": "events.subscribe",
                "params": {"subscriptions": [{"type": name} for name in self.events]},
            }).encode() + b"\n")
            sock.setblocking(False)
        except (OSError, socket.timeout):
            return False
        self.sock = sock
        self.buf = b""
        return True

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def fileno(self):
        return self.sock.fileno() if self.sock is not None else None

    def drain(self):
        """Consume everything readable; returns the list of event names seen."""
        if self.sock is None:
            return []
        while True:
            try:
                chunk = self.sock.recv(65536)
            except BlockingIOError:
                break
            except OSError:
                self.close()
                return []
            if not chunk:
                self.close()
                return []
            self.buf += chunk
        names = []
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                message = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue
            name = message.get("event")
            if name:
                names.append(name)
        return names
