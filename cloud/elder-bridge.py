#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Elder Guardian 云端桥接器 v2 —— 板端同格式输出版。

订阅云端 mosquitto 的 elder/v1/#，做三件事：
  1. 报警事件写入 MySQL elder_guardian.events（event_id 幂等去重）；
  2. 状态快照降频落库 elder_guardian.state_samples（默认 60s 一条）；
  3. 输出**板端同格式文件**给云端 UI（默认 /opt/elder-ui/）：
       - status.json  ← 最近一条 state 消息原样落盘（板端 schema，UI 直读）
       - alarms.jsonl ← 新事件按板端行格式追加（缺文件时从 DB 重建最近 50 条）

云端 UI = 板端 app.py 的原样部署（仅改监听端口，nginx 反代 :8080）。
本模块不参与任何实时判定（云端只留档 + 展示）。
"""

import json
import os
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
import pymysql

MQTT_HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "8883"))
MQTT_USER = os.environ.get("MQTT_USER", "cloud")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "")
MQTT_CA = os.environ.get("MQTT_CA", "/opt/elder-bridge/ca.crt")

DB_HOST = os.environ.get("DB_HOST", "127.0.0.1")
DB_USER = os.environ.get("DB_USER", "elder")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
DB_NAME = os.environ.get("DB_NAME", "elder_guardian")

OUT_DIR = os.environ.get("OUT_DIR", "/opt/elder-ui")
STATUS_FILE = os.path.join(OUT_DIR, "status.json")
ALARMS_FILE = os.path.join(OUT_DIR, "alarms.jsonl")

STATE_DB_INTERVAL = 60.0
STATUS_PERIOD = 2.0

PLACEHOLDER = {
    "schema_version": 1,
    "waiting": True,
    "device_id": None,
    "seq": 0,
    "presence_state": False,
    "radar_targets": [],
    "button": None,
    "mqtt": False,
    "detector": {"state": "WAITING", "confidence": 0.0, "model_version": "unknown", "evidence": {}},
    "updated_at": None,
}


def atomic_write(path, data):
    tmp = "%s.tmp.%d.%d" % (path, os.getpid(), time.time_ns())
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, path)


class Bridge:
    def __init__(self):
        self.db = None
        self.latest_state = None
        self.last_state_db = 0.0
        self._connect_db()
        os.makedirs(OUT_DIR, exist_ok=True)
        if not os.path.exists(ALARMS_FILE):
            self._rebuild_alarms_file()
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id="elder-cloud-bridge"
        )
        self.client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
        self.client.tls_set(ca_certs=MQTT_CA)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    # ------------------------------------------------------------ MySQL
    def _connect_db(self):
        try:
            self.db = pymysql.connect(
                host=DB_HOST, user=DB_USER, password=DB_PASSWORD,
                database=DB_NAME, charset="utf8mb4", autocommit=True,
            )
        except Exception as exc:
            print("db connect failed:", exc, flush=True)
            self.db = None

    def _db_exec(self, sql, args=None):
        for _ in (1, 2):
            if self.db is None:
                self._connect_db()
            if self.db is None:
                return None
            try:
                cur = self.db.cursor()
                cur.execute(sql, args)
                rowcount = cur.rowcount
                cur.close()
                return rowcount
            except Exception as exc:
                print("db exec failed:", exc, flush=True)
                try:
                    self.db.close()
                except Exception:
                    pass
                self.db = None
        return None

    def _rebuild_alarms_file(self):
        if self.db is None:
            return
        try:
            with self.db.cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute(
                    "SELECT event_id, device_id, event_type, occurred_at, confidence, detail "
                    "FROM events ORDER BY id DESC LIMIT 50"
                )
                rows = cur.fetchall()
        except Exception as exc:
            print("rebuild alarms failed:", exc, flush=True)
            return
        tmp = "%s.tmp.%d.%d" % (ALARMS_FILE, os.getpid(), time.time_ns())
        with open(tmp, "w", encoding="utf-8") as fh:
            for row in reversed(rows):
                occurred = row.get("occurred_at")
                stamp = occurred.isoformat() if hasattr(occurred, "isoformat") else str(occurred or "")
                fh.write(json.dumps({
                    "time": stamp,
                    "occurred_at": stamp,
                    "type": row.get("event_type") or "FALL",
                    "event_type": row.get("event_type") or "FALL",
                    "detail": row.get("detail") or "",
                    "event_id": row.get("event_id"),
                    "device_id": row.get("device_id"),
                    "confidence": float(row["confidence"]) if row.get("confidence") is not None else None,
                }, ensure_ascii=False) + "\n")
        os.replace(tmp, ALARMS_FILE)
        print("alarms.jsonl rebuilt:", len(rows), "lines", flush=True)

    # ------------------------------------------------------------ MQTT
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        print("mqtt connected:", reason_code, flush=True)
        client.subscribe("elder/#", qos=1)

    def _on_message(self, client, userdata, msg):
        kind = msg.topic.rsplit("/", 1)[-1]
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        if kind == "state":
            self.latest_state = payload
            self._maybe_store_state(payload)
            self.flush_status()
        elif kind == "event":
            self._store_event(payload)

    def _maybe_store_state(self, payload):
        now = time.time()
        if now - self.last_state_db < STATE_DB_INTERVAL:
            return
        self.last_state_db = now
        detector = payload.get("detector") or {}
        self._db_exec(
            "INSERT INTO state_samples "
            "(device_id, seq, presence, temperature, humidity, detector_state, confidence, sampled_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                payload.get("device_id"), payload.get("seq", 0),
                1 if payload.get("presence_state") else 0,
                payload.get("temperature"), payload.get("humidity"),
                detector.get("state"), detector.get("confidence"),
                datetime.now(timezone.utc).replace(tzinfo=None),
            ),
        )

    def _store_event(self, payload):
        event_id = payload.get("event_id") or f"legacy-{payload.get('time', '')}-{str(payload.get('detail', ''))[:40]}"
        occurred = payload.get("occurred_at") or payload.get("time")
        try:
            normalized = datetime.fromisoformat(str(occurred).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            normalized = None
        evidence = payload.get("evidence")
        rowcount = self._db_exec(
            "INSERT IGNORE INTO events "
            "(event_id, device_id, room_id, event_type, occurred_at, confidence, detail, evidence, received_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                str(event_id)[:64], payload.get("device_id"), payload.get("room_id"),
                payload.get("event_type") or payload.get("type") or "FALL",
                normalized.replace(tzinfo=None) if normalized else None,
                payload.get("confidence"), str(payload.get("detail", ""))[:255],
                json.dumps(evidence, ensure_ascii=False) if evidence else None,
                datetime.now(timezone.utc).replace(tzinfo=None),
            ),
        )
        if rowcount == 1:  # 仅新事件追写到板端格式文件（幂等：重复上报不重复显示）
            line = dict(payload)
            line.setdefault("event_type", line.get("type", "FALL"))
            line.setdefault("type", line.get("event_type", "FALL"))
            with open(ALARMS_FILE, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
            print("event stored:", event_id, "detail:", str(payload.get("detail", ""))[:60], flush=True)

    # ------------------------------------------------------------ 板端格式输出
    def flush_status(self):
        atomic_write(STATUS_FILE, self.latest_state if self.latest_state else PLACEHOLDER)

    def run(self):
        self.client.connect(MQTT_HOST, MQTT_PORT, 60)
        self.client.loop_start()
        self.flush_status()
        while True:
            time.sleep(STATUS_PERIOD)
            self.flush_status()


if __name__ == "__main__":
    Bridge().run()
