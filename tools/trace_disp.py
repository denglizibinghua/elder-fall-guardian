"""70s status.json sampler at 2Hz -> displacement speed profile (read-only).

Run on Pi:  python3 -u /home/admin/trace_disp.py > ~/trace_disp.log 2>&1
No sensor access, no UART, pure JSON file polling. x/y are mm.
"""
import time
import json
import math

STATUS = "/home/admin/food_project/status.json"
DURATION = 70.0


def read_status():
    with open(STATUS, "r", encoding="utf-8") as fh:
        return json.load(fh)


prev_xy = None
prev_t = None
t_end = time.time() + DURATION
t_start = t_end - DURATION
disp_ema = 0.0
while time.time() < t_end:
    try:
        raw = read_status()
        targets = raw.get("radar_targets") or []
        valid = [t for t in targets if t.get("dist", 0) > 0]
        now = time.time()
        line = "T+%.2f" % (now - t_start)
        if valid:
            t0 = valid[0]
            x, y = t0.get("x", 0), t0.get("y", 0)
            if prev_xy is not None and prev_t is not None:
                dt = now - prev_t
                if 0 < dt < 5:
                    dmm = math.hypot(x - prev_xy[0], y - prev_xy[1])
                    inst = min(300.0, (dmm / 10.0) / dt)
                    disp_ema = 0.45 * inst + 0.55 * disp_ema
                else:
                    disp_ema = 0.45 * 0.0 + 0.55 * disp_ema
            prev_xy = (x, y)
            prev_t = now
            line += " dist=%.2f disp_ema=%.1f" % (t0.get("dist", 0), disp_ema)
        else:
            prev_xy = None
            prev_t = None
            disp_ema = 0.55 * disp_ema
            line += " ABSENT disp_ema=%.1f" % disp_ema
        print(line, flush=True)
    except Exception as exc:
        print("read_err=%s" % exc, flush=True)
    time.sleep(0.5)

print("TRACE_DONE", flush=True)
