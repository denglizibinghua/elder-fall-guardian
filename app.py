#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
老人跌倒监护 · 移动端多 tab Web 应用 app.py（单文件，仅标准库）
================================================================
应用层：手机浏览器（同 WiFi）打开 http://<派IP>:8080 的移动端应用，
6 个底部 tab：总览 / 轨迹 / 报警 / 设备 / 相册 / 关于。

部署步骤（树莓派 Pi 4B，系统 Python 3.13）：
  1) 复制本文件到派上：
       scp webapp/app.py admin@<派IP>:/home/admin/food_project/app.py
       （或 U 盘拷贝；本文件自身不含任何 IP/密码，访问一律用相对 URL）
  2) 新建 systemd 单元 /etc/systemd/system/webapp.service：
       [Unit]
       Description=老人监护移动端Web应用(:8080)
       After=network.target
       [Service]
       User=admin
       WorkingDirectory=/home/admin/food_project
       ExecStart=/usr/bin/python3 /home/admin/food_project/app.py
       Restart=always
       [Install]
       WantedBy=multi-user.target
     然后：
       sudo systemctl daemon-reload
       sudo systemctl enable --now webapp.service
  3) 停用旧看板，避免同抢 8080 端口：
       sudo systemctl disable --now dashboard.service
  4) 手机连同一 WiFi，浏览器打开 http://<派IP>:8080
     课程演示四步：存在 / 跌倒模拟 / 按键求助 / 断网本地报警。

数据源（与 dashboard.py 同目录约定，缺文件/坏文件永不崩溃）：
  状态 : 同目录 status.json（main 主循环每 2 秒写一次）
  报警 : 同目录 alarms.jsonl（fall_fusion 写，每行 {time,type,detail}）
  设备 : 同目录 devices.json（可缺省，形如 [{id,name,layer,interface,note}]）

路由（http.server 标准库，无第三方依赖、无 CDN、无外部 CSS/JS）：
  GET /             移动端 6-tab 单页（简体中文，viewport，底部 tab 栏）
  GET /api/status   与 dashboard.py 同形的状态 JSON
  GET /api/alarms   {"alarms": [...最近50条...], "count": N}
  GET /api/devices  {"devices": [...] 或 []}
  GET /api/export   报警 CSV 导出（text/csv，附件 alarms.csv，表头 time,type,detail）
  GET /api/photos   {"photos": [{name, mtime}...最近20张...], "count": N}
  GET /photo/<name> 单张 *_food.jpg（image/jpeg，basename+.jpg+BASE_DIR 越界校验）

数据契约（dashboard 兼容）：
  status.json 键：waiting(bool), presence_state(bool), radar_targets([{x,y,speed,dist}]),
    steps(int), temperature, humidity, temp_recorded_at, bpm, spo2,
    button(str: RELEASED/PRESSED/未知), take_photo_flag, updated_at
  alarms.jsonl 行：{time, type, detail}（同时兼容旧字段 message/timestamp）
