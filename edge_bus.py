"""Offline-first MQTT edge bus for the elderly fall monitor.

The hardware process must never block on a network operation.  ``EdgeBus``
therefore publishes immediately when a broker is connected and appends the
message to a JSONL outbox otherwise.  A small background flusher retries the
outbox after reconnecting.  The module is importable without paho-mqtt so
desktop tests and the web application remain hardware independent.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Callable

try:
    import paho.mqtt.client as mqtt
except Exception:  # pragma: no cover - optional dependency on desktop
    mqtt = None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class JsonlOutbox:
    """Durable FIFO queue with atomic compaction."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def enqueue(self, message: dict) -> None:
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        line = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def read(self, limit: int = 100) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        out: list[dict] = []
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if len(out) >= limit:
                            break
                        try:
                            item = json.loads(line)
                        except (TypeError, ValueError):
                            continue
                        if isinstance(item, dict):
                            out.append(item)
            except OSError:
                return []
        return out

    def remove_prefix(self, count: int) -> None:
        if count <= 0 or not os.path.exists(self.path):
            return
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
                    lines = fh.readlines()
                remaining = lines[count:]
                if remaining:
                    tmp = self.path + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as fh:
                        fh.writelines(remaining)
                    os.replace(tmp, self.path)
                else:
                    try:
                        os.remove(self.path)
                    except OSError:
                        pass
            except OSError:
                pass

    def __len__(self) -> int:
        if not os.path.exists(self.path):
            return 0
        try:
            with open(self.path, "rb") as fh:
                return sum(1 for line in fh if line.strip())
        except OSError:
            return 0


class EdgeBus:
    """MQTT publisher/subscriber with an offline outbox.

    MQTT is the data plane.  The caller can still use the returned ``mqtt``
    boolean in status snapshots to make broker health visible in the UI.
    """

    def __init__(
        self,
        *,
        room_id: str,
        device_id: str,
        outbox_path: str,
        host: str = "127.0.0.1",
        port: int = 1883,
        tls: bool = False,
        username: str | None = None,
        password: str | None = None,
        on_command: Callable[[dict], None] | None = None,
    ):
        self.room_id = room_id
        self.device_id = device_id
        self.host = host
        self.port = port
        self.tls = tls
        self.username = username
        self.password = password
        self.on_command = on_command
        self.outbox = JsonlOutbox(outbox_path)
        self.connected = False
        self._stop = threading.Event()
        self._client = None
        self._flush_thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def topic(self, kind: str, device_id: str | None = None) -> str:
        did = device_id or self.device_id
        return f"elder/v1/{self.room_id}/{did}/{kind}"

    def start(self) -> bool:
        if mqtt is None:
            return False
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"elder-{self.device_id}-{uuid.uuid4().hex[:8]}")
            if self.username:
                client.username_pw_set(self.username, self.password)
            if self.tls:
                client.tls_set()
            client.will_set(
                self.topic("heartbeat"),
                json.dumps({
                    "schema_version": 1,
                    "device_id": self.device_id,
                    "status": "offline",
                    "updated_at": utc_now_iso(),
                }, ensure_ascii=False),
                qos=1,
                retain=True,
            )
            client.on_connect = self._on_connect
            client.on_disconnect = self._on_disconnect
            client.on_message = self._on_message
            client.connect_async(self.host, self.port, 60)
            client.loop_start()
            self._client = client
            self._flush_thread = threading.Thread(target=self._flush_loop, name="mqtt-outbox", daemon=True)
            self._flush_thread.start()
            return True
        except Exception:
            self._client = None
            self.connected = False
            return False

    def stop(self) -> None:
        self._stop.set()
        client = self._client
        if client is not None:
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:
                pass
        self.connected = False

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        try:
            code = getattr(reason_code, "value", reason_code)
            self.connected = int(code) == 0
        except (TypeError, ValueError):
            self.connected = not bool(getattr(reason_code, "is_failure", False))
        if self.connected:
            client.subscribe(self.topic("command"), qos=1)
            self.publish_json(
                "heartbeat",
                {
                    "schema_version": 1,
                    "device_id": self.device_id,
                    "status": "online",
                    "updated_at": utc_now_iso(),
                },
                retain=True,
            )

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties=None):
        self.connected = False

    def _on_message(self, client, userdata, msg):
        if self.on_command is None:
            return
        try:
            data = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return
        if isinstance(data, dict):
            try:
                self.on_command(data)
            except Exception:
                pass

    def publish_json(
        self,
        kind: str,
        payload: dict,
        *,
        device_id: str | None = None,
        qos: int = 1,
        retain: bool = False,
    ) -> bool:
        message = {
            "topic": self.topic(kind, device_id=device_id),
            "payload": payload,
            "qos": qos,
            "retain": retain,
            "queued_at": utc_now_iso(),
        }
        if self._try_publish(message):
            return True
        self.outbox.enqueue(message)
        return False

    def _try_publish(self, message: dict) -> bool:
        client = self._client
        if not self.connected or client is None:
            return False
        try:
            info = client.publish(
                message["topic"],
                json.dumps(message["payload"], ensure_ascii=False, separators=(",", ":")),
                qos=int(message.get("qos", 1)),
                retain=bool(message.get("retain", False)),
            )
            return getattr(info, "rc", 1) == 0
        except Exception:
            self.connected = False
            return False

    def _flush_loop(self) -> None:
        while not self._stop.wait(1.0):
            if not self.connected:
                continue
            messages = self.outbox.read(50)
            if not messages:
                continue
            sent = 0
            for message in messages:
                if not self._try_publish(message):
                    break
                sent += 1
            if sent:
                self.outbox.remove_prefix(sent)

