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
  GET /api/rules    {"ok":bool,"items":[{key,label,desc,value}]} 判定参数（rules.json）
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
import json
import os
import socket
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATUS_FILE = os.path.join(BASE_DIR, "status.json")
ALARMS_FILE = os.path.join(BASE_DIR, "alarms.jsonl")
DEVICES_FILE = os.path.join(BASE_DIR, "devices.json")
RULES_FILE = os.path.join(BASE_DIR, "rules.json")
PORT = 8080


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
        "waiting": True,
        "presence_state": False,
        "presence_text": "等待树莓派数据",
        "radar_targets": [],
        "steps": 0,
        "temperature": None,
        "humidity": None,
        "temp_recorded_at": None,
        "bpm": 0,
        "spo2": 0,
        "button": "未知",
        "take_photo_flag": False,
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
        presence = bool(raw.get("presence_state", False))
        btn = raw.get("button", raw.get("button_state", "未知"))
        spo2 = _to_float(raw.get("spo2", 0) or 0)
        out = {
            "waiting": False,
            "presence_state": presence,
            "presence_text": "存在" if presence else "无人",
            "radar_targets": clean_targets,
            "steps": _to_int(raw.get("steps", 0)),
            "temperature": _to_float(raw.get("temperature")),
            "humidity": _to_float(raw.get("humidity")),
            "temp_recorded_at": raw.get("temp_recorded_at"),
            "bpm": _to_int(raw.get("bpm", 0)),
            "spo2": 0 if spo2 is None else spo2,
            "button": normalize_button(btn),
            "take_photo_flag": bool(raw.get("take_photo_flag", False)),
            "updated_at": raw.get("updated_at"),
        }
        if "mqtt" in raw and isinstance(raw.get("mqtt"), bool):
            out["mqtt"] = bool(raw.get("mqtt"))
        return out
    except Exception:
        return placeholder


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
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    out.append(obj)
                else:
                    out.append({"message": line})
            except Exception:
                out.append({"message": line})
        return out
    except Exception:
        return []


# ---------------------------------------------------------------- CSV 导出
def _csv_field(v):
    """CSV 字段加引号，内部双引号翻倍（RFC 4180 风格）。"""
    s = "" if v is None else str(v)
    return '"' + s.replace('"', '""') + '"'


def _alarm_cells(it):
    """从单条报警提取 (time, type, detail)，兼容旧字段 message/timestamp。"""
    if not isinstance(it, dict):
        return ("", "", str(it))
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
    return (t, ty, d)


def build_alarms_csv(alarms):
    """报警列表 -> CSV 文本（表头 time,type,detail，每条一行，全字段引号包裹）。"""
    lines = ["time,type,detail"]
    for it in alarms or []:
        t, ty, d = _alarm_cells(it)
        lines.append(",".join((_csv_field(t), _csv_field(ty), _csv_field(d))))
    return "\r\n".join(lines) + "\r\n"


# ---------------------------------------------------------------- 规则读取
RULE_LABELS = {
    "alarm_mode": ("判定模式", "transition=速度骤降沿（默认）/ fusion=IMU+静止融合"),
    "active_speed_threshold": ("活动阈值", "位移速度高于此值视为活动"),
    "alarm_cooldown": ("报警冷却", "两次报警之间的最小间隔"),
    "fusion_window": ("融合窗口", "IMU 尖峰后等待静止的时间窗"),
    "spike_threshold": ("IMU 尖峰阈值", "合加速度突变门限"),
    "still_speed_threshold": ("静止阈值", "低于此值视为静止"),
    "stillness_duration": ("静止持续", "静止保持多久才确认"),
    "transition_window": ("骤降窗口", "活动结束后多久内骤停算跌倒"),
}