"""

import glob
import base64
import hashlib
import json
import os
import select
import socket
import threading
import time
import re
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

try:
    from ws import WebSocketHub, accept_key
except Exception:
    class WebSocketHub:
        def __init__(self):
            self._peers = {}
            self._lock = threading.Lock()

        def add(self, connection, writer):
            peer = {"connection": connection, "writer": writer, "lock": threading.Lock(), "closed": False}
            with self._lock:
                self._peers[id(peer)] = peer
            return peer

        def remove(self, peer):
            peer["closed"] = True
            with self._lock:
                self._peers.pop(id(peer), None)

        def broadcast(self, value):
            payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            length = len(payload)
            if length < 126:
                frame = b"\x81" + bytes([length]) + payload
            elif length < 65536:
                frame = b"\x81\x7e" + length.to_bytes(2, "big") + payload
            else:
                frame = b"\x81\x7f" + length.to_bytes(8, "big") + payload
            with self._lock:
                peers = list(self._peers.values())
            for peer in peers:
                if peer["closed"]:
                    continue
                try:
                    with peer["lock"]:
                        peer["writer"].write(frame)
                        peer["writer"].flush()
                except OSError:
                    self.remove(peer)

        def count(self):
            with self._lock:
                return len(self._peers)

    def accept_key(client_key):
        raw = (client_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
        return base64.b64encode(hashlib.sha1(raw).digest()).decode("ascii")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATUS_FILE = os.path.join(BASE_DIR, "status.json")
ALARMS_FILE = os.path.join(BASE_DIR, "alarms.jsonl")
DEVICES_FILE = os.path.join(BASE_DIR, "devices.json")
ACK_FILE = os.path.join(BASE_DIR, "acknowledged.jsonl")
PORT = 8080
WS_HUB = WebSocketHub()
ACK_LOCK = threading.Lock()


def live_snapshot():
    return {
        "type": "snapshot",
        "status": load_status(),
        "alarms": load_alarms(50),
        "nodes": node_payload(),
    }


def _watch_live_files():
    """Push state changes to browsers without polling from the browser."""
    last_signature = None
    while True:
        try:
            status_mtime = os.path.getmtime(STATUS_FILE) if os.path.exists(STATUS_FILE) else 0
            alarm_mtime = os.path.getmtime(ALARMS_FILE) if os.path.exists(ALARMS_FILE) else 0
            signature = (status_mtime, alarm_mtime)
            if signature != last_signature:
                WS_HUB.broadcast(live_snapshot())
                last_signature = signature
        except Exception:
            pass
        time.sleep(0.35)


# ---------------------------------------------------------------- 状态读取
def _to_float(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _to_int(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _safe_mtime(path):
    try:
        return os.path.getmtime(path) if os.path.exists(path) else None
    except OSError:
        return None


def normalize_button(raw):
    """统一为 RELEASED / PRESSED / 未知（兼容旧中文 按下/松开 与布尔量）。"""
    if isinstance(raw, bool):
        return "PRESSED" if raw else "RELEASED"
    s = str(raw).strip() if raw is not None else ""
    if s in ("PRESSED", "按下", "按住", "1", "HIGH", "high"):
        return "PRESSED"
    if s in ("RELEASED", "松开", "释放", "弹起", "0", "LOW", "low"):
        return "RELEASED"
    su = s.upper()
    if su == "PRESSED":
        return "PRESSED"
    if su == "RELEASED":
        return "RELEASED"
    return "未知"


def load_status():
    """读 status.json；缺失/损坏时返回等待占位，不抛异常。"""
    placeholder = {
        "schema_version": 1,
        "waiting": True,
        "device_id": None,
        "seq": 0,
        "presence_state": False,
        "presence_text": "等待树莓派数据",
        "radar_targets": [],
        "steps": 0,
        "temperature": None,
        "humidity": None,
        "temp_recorded_at": None,
        "bpm": 0,
        "spo2": 0,
        "bpm_valid": False,
        "spo2_valid": False,
        "button": "未知",
        "take_photo_flag": False,
        "detector": {
            "state": "WAITING",
            "confidence": 0.0,
            "model_version": "unknown",
            "motion_speed_cm_s": 0.0,
            "evidence": {},
        },
        "updated_at": None,
    }
    try:
        if not os.path.exists(STATUS_FILE):
            return placeholder
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return placeholder
        targets = raw.get("radar_targets", [])
        if not isinstance(targets, list):
            targets = []
        clean_targets = []
        for t in targets[:3]:
            if not isinstance(t, dict):
                continue
            try:
                _dist = float(t.get("dist", 0))
                if _dist <= 0:
                    continue
                clean_targets.append({
                    "x": float(t.get("x", 0)),
                    "y": float(t.get("y", 0)),
                    "speed": float(t.get("speed", 0)),
                    "dist": _dist,
                })
            except (TypeError, ValueError):
                continue
        presence_sensor = bool(raw.get("presence_state", False))
        # LD2450 is a useful presence fallback when LD2410 GPIO data lags.
        presence = presence_sensor or bool(clean_targets)
        btn = raw.get("button", raw.get("button_state", "未知"))
        spo2 = _to_float(raw.get("spo2", 0) or 0)
        bpm = _to_int(raw.get("bpm", 0))
        bpm_valid = bool(raw.get("bpm_valid", 30 <= bpm <= 200)) and 30 <= bpm <= 200
        spo2_valid = bool(raw.get("spo2_valid", spo2 is not None and 70 <= spo2 <= 100))
        detector = raw.get("detector", {})
        if not isinstance(detector, dict):
            detector = {}
        evidence = detector.get("evidence", {})
        if not isinstance(evidence, dict):
            evidence = {}
        motion_speed = _to_float(
            detector.get("motion_speed_cm_s", evidence.get("motion_speed_cm_s", 0.0))
        )
        out = {
            "schema_version": _to_int(raw.get("schema_version", 1)),
            "waiting": False,
            "device_id": raw.get("device_id"),
            "room_id": raw.get("room_id"),
            "seq": _to_int(raw.get("seq", 0)),
            "presence_state": presence,
            "presence_sensor": presence_sensor,
            "presence_text": "存在" if presence else "无人",
            "radar_targets": clean_targets,
            "steps": _to_int(raw.get("steps", 0)),
            "temperature": _to_float(raw.get("temperature")),
            "humidity": _to_float(raw.get("humidity")),
            "temp_recorded_at": raw.get("temp_recorded_at"),
            "bpm": bpm if bpm_valid else 0,
            "spo2": spo2 if spo2_valid and spo2 is not None else 0,
            "bpm_valid": bpm_valid,
            "spo2_valid": spo2_valid,
            "button": normalize_button(btn),
            "take_photo_flag": bool(raw.get("take_photo_flag", False)),
            "mqtt": bool(raw.get("mqtt", False)),
            "outbox_pending": _to_int(raw.get("outbox_pending", 0)),
            "detector": {
                "state": str(detector.get("state", "WAITING")),
                "confidence": _to_float(detector.get("confidence", 0.0)) or 0.0,
                "model_version": str(detector.get("model_version", "unknown")),
                "motion_speed_cm_s": max(0.0, motion_speed or 0.0),
                "evidence": evidence,
            },
            "health": raw.get("health", {}),
            "updated_at": raw.get("updated_at"),
        }
        return out
    except Exception:
        return placeholder


# ---------------------------------------------------------------- 报警与确认记录
def _clean_control_text(value):
    """Remove NUL and non-printing control bytes before parsing/exporting data."""
    if value is None:
        return ""
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(value))


def alarm_id(item):
    if not isinstance(item, dict):
        return ""
    return str(
        item.get("event_id")
        or item.get("id")
        or item.get("time")
        or item.get("timestamp")
        or item.get("detail")
        or item.get("message")
        or ""
    )


def load_acknowledgements():
    records = {}
    if not os.path.exists(ACK_FILE):
        return records
    try:
        with open(ACK_FILE, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = _clean_control_text(raw).strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                if isinstance(item, dict) and item.get("event_id"):
                    records[str(item["event_id"])] = item
    except OSError:
        pass
    return records


def acknowledge_alarm(event_id, actor="现场操作员"):
    event_id = _clean_control_text(event_id).strip()
    if not event_id:
        return False
    record = {
        "event_id": event_id,
        "actor": _clean_control_text(actor).strip() or "现场操作员",
        "acknowledged_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with ACK_LOCK:
            with open(ACK_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- 报警读取
def load_alarms(limit=50):
    """tail 最后 limit 行 alarms.jsonl；缺文件/坏行不崩溃（坏行包成 message 行）。"""
    if not os.path.exists(ALARMS_FILE):
        return []
    try:
        with open(ALARMS_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        tail = lines[-limit:] if len(lines) > limit else lines
        out = []
        for line in tail:
            line = _clean_control_text(line).strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    out.append(obj)
                else:
                    out.append({"message": line, "invalid": True})
            except Exception:
                out.append({"message": line, "invalid": True})
        acknowledgements = load_acknowledgements()
        for item in out:
            event_key = alarm_id(item)
            ack = acknowledgements.get(event_key)
            item["acknowledged"] = bool(ack)
            if ack:
                item["acknowledged_at"] = ack.get("acknowledged_at")
                item["acknowledged_by"] = ack.get("actor")
        return out
    except Exception:
        return []


def latest_alarm_ts(alarms):
    """从报警里提取最新时间戳（秒）；解析失败返回 None。"""
    if not alarms:
        return None
    last = alarms[-1]
    if not isinstance(last, dict):
        return None
    for key in ("timestamp", "time", "created_at", "recorded_at", "ts"):
        v = last.get(key)
        if v is None:
            continue
        try:
            if isinstance(v, (int, float)):
                return float(v / 1000.0) if v > 1e12 else float(v)
            s = str(v)
            iso = s.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            continue
    return None


# ---------------------------------------------------------------- CSV 导出
def _csv_field(v):
    """CSV 字段加引号，内部双引号翻倍（RFC 4180 风格）。"""
    s = _clean_control_text(v)
    return '"' + s.replace('"', '""') + '"'


def _alarm_cells(it):
    """从单条报警提取 (time, type, detail)，兼容旧字段 message/timestamp。"""
    if not isinstance(it, dict):
        return ("", "", _clean_control_text(it))
    t = it.get("time", it.get("timestamp", it.get("created_at", "")))
    ty = it.get("type", it.get("alert_type", ""))
    if it.get("detail") is not None:
        d = it.get("detail")
    elif it.get("message") is not None:
        d = it.get("message")
    else:
        try:
            d = json.dumps(it, ensure_ascii=False)
        except Exception:
            d = str(it)
    if t is None:
        t = ""
    if ty is None:
        ty = ""
    if d is None:
        d = ""
    return (_clean_control_text(t), _clean_control_text(ty), _clean_control_text(d))


def build_alarms_csv(alarms):
    """报警列表 -> CSV 文本（表头 time,type,detail，每条一行，全字段引号包裹）。"""
    lines = ["time,type,detail"]
    for it in alarms or []:
        t, ty, d = _alarm_cells(it)
        lines.append(",".join((_csv_field(t), _csv_field(ty), _csv_field(d))))
    return "\r\n".join(lines) + "\r\n"


# ---------------------------------------------------------------- 设备读取
def load_devices():
    """读 devices.json -> [{id,name,layer,interface,note}]；缺省/损坏返回 []。"""
    try:
        if not os.path.exists(DEVICES_FILE):
            return []
        with open(DEVICES_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            raw = raw.get("devices", [])
        if not isinstance(raw, list):
            return []
        out = []
        for d in raw:
            if not isinstance(d, dict):
                continue
            out.append({
                "id": str(d.get("id", "")),
                "name": str(d.get("name", "")),
                "layer": str(d.get("layer", "")),
                "interface": str(d.get("interface", "")),
                "note": str(d.get("note", "")),
            })
        return out
    except Exception:
        return []


def load_nodes():
    """Expose the local Raspberry Pi as a node for the unified device contract."""
    status = load_status()
    updated_at = status.get("updated_at")
    age_s = None
    if updated_at:
        try:
            dt = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_s = round(max(0.0, time.time() - dt.timestamp()), 1)
        except (TypeError, ValueError, OverflowError):
            age_s = None
    online = not status.get("waiting") and (age_s is None or age_s <= 5.0)
    return [{
        "id": status.get("device_id") or "pi-local",
        "room_id": status.get("room_id") or "未分配房间",
        "status": "online" if online else "offline",
        "age_s": age_s,
        "last_seen": updated_at,
        "seq": status.get("seq", 0),
        "presence_state": status.get("presence_state"),
        "detector": status.get("detector", {}),
        "local": True,
        "health": status.get("health", {}),
    }]


def node_payload():
    status = load_status()
    return {
        "connected": bool(status.get("mqtt", False)),
        "enabled": bool(status.get("mqtt", False)),
        "nodes": load_nodes(),
    }


# ---------------------------------------------------------------- 照片读取
def load_photos(limit=20):
    """扫 BASE_DIR 下 *_food.jpg，按 mtime 倒序取前 limit 个；缺目录/无文件返回 []。"""
    try:
        if not os.path.isdir(BASE_DIR):
            return []
        paths = glob.glob(os.path.join(BASE_DIR, "*_food.jpg"))
        items = []
        for p in paths:
            try:
                if not os.path.isfile(p):
                    continue
                mt = os.path.getmtime(p)
                items.append({
                    "name": os.path.basename(p),
                    "mtime": datetime.fromtimestamp(mt).isoformat(timespec="seconds"),
                    "valid": os.path.getsize(p) > 0,
                    "size": os.path.getsize(p),
                    "_mt": mt,
                })
            except Exception:
                continue
        items.sort(key=lambda d: d["_mt"], reverse=True)
        return [
            {
                "name": d["name"],
                "mtime": d["mtime"],
                "valid": d["valid"],
                "size": d["size"],
            }
            for d in items[:limit]
        ]
    except Exception:
        return []


# ---------------------------------------------------------------- 页面模板
PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<link rel="manifest" href="/manifest.json">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#070d1a">
<title>老人跌倒监护</title>
<style>
:root{--bg:#070d1a;--bg2:#0b1428;--card:rgba(148,163,184,.07);--line:rgba(148,163,184,.18);--txt:#e8eff8;--mut:#8fa1b8;--acc:#22d3ee;--red:#f87171;--grn:#34d399;--amb:#fbbf24}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC","Hiragino Sans GB","Microsoft YaHei",system-ui,sans-serif;background:radial-gradient(120% 60% at 50% -10%,#12314a 0%,transparent 55%),linear-gradient(180deg,#0a1326 0%,#070d1a 60%,#060b16 100%);color:var(--txt);padding:12px 12px 92px;max-width:640px;margin-left:auto;margin-right:auto;line-height:1.5;min-height:100vh}
header{position:sticky;top:0;background:rgba(7,13,26,.88);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);padding:10px 0 10px;z-index:5;border-bottom:1px solid var(--line)}
h1{font-size:19px;margin:2px 0 8px;letter-spacing:.04em}
h1::before{content:"";display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--acc);box-shadow:0 0 10px var(--acc);margin-right:8px;vertical-align:2px}
.badge{display:inline-block;padding:6px 13px;border-radius:999px;font-size:13px;line-height:1.4;margin:2px 6px 2px 0;border:1px solid var(--line);font-variant-numeric:tabular-nums}
.ok{background:rgba(52,211,153,.12);color:#6ee7b7;border-color:rgba(52,211,153,.4)}.warn{background:rgba(251,191,36,.12);color:#fcd34d;border-color:rgba(251,191,36,.4)}.bad{background:rgba(248,113,113,.14);color:#fca5a5;border-color:rgba(248,113,113,.45)}.info{background:rgba(34,211,238,.1);color:#67e8f9;border-color:rgba(34,211,238,.35)}
.gray{background:rgba(148,163,184,.1);color:#a8b8cc;border-color:var(--line)}
#fallBanner{display:none;background:linear-gradient(180deg,rgba(248,113,113,.22),rgba(248,113,113,.12));border:1px solid rgba(248,113,113,.55);color:#fff;font-weight:bold;text-align:center;padding:14px;border-radius:14px;margin:10px 0;font-size:17px;box-shadow:0 0 24px rgba(248,113,113,.25)}
.card{background:linear-gradient(180deg,rgba(255,255,255,.05),rgba(255,255,255,.015));border:1px solid var(--line);border-radius:16px;padding:16px;margin:14px 0;box-shadow:0 8px 28px rgba(0,0,0,.35)}
.card h2{font-size:13px;margin:0 0 12px;color:var(--acc);letter-spacing:.14em;font-weight:700}
.card h2::after{content:"";display:block;height:1px;background:linear-gradient(90deg,rgba(34,211,238,.5),transparent);margin-top:8px}
section.tab{padding:2px 0}
.hero{text-align:center;padding:22px 14px 18px}
.hero-status{font-size:56px;font-weight:800;line-height:1.15;letter-spacing:.04em}
.hero-status::before{content:"";display:inline-block;width:16px;height:16px;border-radius:50%;margin-right:12px;vertical-align:6px;background:currentColor}
.hero.present .hero-status{color:var(--grn);text-shadow:0 0 28px rgba(52,211,153,.55)}
.hero.present .hero-status::before{background:var(--grn);box-shadow:0 0 14px var(--grn),0 0 34px rgba(52,211,153,.7)}
.hero.absent .hero-status{color:#b7c4d6;text-shadow:0 0 18px rgba(148,163,184,.3)}
.hero.absent .hero-status::before{background:#64748b;box-shadow:0 0 10px rgba(100,116,139,.8)}
.hero.waiting .hero-status{color:#94a3b8;font-size:30px;text-shadow:none}
.hero.waiting .hero-status::before{background:var(--amb);box-shadow:0 0 12px var(--amb)}
.hero.alarm .hero-status{color:var(--red);text-shadow:0 0 28px rgba(248,113,113,.6)}
.hero.alarm .hero-status::before{background:var(--red);box-shadow:0 0 14px var(--red),0 0 34px rgba(248,113,113,.7)}
.hero-time{font-size:13px;color:var(--mut);margin-top:8px;font-variant-numeric:tabular-nums}
.row{display:flex;flex-wrap:wrap;gap:10px}
.kv{flex:1 1 140px;background:rgba(7,13,26,.55);border:1px solid var(--line);border-radius:12px;padding:12px;min-height:70px}
.kv .k{font-size:12px;color:var(--mut)}.kv .v{font-size:23px;margin-top:3px;font-variant-numeric:tabular-nums;font-weight:700}
table{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}
th,td{border-bottom:1px solid var(--line);padding:9px 4px;text-align:left;word-break:break-all}
th{color:var(--mut);font-size:12px;letter-spacing:.06em}
canvas{width:100%;background:#050b18;border:1px solid var(--line);border-radius:12px}
#roomMap{height:280px}
#speedCv{height:150px}
.small{font-size:12px;color:var(--mut)}
.tnum{font-variant-numeric:tabular-nums}
.btn{display:inline-block;min-height:48px;line-height:24px;padding:12px 22px;border-radius:12px;background:rgba(34,211,238,.12);color:#67e8f9;font-size:16px;font-weight:bold;text-decoration:none;border:1px solid rgba(34,211,238,.5)}
.btn:active{background:rgba(34,211,238,.25)}
.dev{border:1px solid var(--line);border-radius:12px;padding:12px;margin:8px 0;background:rgba(7,13,26,.55)}
.dev .nm{font-size:16px;font-weight:bold}
.dev .meta{font-size:13px;color:var(--mut);margin-top:4px}
nav#tabbar{position:fixed;left:0;right:0;bottom:0;display:flex;background:rgba(9,16,32,.94);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);border-top:1px solid var(--line);padding-bottom:env(safe-area-inset-bottom);z-index:10}
nav#tabbar button{flex:1;border:0;background:none;color:#7d8ea6;font-size:16px;min-height:64px;padding:10px 4px 12px;cursor:pointer;border-top:3px solid transparent}
nav#tabbar button.active{color:#fff;background:rgba(34,211,238,.08);font-weight:800;border-top-color:var(--acc)}
section.tab{display:none}
section.tab.active{display:block}
.arch div{padding:9px 10px;background:rgba(7,13,26,.55);border:1px solid var(--line);border-radius:10px;margin:6px 0;font-size:14px}
.pgrid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
.pgrid img{width:100%;height:90px;object-fit:cover;border-radius:10px;background:#050b18;cursor:pointer;border:1px solid var(--line)}
.latestImg{width:100%;max-height:220px;object-fit:cover;border-radius:12px;cursor:pointer;border:1px solid var(--line)}
#photoOverlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.92);z-index:50;align-items:center;justify-content:center;padding:12px}
#photoOverlay.active{display:flex}
#photoOverlay img{max-width:100%;max-height:100%;border-radius:8px}
.tl{position:relative;margin:10px 0 2px;padding-left:24px;font-variant-numeric:tabular-nums}
.tl::before{content:"";position:absolute;left:7px;top:6px;bottom:6px;width:2px;background:linear-gradient(180deg,var(--acc),rgba(34,211,238,.08));border-radius:2px}
.tl-item{position:relative;background:rgba(7,13,26,.55);border:1px solid var(--line);border-radius:12px;padding:10px 12px;margin:0 0 10px}
.tl-item::before{content:"";position:absolute;left:-22px;top:14px;width:10px;height:10px;border-radius:50%;background:var(--acc);box-shadow:0 0 8px var(--acc)}
.tl-item.crit::before{background:var(--red);box-shadow:0 0 10px var(--red)}
.tl-item.warn2::before{background:var(--amb);box-shadow:0 0 10px var(--amb)}
.tl-time{font-size:12px;color:var(--acc);font-weight:700}
.tl-type{font-size:14px;font-weight:700;margin:2px 0}
.tl-detail{font-size:13px;color:#c6d3e4;word-break:break-all}
#trackList{font-variant-numeric:tabular-nums}
#trackList div{padding:7px 10px;background:rgba(7,13,26,.55);border:1px solid var(--line);border-radius:10px;margin:6px 0;font-size:13px}
.map-legend{display:flex;gap:12px;flex-wrap:wrap;margin-top:8px}
.map-legend span{font-size:12px;color:var(--mut)}
.map-legend i{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:5px;font-style:normal}
</style>
</head>
<body>
<header>
<h1>老人跌倒监护</h1>
<div><span id="netBadge" class="badge info">加载中…</span><span id="mqttBadge" class="badge gray" style="display:none">网关离线</span><span id="clockBadge" class="badge info">—</span></div>
<div id="fallBanner">⚠ 跌倒报警！请立即查看老人情况 <span id="fallAge"></span> <button id="dismissBtn" style="margin-left:10px;min-height:48px;padding:12px 20px;font-size:16px;border:0;border-radius:10px;background:#fff;color:#7f1d1d;font-weight:bold;cursor:pointer">已知晓</button></div>
</header>

<section id="tab-overview" class="tab active">
<div class="card hero waiting" id="heroCard"><div class="hero-status" id="heroStatus">—</div><div class="hero-time" id="heroTime"></div></div>
<div class="card"><h2>人员存在</h2><div class="row">
<div class="kv"><div class="k">状态</div><div class="v" id="ovPresence">—</div></div>
<div class="kv"><div class="k">雷达目标数</div><div class="v" id="ovTargets">—</div></div>
<div class="kv"><div class="k">求助按键</div><div class="v" id="ovButton">—</div></div>
</div></div>
<div class="card"><h2>温湿度 / 计步</h2><div class="row">
<div class="kv"><div class="k">温度 ℃</div><div class="v" id="ovTemp">—</div></div>
<div class="kv"><div class="k">湿度 %</div><div class="v" id="ovHum">—</div></div>
<div class="kv"><div class="k">步数</div><div class="v" id="ovSteps">—</div></div>
</div><div class="small" id="ovTempTime"></div></div>
<div class="card"><h2>心率血氧</h2><div class="row">
<div class="kv"><div class="k">心率 BPM</div><div class="v" id="ovBpm">—</div></div>
<div class="kv"><div class="k">血氧 SpO2 %</div><div class="v" id="ovSpo2">—</div></div>
</div><div class="small">MAX30102 未佩戴（或读数野值）时显示“未佩戴”。</div></div>
<div class="card" id="latestCard" style="display:none"><h2>最近抓拍</h2><img id="latestImg" class="latestImg" loading="lazy" alt="最近抓拍"><div class="small" id="latestTime"></div></div>
</section>

<section id="tab-track" class="tab">
<div class="card"><h2>房间俯视图（4m × 4m）</h2>
<canvas id="roomMap" width="480" height="480"></canvas>
<div class="map-legend"><span><i style="background:#22d3ee;box-shadow:0 0 6px #22d3ee"></i>正常目标</span><span><i style="background:#f87171;box-shadow:0 0 6px #f87171"></i>报警冻结</span><span>雷达在下方中点 ★</span></div>
<div class="small">圆点带余晖拖尾为近 ~20 次位置；报警时红点定格。</div></div>
<div class="card"><h2>雷达目标（LD2450）</h2>
<div id="trackList" class="small">等待数据…</div>
<div class="small">坐标 x/y 单位毫米；径向速度=朝向/远离雷达分量（横走≈0），报警判定用位移速度（见速度曲线卡片）。</div></div>
<div class="card"><h2>速度曲线（第一目标 · 位移速度）</h2>
<canvas id="speedCv" width="600" height="150"></canvas>
<div class="small">曲线=两帧坐标位移算出的真速度（横走也可见）；100 cm/s 红色虚线为静止分界参考，非报警线；报警 = 1m/s 活动后 3 秒内骤停 + 静止 3 秒。</div></div>
</section>

<section id="tab-alarms" class="tab">
<div class="card"><h2>报警记录 <span id="alarmCount" class="small"></span></h2>
<div style="margin:0 0 10px"><a class="btn" href="/api/export">导出CSV</a></div>
<div id="alarmTimeline" class="tl"></div>
<div id="alarmTable" class="small">加载中…</div></div>
</section>

<section id="tab-devices" class="tab">
<div class="card"><h2>设备列表</h2>
<div id="devList" class="small">加载中…</div></div>
</section>

<section id="tab-photos" class="tab">
<div class="card"><h2>报警抓拍 <span id="photoCount" class="small"></span></h2>
<div id="photoGrid" class="pgrid small">加载中…</div></div>
</section>

<section id="tab-about" class="tab">
<div class="card"><h2>系统架构（四层）</h2><div class="arch">
<div>感知层：LD2450 轨迹雷达 / LD2410 存在雷达 / MPU6050 / DHT11 / MAX30102 / 按键</div>
<div>网络层：同 WiFi 局域网直连，手机浏览器打开即可</div>
<div>平台层：树莓派边缘判定（速度骤降沿检测：活动后 3 秒内静止沿 + 静止 3 秒，阈值 rules.json 可调）与本地文件存储</div>
<div>应用层：本页面（总览 / 轨迹 / 报警 / 设备 / 相册 / 关于），判决不依赖存在检测</div>
</div>
<div class="small">离线优先：断网本地照常判断与报警，不依赖外网，不存密码。<br>演示四步：存在 / 跌倒模拟 / 按键求助 / 断网本地报警。</div></div>
</section>

<div id="photoOverlay"><img id="photoFull" alt="抓拍大图"></div>
<nav id="tabbar">
<button data-tab="overview" class="active">总览</button>
<button data-tab="track">轨迹</button>
<button data-tab="alarms">报警</button>
<button data-tab="devices">设备</button>
<button data-tab="photos">相册</button>
<button data-tab="about">关于</button>
</nav>

<script>
var activeTab = "overview";
var speedHist = [];
var lastAlarmId = "";
var dismissedAlarmId = "";
var currentBannerId = "";
var audioCtx = null;
var trails = [[], [], []];
var alarmActive = false;
var dispPrev = {ok:false, x:0, y:0, t:0};
var dispEma = 0;
document.querySelectorAll("#tabbar button").forEach(function(b){
  b.addEventListener("click", function(){
    activeTab = b.getAttribute("data-tab");
    document.querySelectorAll("#tabbar button").forEach(function(x){ x.classList.remove("active"); });
    b.classList.add("active");
    document.querySelectorAll("section.tab").forEach(function(s){ s.classList.remove("active"); });
    document.getElementById("tab-" + activeTab).classList.add("active");
    ensureAudio();
    tick();
  });
});
document.getElementById("dismissBtn").addEventListener("click", function(){
  dismissedAlarmId = currentBannerId || lastAlarmId || dismissedAlarmId;
  alarmActive = false;
  document.getElementById("fallBanner").style.display = "none";
});
function ensureAudio(){
  try{
    if(!audioCtx && (window.AudioContext || window.webkitAudioContext)){
      audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    }
    if(audioCtx && audioCtx.state === "suspended"){ audioCtx.resume(); }
  }catch(e){}
}
function beep(){
  try{
    ensureAudio();
    if(!audioCtx) return;
    var o = audioCtx.createOscillator(), g = audioCtx.createGain();
    o.type = "sine"; o.frequency.value = 880;
    g.gain.value = 0.25;
    o.connect(g); g.connect(audioCtx.destination);
    var t = audioCtx.currentTime;
    [0, 0.25, 0.5].forEach(function(off){
      o = audioCtx.createOscillator(); g = audioCtx.createGain();
      o.type = "sine"; o.frequency.value = 880;
      o.connect(g); g.connect(audioCtx.destination);
      o.start(t + off); o.stop(t + off + 0.18);
    });
  }catch(e){}
}
function esc(s){ return String(s===undefined||s===null?"":s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;"); }
function fmt(v, d){ if(v===null||v===undefined||v===""||isNaN(Number(v))) return "—"; return Number(v).toFixed(d===undefined?1:d); }
function alarmId(it){
  if(!it || typeof it !== "object") return "";
  return [it.time||it.timestamp||it.created_at||"", it.type||it.alert_type||"", it.detail||it.message||JSON.stringify(it)].join("|");
}
function drawRoom(targets, frozen){
  var c = document.getElementById("roomMap");
  if(!c) return;
  var ctx = c.getContext("2d"), W = c.width, H = c.height;
  ctx.clearRect(0,0,W,H);
  var pad = 26, iw = W-pad*2, ih = H-pad*2, HALF = 2000;
  function px(x){ x = Math.max(-HALF, Math.min(HALF, x)); return pad + (x/HALF*0.5+0.5)*iw; }
  function py(y){ y = Math.max(-HALF, Math.min(HALF, y)); return pad + (0.5-y/HALF*0.5)*ih; }
  ctx.strokeStyle = "rgba(148,163,184,.14)"; ctx.lineWidth = 1;
  for(var g=-2000; g<=2000; g+=1000){
    ctx.beginPath(); ctx.moveTo(px(g),py(-2000)); ctx.lineTo(px(g),py(2000)); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(px(-2000),py(g)); ctx.lineTo(px(2000),py(g)); ctx.stroke();
  }
  ctx.strokeStyle = "rgba(34,211,238,.55)"; ctx.lineWidth = 2;
  ctx.strokeRect(px(-2000), py(2000), px(2000)-px(-2000), py(-2000)-py(2000));
  ctx.fillStyle = "rgba(143,161,184,.9)"; ctx.font = "11px sans-serif";
  ctx.fillText("4m", px(0)-8, py(2000)-5); ctx.fillText("4m", px(-2000)+4, py(0)-5);
  var rx = px(0), ry = py(-2000);
  ctx.fillStyle = "#22d3ee"; ctx.font = "13px sans-serif"; ctx.textAlign = "center";
  ctx.fillText("★", rx, ry+16); ctx.textAlign = "left";
  (targets||[]).slice(0,3).forEach(function(t, i){
    var X = px(t.x||0), Y = py(t.y||0);
    var tr = trails[i] || (trails[i] = []);
    tr.forEach(function(p, k){
      var a = (k+1)/tr.length;
      ctx.beginPath(); ctx.arc(px(p.x), py(p.y), 2+a*2.5, 0, 6.283);
      ctx.fillStyle = frozen ? ("rgba(248,113,113,"+(0.08+a*0.4)+")") : ("rgba(34,211,238,"+(0.08+a*0.4)+")");
      ctx.fill();
    });
    var col = frozen ? "#f87171" : (i===0 ? "#22d3ee" : (i===1 ? "#a78bfa" : "#34d399"));
    ctx.save(); ctx.shadowColor = col; ctx.shadowBlur = 16;
    ctx.beginPath(); ctx.arc(X, Y, 8, 0, 6.283); ctx.fillStyle = col; ctx.fill();
    ctx.restore();
    ctx.beginPath(); ctx.arc(X, Y, 8, 0, 6.283); ctx.fillStyle = "rgba(255,255,255,.85)"; ctx.fill();
    ctx.beginPath(); ctx.arc(X, Y, 3.2, 0, 6.283); ctx.fillStyle = "#06121f"; ctx.fill();
    if(frozen){
      ctx.strokeStyle = "rgba(248,113,113,.85)"; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(X, Y, 14, 0, 6.283); ctx.stroke();
      ctx.strokeStyle = "rgba(248,113,113,.35)";
      ctx.beginPath(); ctx.arc(X, Y, 21, 0, 6.283); ctx.stroke(); ctx.lineWidth = 1;
    }
    ctx.fillStyle = frozen ? "#fca5a5" : "#c9f6ff"; ctx.font = "bold 12px sans-serif";
    var sp = Number(t.speed)||0;
    ctx.fillText((i+1)+"号 "+sp.toFixed(0)+"cm/s", Math.min(X+13, W-92), Math.max(Y-10, 12));
  });
  if(!(targets||[]).length){
    ctx.fillStyle = "rgba(143,161,184,.75)"; ctx.font = "13px sans-serif"; ctx.textAlign = "center";
    ctx.fillText("当前无人 / 无有效目标", W/2, H/2); ctx.textAlign = "left";
  }
}
function drawCurve(){
  var c = document.getElementById("speedCv"), ctx = c.getContext("2d");
  ctx.clearRect(0,0,c.width,c.height);
  ctx.strokeStyle = "rgba(148,163,184,.25)"; ctx.beginPath(); ctx.moveTo(0,0); ctx.lineTo(0,c.height); ctx.moveTo(0,c.height-1); ctx.lineTo(c.width,c.height-1); ctx.stroke();
  var maxV = 150, y = c.height - (100/maxV)*c.height;
  ctx.strokeStyle = "#f87171"; ctx.setLineDash([5,4]); ctx.beginPath(); ctx.moveTo(0,y); ctx.lineTo(c.width,y); ctx.stroke(); ctx.setLineDash([]);
  ctx.fillStyle = "#8fa1b8"; ctx.font = "12px sans-serif"; ctx.fillText("静止分界100", 6, y-4);
  if(speedHist.length < 2) return;
  ctx.strokeStyle = "#22d3ee"; ctx.lineWidth = 2; ctx.beginPath();
  for(var i=0;i<speedHist.length;i++){
    var x = i/59*c.width, v = Math.min(speedHist[i], maxV), yy = c.height-(v/maxV)*c.height;
    if(i===0) ctx.moveTo(x,yy); else ctx.lineTo(x,yy);
  }
  ctx.stroke(); ctx.lineWidth = 1;
}
function renderStatus(s){
  var nb = document.getElementById("netBadge");
  nb.textContent = s.waiting ? "等待" : "在线";
  nb.className = "badge " + (s.waiting ? "warn" : "ok");
  var mb = document.getElementById("mqttBadge");
  if(mb){
    if(s.mqtt === true){ mb.style.display = ""; mb.textContent = "网关在线"; mb.className = "badge ok"; }
    else if(s.mqtt === false){ mb.style.display = ""; mb.textContent = "网关离线"; mb.className = "badge gray"; }
    else { mb.style.display = "none"; }
  }
  var cb = document.getElementById("clockBadge");
  var tsMs = NaN;
  try{
    if(s.updated_at !== undefined && s.updated_at !== null && s.updated_at !== ""){
      var _u = String(s.updated_at).replace(" ", "T");
      tsMs = Date.parse(_u);
    }
  }catch(e){ tsMs = NaN; }
  var stale = isNaN(tsMs) || (Date.now() - tsMs > 10000);
  if(stale){
    cb.className = "badge gray";
    cb.textContent = s.updated_at ? ("数据陈旧 · " + s.updated_at) : "数据陈旧";
  }else{
    cb.className = "badge info";
    cb.textContent = "更新：" + (s.updated_at || "—");
  }
  var heroTxt = s.waiting ? "等待数据" : (s.presence_text || (s.presence_state ? "有人" : "无人"));
  var heroEl = document.getElementById("heroStatus");
  var heroTime = document.getElementById("heroTime");
  var heroCard = document.getElementById("heroCard");
  if(heroEl) heroEl.textContent = heroTxt;
  if(heroTime) heroTime.textContent = s.updated_at ? ("更新：" + s.updated_at) : "暂无更新";
  if(heroCard) heroCard.className = "card hero " + (s.waiting ? "waiting" : (alarmActive ? "alarm" : (s.presence_state ? "present" : "absent")));
  document.getElementById("ovPresence").textContent = s.waiting ? "等待树莓派数据" : (s.presence_text || (s.presence_state ? "存在" : "无人"));
  document.getElementById("ovTargets").textContent = (s.radar_targets||[]).length;
  var b = s.button || "未知";
  document.getElementById("ovButton").textContent = b;
  document.getElementById("ovButton").style.color = (b === "PRESSED") ? "#f87171" : "";
  document.getElementById("ovTemp").textContent = (s.temperature===null||s.temperature===undefined) ? "—" : fmt(s.temperature,1);
  document.getElementById("ovHum").textContent = (s.humidity===null||s.humidity===undefined) ? "—" : fmt(s.humidity,0);
  document.getElementById("ovSteps").textContent = (s.steps===undefined||s.steps===null) ? "—" : s.steps;
  document.getElementById("ovTempTime").textContent = "采样时间：" + (s.temp_recorded_at || "—");
  var _bpm = Number(s.bpm);
  document.getElementById("ovBpm").textContent = (!_bpm || _bpm <= 0 || _bpm > 200) ? "未佩戴" : s.bpm;
  var _spo2 = Number(s.spo2);
  document.getElementById("ovSpo2").textContent = (!_spo2 || _spo2 <= 0 || _spo2 < 70) ? "未佩戴" : s.spo2;
  var html = "";
  (s.radar_targets||[]).forEach(function(t,i){
    html += "<div>目标"+(i+1)+"：x="+fmt(t.x,0)+"mm y="+fmt(t.y,0)+"mm 径向速度="+fmt(t.speed,1)+"cm/s 距离="+fmt(t.dist,2)+"m</div>";
  });
  document.getElementById("trackList").innerHTML = html || "当前无人 / 无有效目标";
  // 曲线喂位移速度（判定同源）：两帧坐标差/时间差，EMA 平滑；无坐标退回径向速度
  var tgt0 = (s.radar_targets||[])[0] || {};
  var dispSp = null, nowMs = Date.now();
  if(tgt0.dist > 0 && tgt0.x !== undefined && !isNaN(Number(tgt0.x))){
    if(dispPrev.ok){
      var dts = (nowMs - dispPrev.t) / 1000;
      if(dts > 0 && dts < 8){
        var dmm = Math.hypot(Number(tgt0.x) - dispPrev.x, Number(tgt0.y) - dispPrev.y);
        dispSp = Math.min(150, (dmm/10)/dts);
      }
    }
    dispPrev = {ok:true, x:Number(tgt0.x), y:Number(tgt0.y), t:nowMs};
  } else { dispPrev.ok = false; }
  var sp;
  if(dispSp !== null){ dispEma = 0.45*dispSp + 0.55*dispEma; sp = dispEma; }
  else { dispEma = 0.65*dispEma; sp = Number(tgt0.speed)||0; }
  speedHist.push(sp || 0); if(speedHist.length>60) speedHist.shift(); drawCurve();
  var tg = s.radar_targets || [];
  for(var ti=0; ti<3; ti++){
    if(!trails[ti]) trails[ti] = [];
    if(tg[ti] && !alarmActive){ trails[ti].push({x: Number(tg[ti].x)||0, y: Number(tg[ti].y)||0}); if(trails[ti].length>20) trails[ti].shift(); }
    if(!tg[ti] && trails[ti].length) trails[ti].shift();
  }
  drawRoom(tg, alarmActive);
}
function renderAlarms(list){
  var box = document.getElementById("alarmTable");
  document.getElementById("alarmCount").textContent = "（共 " + list.length + " 条）";
  var tl = document.getElementById("alarmTimeline");
  if(!list.length){ box.textContent = "暂无报警记录"; if(tl) tl.innerHTML = ""; return; }
  var h = "<table><tr><th>时间</th><th>类型</th><th>内容</th></tr>";
  list.slice().reverse().forEach(function(it){
    var detail = it.detail !== undefined ? it.detail : (it.message !== undefined ? it.message : JSON.stringify(it));
    h += "<tr><td>"+esc(it.time||it.timestamp||it.created_at||"—")+"</td><td>"+esc(it.type||it.alert_type||"—")+"</td><td>"+esc(detail)+"</td></tr>";
  });
  box.innerHTML = h + "</table>";
  if(tl){
    var th = "";
    list.slice().reverse().forEach(function(it){
      var ty = String(it.type||it.alert_type||"报警");
      var detail = it.detail !== undefined ? it.detail : (it.message !== undefined ? it.message : JSON.stringify(it));
      var tm = it.time||it.timestamp||it.created_at||"—";
      var cls = (/跌倒|求助|按键|fall|button/i.test(ty + detail)) ? "crit" : ((/警告|离线|warn/i.test(ty + detail)) ? "warn2" : "");
      th += "<div class='tl-item '+cls+''><div class='tl-time'>"+esc(tm)+"</div><div class='tl-type'>"+esc(ty)+"</div><div class='tl-detail'>"+esc(detail)+"</div></div>";
    });
    tl.innerHTML = th;
  }
}
function updateBanner(list){
  var banner = document.getElementById("fallBanner");
  var ageEl = document.getElementById("fallAge");
  try{
    if(!list.length){ currentBannerId = ""; alarmActive = false; banner.style.display = "none"; return; }
    var last = list[list.length-1];
    var id = alarmId(last);
    currentBannerId = id;
    if(id && id === dismissedAlarmId){ alarmActive = false; banner.style.display = "none"; return; }
    var ts = null;
    ["timestamp","time","created_at","recorded_at","ts"].forEach(function(k){
      if(ts!==null||last[k]===undefined) return;
      var v = last[k];
      if(typeof v === "number"){ ts = v>1e12?v/1000:v; }
      else { var d = Date.parse(String(v).replace(" ","T")); if(!isNaN(d)) ts = d/1000; }
    });
    var ageTxt = "";
    if(ts!==null){
      var age = Math.max(0, Math.floor(Date.now()/1000 - ts));
      ageTxt = age < 60 ? age + "秒前" : Math.floor(age/60) + "分" + (age%60) + "秒前";
    }
    if(ageEl) ageEl.textContent = ageTxt ? "（" + ageTxt + "）" : "";
    alarmActive = true;
    banner.style.display = "block";
  }catch(e){ alarmActive = true; banner.style.display = "block"; }
}
function renderDevices(list){
  var box = document.getElementById("devList");
  if(!list.length){ box.textContent = "暂无设备信息（缺 devices.json，属正常）"; return; }
  var h = "";
  list.forEach(function(d){
    h += "<div class='dev'><div class='nm'>"+esc(d.name||d.id||"未命名设备")+"</div>"
      + "<div class='meta'>编号：" + esc(d.id||"—") + " · 层级：" + esc(d.layer||"—") + "</div>"
      + "<div class='meta'>接口：" + esc(d.interface||"—") + "</div>"
      + (d.note ? "<div class='meta'>备注：" + esc(d.note) + "</div>" : "") + "</div>";
  });
  box.innerHTML = h;
}
async function fetchJSON(url){
  var r = await fetch(url);
  return await r.json();
}
function openPhoto(name){
  document.getElementById("photoFull").src = "/photo/" + encodeURIComponent(name);
  document.getElementById("photoOverlay").classList.add("active");
}
document.getElementById("photoOverlay").addEventListener("click", function(){
  this.classList.remove("active");
  document.getElementById("photoFull").src = "";
});
document.getElementById("latestImg").addEventListener("click", function(){
  if(this.dataset.name) openPhoto(this.dataset.name);
});
function renderPhotos(list){
  var box = document.getElementById("photoGrid");
  document.getElementById("photoCount").textContent = "（共 " + list.length + " 张）";
  if(!list.length){ box.textContent = "暂无照片"; return; }
  var h = "";
  list.forEach(function(p){
    h += "<img loading='lazy' src='/photo/" + encodeURIComponent(p.name) + "' alt='" + esc(p.name) + "' data-name='" + esc(p.name) + "'>";
  });
  box.innerHTML = h;
  box.querySelectorAll("img").forEach(function(im){
    im.addEventListener("click", function(){ openPhoto(im.getAttribute("data-name")); });
  });
}
function renderLatest(list){
  var card = document.getElementById("latestCard");
  if(!list.length){ card.style.display = "none"; return; }
  var p = list[0];
  card.style.display = "block";
  var im = document.getElementById("latestImg");
  im.src = "/photo/" + encodeURIComponent(p.name);
  im.dataset.name = p.name;
  document.getElementById("latestTime").textContent = p.mtime || p.name;
}
async function tick(){
  // 顶栏徽标 + 横幅/蜂鸣常驻（所有 tab 可见）；各 tab 专属数据按 activeTab 按需拉取
  try{
    var s = await fetchJSON("/api/status");
    if(s && !s.error) renderStatus(s);
  }catch(e){}
  try{
    var a = await fetchJSON("/api/alarms");
    var list = (a && a.alarms) || [];
    updateBanner(list);
    if(activeTab === "alarms") renderAlarms(list);
    if(list.length){
      var id = alarmId(list[list.length-1]);
      if(lastAlarmId && id && id !== lastAlarmId) beep();
      if(id) lastAlarmId = id;
    }
  }catch(e){}
  if(activeTab === "devices"){
    try{
      var d = await fetchJSON("/api/devices");
      renderDevices((d && d.devices) || []);
    }catch(e){}
  }
  if(activeTab === "photos" || activeTab === "overview"){
    try{
      var p = await fetchJSON("/api/photos");
      var plist = (p && p.photos) || [];
      if(activeTab === "photos") renderPhotos(plist);
      renderLatest(plist);
    }catch(e){}
  }
}
var liveSocket = null;
var liveSocketReady = false;
var socketRetryDelay = 1000;
var loadStatusCache = {};
function connectLiveSocket(){
  try{
    var proto = location.protocol === "https:" ? "wss://" : "ws://";
    liveSocket = new WebSocket(proto + location.host + "/ws");
    liveSocket.onopen = function(){
      liveSocketReady = true;
      socketRetryDelay = 1000;
      renderStatus(loadStatusCache || {});
    };
    liveSocket.onmessage = function(evt){
      try{
        var data = JSON.parse(evt.data);
        var s = data && data.status ? data.status : data;
        if(s && typeof s === "object"){ loadStatusCache = s; renderStatus(s); }
      }catch(e){}
    };
    liveSocket.onclose = function(){ scheduleLiveSocket(); };
    liveSocket.onerror = function(){ scheduleLiveSocket(); };
  }catch(e){ scheduleLiveSocket(); }
}
function scheduleLiveSocket(){
  if(liveSocketReady || liveSocket){ liveSocketReady = false; try{ liveSocket.close(); }catch(e){} }
  liveSocket = null;
  setTimeout(connectLiveSocket, socketRetryDelay);
  socketRetryDelay = Math.min(5000, socketRetryDelay * 2);
}
connectLiveSocket();
tick(); setInterval(function(){ if(!liveSocketReady) tick(); }, 600);
</script>
</body>
</html>
"""


