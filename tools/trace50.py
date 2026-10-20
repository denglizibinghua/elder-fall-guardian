"""75s 50Hz MPU mag trace + 1Hz radar snapshot (read-only, no UART access).

Run on Pi:  python3 -u /home/admin/trace50.py > ~/trace50.log 2>&1
High rate (0.02s) to catch short impact spikes (<100ms) that 10Hz sampling
could miss. Correlate later against which action was performed when.
"""
import time
import json
import math
import smbus2 as smbus

ADDR = 0x68
ACCEL = 0x3B
STATUS = "/home/admin/food_project/status.json"
DURATION = 75.0

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


t_end = time.time() + DURATION
t_start = t_end - DURATION
next_status_ts = 0.0
while time.time() < t_end:
    try:
        m = read_mag()
    except Exception as exc:
        m = -1
        print("i2c_err=%s" % exc, flush=True)
    now = time.time()
    line = "T+%.2f mag=%.0f" % (now - t_start, m)
    if now >= next_status_ts:
        try:
            with open(STATUS, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            targets = raw.get("radar_targets") or []
            valid = [t for t in targets if t.get("dist", 0) > 0]
            if valid:
                t0 = valid[0]
                line += " | dist=%.2f speed=%.1f" % (t0.get("dist", 0), t0.get("speed", 0))
        except Exception:
            pass
        next_status_ts = now + 1.0
    print(line, flush=True)
    time.sleep(0.02)

print("TRACE_DONE", flush=True)
