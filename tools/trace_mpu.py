"""60s I2C MPU mag + status.json radar snapshot tracer (read-only, no UART access).

Run on Pi:  python3 -u /home/admin/trace_mpu.py > ~/trace.log 2>&1
Safe to run beside food_project main.py: SMBus reads only (no serial open),
status.json file poll only. Western answers = T+sec mag speed dist presence.
"""
import time
import json
import math
import smbus2 as smbus

ADDR = 0x68
ACCEL = 0x3B
STATUS = "/home/admin/food_project/status.json"
DURATION = 90.0

bus = smbus.SMBus(1)


def read_mag():
    vals = []
    for lo_reg in (ACCEL, ACCEL + 2, ACCEL + 4):
        hi = bus.read_byte_data(ADDR, lo_reg)
        lo = bus.read_byte_data(ADDR, lo_reg + 1)
        v = (hi << 8) | lo
        if v > 32768:
            v -= 65536
        vals.append(v)
    return math.sqrt(vals[0] * vals[0] + vals[1] * vals[1] + vals[2] * vals[2])


def read_status_summary():
    try:
        with open(STATUS, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        targets = raw.get("radar_targets") or []
        valid = [t for t in targets if t.get("dist", 0) > 0]
        pres = raw.get("presence_state", False)
        if valid:
            t = valid[0]
            return "valid_targets=%d presence=%s dist=%.2f speed=%.1f" % (
                len(valid), pres, t.get("dist", 0), t.get("speed", 0))
        return "valid_targets=0 presence=%s" % pres
    except Exception as exc:
        return "status_err=%s" % exc


t_end = time.time() + DURATION
next_status_ts = 0.0
while time.time() < t_end:
    try:
        m = read_mag()
    except Exception as exc:
        m = -1
        print("i2c_err=%s" % exc, flush=True)
    now = time.time()
    line = "T+%.1f mag=%.0f" % (now - (t_end - DURATION), m)
    if now >= next_status_ts:
        line += " | " + read_status_summary()
        next_status_ts = now + 2.0
    print(line, flush=True)
    time.sleep(0.1)

print("TRACE_DONE", flush=True)