# Prefer the editable production UI beside the server; keep the embedded page as rollback fallback.
UI_FILE = os.path.join(BASE_DIR, "index.html")
try:
    with open(UI_FILE, "r", encoding="utf-8") as _ui_file:
        PAGE = _ui_file.read()
except (OSError, UnicodeError):
    pass


# ---------------------------------------------------------------- HTTP 服务
class Handler(BaseHTTPRequestHandler):
    server_version = "PiButlerApp/1.0"

    def log_message(self, *args):
        pass

    def _handle_websocket(self):
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self._send(400, "缺少 WebSocket 握手头", "text/plain; charset=utf-8")
            return
        accept = accept_key(key.strip())
        handshake = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode("ascii")
        self.wfile.write(handshake)
        self.wfile.flush()
        peer = WS_HUB.add(self.connection, self.wfile)
        peer.send(live_snapshot())
        self.close_connection = True
        try:
            self.connection.settimeout(1.0)
            while not peer.closed:
                readable, _, _ = select.select([self.connection], [], [], 1.0)
                if not readable:
                    continue
                data = self.connection.recv(2)
                if not data:
                    break
                if data[0] & 0x0F == 0x8:
                    break
        except (OSError, ValueError):
            pass
        finally:
            WS_HUB.remove(peer)

    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # The dashboard and status APIs are live operational data. Avoid a
        # phone/browser cache showing yesterday's page during a demo.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj):
        try:
            body = json.dumps(obj, ensure_ascii=False)
        except Exception as e:
            body = json.dumps({"error": "encode failed: %s" % e}, ensure_ascii=False)
        self._send(200, body, "application/json; charset=utf-8")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path != "/api/ack":
            self._send(404, "未找到", "text/plain; charset=utf-8")
            return
        try:
            length = min(int(self.headers.get("Content-Length", "0") or 0), 4096)
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8", "replace"))
            if not isinstance(payload, dict):
                raise ValueError("请求体必须是 JSON 对象")
            event_id = payload.get("event_id")
            if not acknowledge_alarm(event_id, payload.get("actor", "现场操作员")):
                self._send_json({"ok": False, "error": "缺少 event_id 或无法写入确认记录"})
                return
            self._send_json({"ok": True, "event_id": str(event_id)})
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)})

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        try:
            if path == "/ws" and self.headers.get("Upgrade", "").lower() == "websocket":
                self._handle_websocket()
            elif path == "/" or path == "/index.html":
                self._send(200, PAGE, "text/html; charset=utf-8")
            elif path == "/api/status":
                try:
                    self._send_json(load_status())
                except Exception as e:
                    self._send_json({"error": str(e)})
            elif path == "/api/alarms":
                try:
                    alarms = load_alarms(50)
                    self._send_json({"alarms": alarms, "count": len(alarms)})
                except Exception as e:
                    self._send_json({"error": str(e)})
            elif path == "/api/devices":
                try:
                    devs = load_devices()
                    self._send_json({"devices": devs, "count": len(devs)})
                except Exception as e:
                    self._send_json({"error": str(e)})
            elif path == "/api/nodes":
                try:
                    self._send_json(node_payload())
                except Exception as e:
                    self._send_json({"error": str(e), "nodes": []})
            elif path == "/api/photos":
                try:
                    photos = load_photos(20)
                    self._send_json({"photos": photos, "count": len(photos)})
                except Exception as e:
                    self._send_json({"error": str(e)})
            elif path == "/api/export":
                try:
                    alarms = load_alarms(1000)
                    body = build_alarms_csv(alarms).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/csv; charset=utf-8")
                    self.send_header("Content-Disposition", 'attachment; filename="alarms.csv"')
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
                    self.end_headers()
                    self.wfile.write(body)
                except Exception as e:
                    self._send_json({"error": str(e)})
            elif path.startswith("/photo/"):
                try:
                    raw = unquote(path[len("/photo/"):])
                    name = os.path.basename(raw)
                    if not name or not name.endswith(".jpg"):
                        self._send(404, "未找到", "text/plain; charset=utf-8")
                    else:
                        full = os.path.abspath(os.path.join(BASE_DIR, name))
                        base = os.path.abspath(BASE_DIR)
                        if full != base and not full.startswith(base + os.sep):
                            self._send(404, "未找到", "text/plain; charset=utf-8")
                        elif not os.path.isfile(full):
                            self._send(404, "未找到", "text/plain; charset=utf-8")
                        else:
                            with open(full, "rb") as f:
                                data = f.read()
                            self._send(200, data, "image/jpeg")
                except Exception as e:
                    self._send_json({"error": str(e)})
            elif path == "/manifest.json":
                self._send(200, '{"name":"老人跌倒监护","short_name":"跌倒监护","display":"standalone","start_url":"/","theme_color":"#f3f7f6","background_color":"#f3f7f6","icons":[{"src":"/icon.svg","sizes":"any","type":"image/svg+xml"}]}', "application/manifest+json; charset=utf-8")
            elif path == "/icon.svg":
                self._send(200, '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="#0f766e"/><path d="M18 42a22 22 0 0 1 0-20M26 42a12 12 0 0 1 0-14M34 42a4 4 0 0 1 0-8" stroke="#ffffff" stroke-width="4" fill="none" stroke-linecap="round"/><circle cx="34" cy="38" r="4" fill="#ffffff"/></svg>', "image/svg+xml")
            elif path == "/api/health":
                status_mtime = _safe_mtime(STATUS_FILE)
                alarms_mtime = _safe_mtime(ALARMS_FILE)
                now = time.time()
                self._send_json({
                    "ok": True,
                    "websocket_clients": WS_HUB.count(),
                    "transport": "websocket" if WS_HUB.count() else "polling",
                    "status_age_s": None if status_mtime is None else round(max(0.0, now - status_mtime), 2),
                    "alarms_age_s": None if alarms_mtime is None else round(max(0.0, now - alarms_mtime), 2),
                    "status_file": os.path.basename(STATUS_FILE),
                    "alarms_file": os.path.basename(ALARMS_FILE),
                    "mqtt": bool(load_status().get("mqtt", False)),
                    "outbox_pending": load_status().get("outbox_pending", 0),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })
            else:
                self._send(404, "未找到", "text/plain; charset=utf-8")
        except Exception as e:
            try:
                self._send_json({"error": str(e)})
            except Exception:
                pass


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=_watch_live_files, name="websocket-live-watcher", daemon=True).start()
    try:
        ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        ip = "<派IP>"
    print("=" * 55)
    print("老人跌倒监护 · 移动端 Web 应用已启动（仅标准库，无第三方依赖）")
    print("本机访问 : http://127.0.0.1:%d/" % PORT)
    print("手机同WiFi : http://%s:%d/  （把 %s 换成树莓派实际IP）" % (ip, PORT, ip))
    print("状态文件 : %s  （缺失则显示等待占位，不崩溃）" % STATUS_FILE)
    print("报警文件 : %s  （取最后50行）" % ALARMS_FILE)
    print("设备文件 : %s  （可缺省，缺则设备页显示占位）" % DEVICES_FILE)
    print("按 Ctrl+C 停止。")
    print("=" * 55)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()