def load_rules():
    """读 rules.json -> {"ok":bool,"values":{k:v},"items":[{key,label,value,desc}]}。"""
    try:
        if not os.path.exists(RULES_FILE):
            return {"ok": False, "items": []}
        with open(RULES_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return {"ok": False, "items": []}
        items = []
        for k, v in raw.items():
            lab, desc = RULE_LABELS.get(k, (k, ""))
            items.append({
                "key": k,
                "label": lab,
                "desc": desc,
                "value": v if isinstance(v, (int, float, str, bool)) else json.dumps(v, ensure_ascii=False),
            })
        return {"ok": True, "items": items}
    except Exception:
        return {"ok": False, "items": []}


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
                    "_mt": mt,
                })
            except Exception:
                continue
        items.sort(key=lambda d: d["_mt"], reverse=True)
        return [{"name": d["name"], "mtime": d["mtime"]} for d in items[:limit]]
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
:root{--bg:#060a14;--s1:rgba(148,163,184,.055);--s2:rgba(148,163,184,.095);--line:rgba(148,163,184,.14);--line2:rgba(148,163,184,.26);--txt:#eaf1f9;--txt2:#c2d0e2;--mut:#8496ad;--acc:#22d3ee;--accd:rgba(34,211,238,.13);--red:#fb7185;--redd:rgba(251,113,133,.13);--grn:#34d399;--grnd:rgba(52,211,153,.13);--amb:#fbbf24;--ambd:rgba(251,191,36,.13);--vio:#a78bfa;--rl:18px;--rm:14px;--rs:10px}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.38;transform:scale(.76)}}
@keyframes rise{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:none}}
@keyframes glow{0%,100%{box-shadow:0 10px 34px -12px rgba(251,113,133,.45)}50%{box-shadow:0 10px 44px -8px rgba(251,113,133,.75)}}
@keyframes sweep{from{transform:translateX(-120%)}to{transform:translateX(320%)}}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html{-webkit-text-size-adjust:100%}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC","Hiragino Sans GB","Microsoft YaHei",system-ui,sans-serif;background:radial-gradient(90% 46% at 50% -6%,rgba(34,211,238,.15) 0%,transparent 62%),radial-gradient(64% 34% at 90% 4%,rgba(167,139,250,.11) 0%,transparent 58%),linear-gradient(180deg,#0a1326 0%,#070d1a 52%,#05080f 100%);background-attachment:fixed;color:var(--txt);padding:0 14px calc(88px + env(safe-area-inset-bottom));max-width:680px;margin-left:auto;margin-right:auto;line-height:1.55;min-height:100vh;-webkit-font-smoothing:antialiased}
header{position:sticky;top:0;background:rgba(6,10,20,.82);backdrop-filter:blur(14px) saturate(150%);-webkit-backdrop-filter:blur(14px) saturate(150%);padding:13px 2px 11px;z-index:5;border-bottom:1px solid var(--line)}
h1{font-size:19px;margin:0 0 9px;letter-spacing:.05em;font-weight:800;display:flex;align-items:center}
h1::before{content:"";display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--acc);box-shadow:0 0 12px var(--acc);margin-right:9px;animation:pulse 2.4s ease-in-out infinite}
.badge{display:inline-block;padding:5px 11px;border-radius:999px;font-size:12px;line-height:1.45;margin:2px 5px 2px 0;border:1px solid var(--line2);font-variant-numeric:tabular-nums;font-weight:600;white-space:nowrap}
.badge.ok{background:var(--grnd);color:#6ee7b7;border-color:rgba(52,211,153,.34)}.badge.warn{background:var(--ambd);color:#fcd34d;border-color:rgba(251,191,36,.34)}.badge.bad{background:var(--redd);color:#fda4af;border-color:rgba(251,113,133,.42)}.badge.info{background:var(--accd);color:#67e8f9;border-color:rgba(34,211,238,.3)}
.badge.gray{background:var(--s2);color:#a8b8cc;border-color:var(--line)}
#fallBanner{display:none;background:linear-gradient(180deg,rgba(251,113,133,.24),rgba(251,113,133,.1));border:1px solid rgba(251,113,133,.55);color:#fff;font-weight:700;text-align:center;padding:13px 14px;border-radius:var(--rl);margin:12px 0;font-size:16px;animation:glow 1.7s ease-in-out infinite;position:relative;overflow:hidden}
#fallBanner .dismiss-btn{display:block;width:100%;margin:12px 0 0;min-height:48px;padding:13px 20px;font-size:16px;font-weight:800;border:0;border-radius:var(--rm);background:#fff;color:#881337;cursor:pointer;box-shadow:0 6px 18px -8px rgba(136,19,58,.7);transition:transform .12s}
#fallBanner .dismiss-btn:active{transform:translateY(1px)}
#fallBanner::after{content:"";position:absolute;inset:0;width:36%;background:linear-gradient(90deg,transparent,rgba(255,255,255,.13),transparent);animation:sweep 2.2s linear infinite;pointer-events:none}
.card{background:linear-gradient(180deg,rgba(255,255,255,.045),rgba(255,255,255,.012));border:1px solid var(--line);border-radius:var(--rl);padding:16px;margin:14px 0;box-shadow:0 10px 34px -18px rgba(0,0,0,.9);animation:rise .45s cubic-bezier(.2,.7,.3,1) both}
.card h2{font-size:12px;margin:0 0 13px;color:var(--acc);letter-spacing:.16em;font-weight:800;display:flex;align-items:baseline;justify-content:space-between;gap:8px}
.card h2::after{content:"";flex:1;height:1px;background:linear-gradient(90deg,rgba(34,211,238,.4),transparent);margin-left:2px;align-self:center}
section.tab{padding:3px 0}
.hero{text-align:center;padding:26px 14px 22px;position:relative;overflow:hidden}
.hero::before{content:"";position:absolute;inset:-40% 20% auto;height:150px;background:radial-gradient(50% 50% at 50% 50%,currentColor,transparent 70%);opacity:.13;pointer-events:none}
.hero-status{font-size:52px;font-weight:800;line-height:1.12;letter-spacing:.05em}
.hero-status::before{content:"";display:inline-block;width:15px;height:15px;border-radius:50%;margin-right:12px;vertical-align:7px;background:currentColor}
.hero.present .hero-status{color:var(--grn);text-shadow:0 0 30px rgba(52,211,153,.5)}
.hero.present .hero-status::before{background:var(--grn);box-shadow:0 0 14px var(--grn),0 0 36px rgba(52,211,153,.65);animation:pulse 2s ease-in-out infinite}
.hero.absent .hero-status{color:#b7c4d6;text-shadow:0 0 18px rgba(148,163,184,.25)}
.hero.absent .hero-status::before{background:#64748b;box-shadow:0 0 10px rgba(100,116,139,.7)}
.hero.waiting .hero-status{color:#94a3b8;font-size:29px;text-shadow:none}
.hero.waiting .hero-status::before{background:var(--amb);box-shadow:0 0 12px var(--amb);animation:pulse 1.4s ease-in-out infinite}
.hero.alarm .hero-status{color:var(--red);text-shadow:0 0 30px rgba(251,113,133,.55);animation:pulse 1.15s ease-in-out infinite}
.hero.alarm .hero-status::before{background:var(--red);box-shadow:0 0 14px var(--red),0 0 36px rgba(251,113,133,.7)}
.hero-time{font-size:12px;color:var(--mut);margin-top:9px;font-variant-numeric:tabular-nums}
.row{display:flex;flex-wrap:wrap;gap:10px}
.kv{flex:1 1 138px;background:rgba(6,10,20,.5);border:1px solid var(--line);border-radius:var(--rm);padding:12px 13px;min-height:72px;display:flex;flex-direction:column;justify-content:center}
.kv .k{font-size:11px;color:var(--mut);letter-spacing:.04em;font-weight:600}
.kv .v{font-size:23px;margin-top:3px;font-variant-numeric:tabular-nums;font-weight:800;letter-spacing:-.01em}
table{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}
th,td{border-bottom:1px solid var(--line);padding:10px 5px;text-align:left;word-break:break-all;vertical-align:top}
th{color:var(--mut);font-size:11px;letter-spacing:.09em;font-weight:700;text-transform:uppercase;position:sticky;top:0;background:rgba(8,13,26,.96);backdrop-filter:blur(6px);z-index:2}
tbody tr{transition:background .16s}
tbody tr:active{background:var(--s2)}
td:nth-child(2){font-weight:700;color:#fda4af}
canvas{width:100%;background:#050b18;border:1px solid var(--line);border-radius:var(--rm)}
#roomMap{height:280px}
#speedCv{height:150px}
.small{font-size:12px;color:var(--mut);line-height:1.65}
.tnum{font-variant-numeric:tabular-nums}
.btn{display:inline-flex;align-items:center;gap:7px;min-height:46px;line-height:1;padding:12px 20px;border-radius:var(--rm);background:linear-gradient(180deg,rgba(34,211,238,.16),rgba(34,211,238,.07));color:#8ef1ff;font-size:15px;font-weight:700;text-decoration:none;border:1px solid rgba(34,211,238,.36);transition:transform .12s,box-shadow .16s;box-shadow:0 6px 20px -10px rgba(34,211,238,.5)}
.btn:active{transform:translateY(1px);background:linear-gradient(180deg,rgba(34,211,238,.28),rgba(34,211,238,.14))}
.dev{border:1px solid var(--line);border-radius:var(--rm);padding:13px 14px;margin:9px 0;background:linear-gradient(180deg,rgba(255,255,255,.035),rgba(255,255,255,.01));transition:border-color .16s}
.dev:active{border-color:var(--line2)}
.dev .nm{font-size:15px;font-weight:800;display:flex;align-items:center;gap:8px}
.dev .nm::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--vio);box-shadow:0 0 9px var(--vio);flex:none}
.dev .meta{font-size:12px;color:var(--mut);margin-top:5px}
nav#tabbar{position:fixed;left:0;right:0;bottom:0;display:flex;background:rgba(7,12,24,.9);backdrop-filter:blur(16px) saturate(160%);-webkit-backdrop-filter:blur(16px) saturate(160%);border-top:1px solid var(--line);padding-bottom:env(safe-area-inset-bottom);z-index:10;max-width:680px;margin:0 auto}
nav#tabbar button{flex:1;border:0;background:none;color:#7d8ea6;font-size:12px;font-weight:700;min-height:60px;padding:9px 2px 11px;cursor:pointer;border-top:2px solid transparent;position:relative;transition:color .18s,background .18s;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px}
nav#tabbar button::before{content:"";width:20px;height:20px;border-radius:7px;background:currentColor;opacity:.28;-webkit-mask:var(--ico) center/contain no-repeat;mask:var(--ico) center/contain no-repeat}
nav#tabbar button[data-tab=overview]{--ico:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23000' stroke-width='2.2' stroke-linecap='round'%3E%3Cpath d='M4 13h6V4H4zM14 20h6v-9h-6zM4 20h6v-4H4zM14 8h6V4h-6z'/%3E%3C/svg%3E")}
nav#tabbar button[data-tab=track]{--ico:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23000' stroke-width='2.2' stroke-linecap='round'%3E%3Cpath d='M3 17c3-6 6 4 9-2s6 4 9-2'/%3E%3Ccircle cx='12' cy='15' r='1.6' fill='%23000'/%3E%3C/svg%3E")}
nav#tabbar button[data-tab=alarms]{--ico:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23000' stroke-width='2.2' stroke-linecap='round'%3E%3Cpath d='M6 9a6 6 0 1 1 12 0c0 5 2 6 2 6H4s2-1 2-6'/%3E%3Cpath d='M10 20a2 2 0 0 0 4 0'/%3E%3C/svg%3E")}
nav#tabbar button[data-tab=devices]{--ico:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23000' stroke-width='2.2' stroke-linecap='round'%3E%3Crect x='4' y='4' width='16' height='16' rx='3'/%3E%3Cpath d='M9 9h6v6H9z'/%3E%3C/svg%3E")}
nav#tabbar button[data-tab=photos]{--ico:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23000' stroke-width='2.2' stroke-linecap='round'%3E%3Crect x='3' y='5' width='18' height='14' rx='3'/%3E%3Ccircle cx='9' cy='10' r='1.8'/%3E%3Cpath d='M4 17l5-4 4 3 3-2 4 3'/%3E%3C/svg%3E")}
nav#tabbar button[data-tab=about]{--ico:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23000' stroke-width='2.2' stroke-linecap='round'%3E%3Ccircle cx='12' cy='12' r='9'/%3E%3Cpath d='M12 11v5M12 8v.01'/%3E%3C/svg%3E")}
nav#tabbar button.active{color:var(--acc);background:linear-gradient(180deg,rgba(34,211,238,.12),transparent)}
nav#tabbar button.active::before{opacity:1}
nav#tabbar button.alarmdot::after{content:"";position:absolute;top:7px;right:calc(50% - 20px);width:8px;height:8px;border-radius:50%;background:var(--red);box-shadow:0 0 9px var(--red);animation:pulse 1.3s ease-in-out infinite}
section.tab{display:none}
section.tab.active{display:block}
.arch div{padding:10px 12px;background:rgba(6,10,20,.5);border:1px solid var(--line);border-radius:var(--rm);margin:7px 0;font-size:13px;color:var(--txt2)}
.rule-row{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;padding:10px 12px;background:rgba(6,10,20,.5);border:1px solid var(--line);border-radius:var(--rm);margin:7px 0}
.rule-k{font-size:13px;color:var(--txt);font-weight:700}
.rule-d{display:block;font-size:11px;font-weight:400;color:var(--mut);margin-top:3px;line-height:1.5}
.rule-v{font-size:15px;color:var(--acc);white-space:nowrap;font-variant-numeric:tabular-nums}
.pgrid{display:grid;grid-template-columns:repeat(3,1fr);gap:9px}
.pgrid img{width:100%;height:96px;object-fit:cover;border-radius:var(--rs);background:#050b18;cursor:pointer;border:1px solid var(--line);transition:transform .14s,border-color .14s}
.pgrid img:active{transform:scale(.97);border-color:var(--acc)}
.latestImg{width:100%;max-height:230px;object-fit:cover;border-radius:var(--rm);cursor:pointer;border:1px solid var(--line);background:#050b18}
#photoOverlay{display:none;position:fixed;inset:0;background:rgba(2,4,9,.94);z-index:50;align-items:center;justify-content:center;padding:14px;backdrop-filter:blur(4px)}
#photoOverlay.active{display:flex;animation:rise .2s ease both}
#photoOverlay img{max-width:100%;max-height:100%;border-radius:var(--rm);box-shadow:0 24px 70px rgba(0,0,0,.75)}
.tl{position:relative;margin:12px 0 2px;padding-left:24px;font-variant-numeric:tabular-nums}
.tl::before{content:"";position:absolute;left:7px;top:8px;bottom:8px;width:2px;background:linear-gradient(180deg,rgba(34,211,238,.6),rgba(34,211,238,.05));border-radius:2px}
.tl-item{position:relative;background:linear-gradient(180deg,rgba(255,255,255,.04),rgba(255,255,255,.012));border:1px solid var(--line);border-radius:var(--rm);padding:11px 13px;margin:0 0 10px;animation:rise .3s ease both}
.tl-item::before{content:"";position:absolute;left:-22px;top:16px;width:10px;height:10px;border-radius:50%;background:var(--acc);box-shadow:0 0 9px var(--acc)}
.tl-item.crit{border-color:rgba(251,113,133,.4);background:linear-gradient(180deg,rgba(251,113,133,.1),rgba(251,113,133,.02))}
.tl-item.crit::before{background:var(--red);box-shadow:0 0 11px var(--red)}
.tl-item.warn2::before{background:var(--amb);box-shadow:0 0 10px var(--amb)}
.tl-time{font-size:11px;color:var(--acc);font-weight:700;font-variant-numeric:tabular-nums}
.tl-type{font-size:14px;font-weight:800;margin:3px 0}
.tl-detail{font-size:12px;color:var(--txt2);word-break:break-all;line-height:1.6}
#trackList{font-variant-numeric:tabular-nums;display:flex;flex-direction:column;gap:7px}
#trackList div{padding:9px 11px;background:rgba(6,10,20,.5);border:1px solid var(--line);border-radius:var(--rs);font-size:12px;color:var(--txt2)}
.map-legend{display:flex;gap:14px;flex-wrap:wrap;margin-top:9px}
.map-legend span{font-size:11px;color:var(--mut)}
.map-legend i{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:5px;font-style:normal}
.empty{padding:26px 10px;text-align:center;color:var(--mut);font-size:13px}
@media (max-width:400px){.hero-status{font-size:44px}.kv{flex:1 1 100px}.kv .v{font-size:20px}}
@media (prefers-reduced-motion:reduce){*{animation-duration:.01ms!important;animation-iteration-count:1!important}}
</style>
</head>
<body>
<header>
<h1>老人跌倒监护</h1>
<div><span id="netBadge" class="badge info">加载中…</span><span id="mqttBadge" class="badge gray" style="display:none">网关离线</span><span id="clockBadge" class="badge info">—</span></div>
<div id="fallBanner"><span style="position:relative;z-index:1">⚠ 跌倒报警！请立即查看老人情况 <span id="fallAge"></span></span><button id="dismissBtn" class="dismiss-btn" style="position:relative;z-index:1">已知晓</button></div>
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
<div class="card" id="latestCard" style="display:none"><h2>最近抓拍</h2><img id="latestImg" class="latestImg" alt="最近抓拍"><div class="small" id="latestTime"></div></div>
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
<div class="small">曲线=两帧坐标位移算出的真速度（横走也可见）；红色虚线为「静止阈值」，低于它视为静止；报警 = 活动后 3 秒内骤停 + 静止满 3 秒（阈值见「关于」页，读 rules.json）。</div></div>
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
<div class="card"><h2>判定逻辑说明</h2><div class="arch">
<div><b>速度来源</b>：由两帧雷达坐标位移 / 时间差算出真实速度（EMA 平滑），横走也可见；不使用 LD2450 径向 speed 字段（横走读数≈0）</div>
<div><b>默认模式</b>：活动后 3 秒内骤停 + 静止保持 3 秒 → 触发跌倒报警，冷却 30 秒</div>
<div><b>融合模式</b>：MPU6050 合加速度尖峰（&gt;25000）→ 10 秒窗口内静止 3 秒 → 触发，抗单人坐卧误报</div>
<div><b>报警动作</b>：写 alarms.jsonl + 异步抓拍 *_food.jpg（不阻塞判定循环）+ MQTT 广播 + 本页横幅与蜂鸣</div>
</div></div>
<div class="card"><h2>当前判定参数 <span id="rulesState" class="small"></span></h2>
<div id="rulesList" class="arch">读取中…</div>
<div class="small">改 rules.json 后需重启 main.py 才生效（<code>sudo systemctl restart food_project.service</code>），本页每 30 秒刷新一次。</div></div>
<div class="card"><h2>数据文件</h2><div class="arch">
<div>status.json — 每 0.5s 原子写入，雷达 / 温湿度 / 心率 / 按键快照</div>
<div>alarms.jsonl — 报警流水，每行一条 JSON，导出 CSV 取最近 1000 条</div>
<div>*_food.jpg — 报警时抓拍，保留最近 20 张于相册页</div>
<div>devices.json / rules.json — 设备清单与判定阈值</div>
</div></div>
<div class="card"><h2>已知限制</h2><div class="arch">
<div>弱光环境抓拍偏暗（ov5647 + 500ms 曝光），建议报警时补光或延长曝光</div>
<div>心率血氧为 MAX30102 透射式，须夹住手指；脱落时读数 250/74.7% 属野值，界面显示“未佩戴”</div>
<div>存在状态由雷达目标数决定，老人静止坐卧仍可能计为“无人”，不影响跌倒判定</div>
</div></div>
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
var stillThr = 50;   // 静止阈值（cm/s），从 /api/rules 读到后覆盖，画速度曲线用
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
  var maxV = 150, y = c.height - (stillThr/maxV)*c.height;
  ctx.strokeStyle = "#f87171"; ctx.setLineDash([5,4]); ctx.beginPath(); ctx.moveTo(0,y); ctx.lineTo(c.width,y); ctx.stroke(); ctx.setLineDash([]);
  ctx.fillStyle = "#8fa1b8"; ctx.font = "12px sans-serif"; ctx.fillText("静止阈值" + stillThr, 6, y-4);
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
  document.getElementById("ovBpm").textContent = (!_bpm || _bpm <= 0 || _bpm > 200) ? "未佩戴" : String(Math.round(_bpm));
  var _spo2 = Number(s.spo2);
  document.getElementById("ovSpo2").textContent = (!_spo2 || _spo2 <= 0 || _spo2 < 70) ? "未佩戴" : _spo2.toFixed(1);
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
      th += "<div class=\\"tl-item "+cls+"\\"><div class=\\"tl-time\\">"+esc(tm)+"</div><div class=\\"tl-type\\">"+esc(ty)+"</div><div class=\\"tl-detail\\">"+esc(detail)+"</div></div>";
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
    h += "<div class=\\"dev\\"><div class=\\"nm\\">"+esc(d.name||d.id||"未命名设备")+"</div>"
      + "<div class=\\"meta\\">编号：" + esc(d.id||"—") + " · 层级：" + esc(d.layer||"—") + "</div>"
      + "<div class=\\"meta\\">接口：" + esc(d.interface||"—") + "</div>"
      + (d.note ? "<div class=\\"meta\\">备注：" + esc(d.note) + "</div>" : "") + "</div>";
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
  if(!list.length){ box.textContent = "暂无照片"; box.dataset.sig = ""; return; }
  // 关键：tick() 每 600ms 调一次本函数。若无条件重建 innerHTML，20 张图每次
  // 都被销毁重建，浏览器来不及下载完就又重来 → naturalWidth 永远为 0（白框）。
  // 所以只在照片列表真的变化时才重建 DOM。
  var sig = list.map(function(p){ return p.name; }).join("|");
  if(box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  // 不用 loading="lazy"：本项目只有 20 张图（局域网 ~5MB），而 lazy 在部分
  // 环境下（后台标签/无头浏览器/容器滚动）不会触发加载，表现为永远空白。
  // 配合上面的 sig 去重，图片只会被请求一次。
  var h = "";
  list.forEach(function(p){
    h += '<img src="/photo/' + encodeURIComponent(p.name) + '" alt="' + esc(p.name) + '" data-name="' + esc(p.name) + '">';
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
  // 同上：src 没变就不要重设，否则每次 tick 都重新拉一遍图
  if(im.dataset.name !== p.name){
    im.src = "/photo/" + encodeURIComponent(p.name);
    im.dataset.name = p.name;
  }
  document.getElementById("latestTime").textContent = p.mtime || p.name;
}
function renderRules(d){
  var box = document.getElementById("rulesList");
  var st = document.getElementById("rulesState");
  if(!box) return;
  if(!d || d.ok === false || !(d.items||[]).length){
    box.innerHTML = "<div>暂无 rules.json（使用内置默认阈值）</div>";
    if(st) st.textContent = "默认值";
    return;
  }
  var h = "";
  (d.items||[]).forEach(function(r){
    var v = (typeof r.value === "number") ? String(r.value) : esc(r.value);
    h += '<div class="rule-row"><span class="rule-k">' + esc(r.label||r.key)
      + '<span class="rule-d">' + esc(r.desc||"") + ' · ' + esc(r.key) + '</span></span>'
      + '<b class="rule-v">' + v + '</b></div>';
  });
  box.innerHTML = h;
  if(st) st.textContent = "共 " + d.items.length + " 项";
  // 同步静止阈值到速度曲线（曲线画在轨迹 tab，两处必须一致）
  (d.items||[]).forEach(function(r){
    if(r.key === "still_speed_threshold"){
      var n = Number(r.value);
      if(!isNaN(n) && n > 0){ stillThr = n; drawCurve(); }
    }
  });
}
// 启动时先拉一次 rules，保证速度曲线的阈值线在打开轨迹 tab 前就是对的
async function primeRules(){
  try{
    var d = await fetchJSON("/api/rules");
    (d && d.items || []).forEach(function(r){
      if(r.key === "still_speed_threshold"){
        var n = Number(r.value);
        if(!isNaN(n) && n > 0) stillThr = n;
      }
    });
    drawCurve();
  }catch(e){}
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
  if(activeTab === "about"){
    try{
      var r = await fetchJSON("/api/rules");
      renderRules(r);
    }catch(e){}
  }
}
var _rulesTs = 0;
setInterval(function(){
  var n = Date.now();
  if(activeTab === "about" && n - _rulesTs > 30000){ _rulesTs = n; tick(); }
}, 1000);
tick(); setInterval(tick, 600); primeRules();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------- HTTP 服务
class Handler(BaseHTTPRequestHandler):
    server_version = "PiButlerApp/1.0"

    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj):
        try:
            body = json.dumps(obj, ensure_ascii=False)
        except Exception as e:
            body = json.dumps({"error": "encode failed: %s" % e}, ensure_ascii=False)
        self._send(200, body, "application/json; charset=utf-8")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        try:
            if path == "/" or path == "/index.html":
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
                    self.end_headers()
                    self.wfile.write(body)
                except Exception as e:
                    self._send_json({"error": str(e)})
            elif path == "/api/rules":
                try:
                    self._send_json(load_rules())
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
                self._send(200, '{"name":"老人跌倒监护","short_name":"跌倒监护","display":"standalone","start_url":"/","theme_color":"#070d1a","background_color":"#070d1a","icons":[{"src":"/icon.svg","sizes":"any","type":"image/svg+xml"}]}', "application/manifest+json; charset=utf-8")
            elif path == "/icon.svg":
                self._send(200, '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="#070d1a"/><path d="M18 42a22 22 0 0 1 0-20M26 42a12 12 0 0 1 0-14M34 42a4 4 0 0 1 0-8" stroke="#22d3ee" stroke-width="4" fill="none" stroke-linecap="round"/><circle cx="34" cy="38" r="4" fill="#22d3ee"/></svg>', "image/svg+xml")
            else:
                self._send(404, "未找到", "text/plain; charset=utf-8")
        except Exception as e:
            try:
                self._send_json({"error": str(e)})
            except Exception:
                pass


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
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
