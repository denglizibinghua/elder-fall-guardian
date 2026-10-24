"""Small server-to-browser WebSocket helper using only the Python stdlib."""

from __future__ import annotations

import base64
import hashlib
import json
import threading


def accept_key(client_key: str) -> str:
    raw = (client_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
    return base64.b64encode(hashlib.sha1(raw).digest()).decode("ascii")


def text_frame(value: object) -> bytes:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    length = len(payload)
    if length < 126:
        return b"\x81" + bytes([length]) + payload
    if length < 65536:
        return b"\x81\x7e" + length.to_bytes(2, "big") + payload
    return b"\x81\x7f" + length.to_bytes(8, "big") + payload


class Peer:
    def __init__(self, connection, writer):
        self.connection = connection
        self.writer = writer
        self.lock = threading.Lock()
        self.closed = False

    def send(self, value: object) -> bool:
        frame = text_frame(value)
        with self.lock:
            if self.closed:
                return False
            try:
                self.writer.write(frame)
                self.writer.flush()
                return True
            except OSError:
                self.closed = True
                return False


class WebSocketHub:
    def __init__(self):
        self._peers: set[Peer] = set()
        self._lock = threading.Lock()

    def add(self, connection, writer) -> Peer:
        peer = Peer(connection, writer)
        with self._lock:
            self._peers.add(peer)
        return peer

    def remove(self, peer: Peer) -> None:
        peer.closed = True
        with self._lock:
            self._peers.discard(peer)

    def broadcast(self, value: object) -> None:
        with self._lock:
            peers = list(self._peers)
        dead = []
        for peer in peers:
            if not peer.send(value):
                dead.append(peer)
        for peer in dead:
            self.remove(peer)

    def count(self) -> int:
        with self._lock:
            return len(self._peers)
