"""Fusion fall-detection module for Raspberry Pi 4B elderly monitoring.

Standalone: ``import fall_fusion`` from ``main_pi.py`` (or run directly for self-test).
No hardware imports at module import time -- RPi.GPIO / smbus are lazily
imported inside the ``*_live`` helper functions only, so this file is importable
and unit-testable on any machine with stdlib only.

Data shapes reused from ``main_pi.py`` (do NOT modify that file):
  * radar targets: list of dicts ``{dist, x, y, speed}`` produced by
    ``ld2450_worker`` (UART2 ``/dev/ttyAMA2`` 256000). ``dist`` is metres
    (``sqrt(x*x+y*y)/1000``), ``x/y/speed`` are raw LD2450 units.
    Only targets with ``dist > 0`` are valid (see main loop ``valid_targets``).
  * MPU6050 raw accel: signed ints from ``read_raw_data(bus, addr)`` on bus 1,
    addr ``0x68``, registers ``ACCEL_XOUT_H (0x3B)`` / +2 / +4.
  * ``SharedState`` fields consumed: ``radar_targets``; produced: nothing
    (caller decides what to do with the returned event dict).

Fusion idea: an MPU impact spike FOLLOWED BY radar stillness within T seconds
= FALL. Either signal alone = SUSPECT observation only (never an alarm).
A stillness confirmation window plus an alarm cooldown cut false positives.

Typical wiring into ``main_pi.py`` (example, not executed here)::

    from fall_fusion import FallDetector
    det = FallDetector()
    # in the main loop, replacing/augmenting section 6:
    #   event = det.update(ax, ay, az, curr_targets)
    #   if event and event["type"] == "FALL":
    #       requests.post(FALL_API_URL, json=..., timeout=2)

Target interpreter: Python 3.13 on the Pi. Stdlib only.
"""

from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timezone

# Default alarm log. Compatible event dict: {time, type, detail}.
# ``main_pi.py`` currently POSTs fall alerts to a backend; this module instead
# appends a JSON line locally AND returns the event dict -- it never does
# network I/O itself (no requests/Flask), so the caller chooses how to forward.
DEFAULT_ALARM_FILE = r"D:\esp32-butler\alarms.jsonl"

# Button wiring (documentation contract):
# Physical button on BCM GPIO17 (see main_pi.py BUTTON_PIN = 17, PUD_UP).
# A press there is treated as an explicit help call -> immediate FALL alarm
# via button_pressed_to_event(). Wiring/monitoring of the pin itself stays in
# main_pi.py's sensor_loop; this module only builds the event dict.
BUTTON_PIN = 17


# ---------------------------------------------------------------------------
# Platform-layer config (rules.json)
# ---------------------------------------------------------------------------

def load_rules(path: str | None = None) -> dict:
    """Load fusion tunables from a JSON file (platform-layer config).

    Args:
        path: JSON file path. Defaults to ``rules.json`` next to this file
            (on the Pi that is ``/home/admin/food_project/rules.json``).

    Returns:
        The parsed dict, or ``{}`` silently when the file is missing,
        corrupt, or not a JSON object. Stdlib only, no I/O besides the read.
    """
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _rule_or_explicit(rules: dict, key: str, explicit: float, default: float) -> float:
    """Resolve one tunable: explicit constructor arg wins over rules.json.

    The rules value for ``key`` is used only when the caller left the
    constructor arg at its built-in ``default``; unknown keys are never
    consulted here, so they are ignored. Missing or non-numeric rules values
    fall back to ``default`` so a bad config can never crash the monitor.
    """
    if explicit != default:
        return float(explicit)
    try:
        return float(rules[key])
    except (KeyError, TypeError, ValueError):
        return float(default)


# ---------------------------------------------------------------------------
# Pure helpers (unit-testable, no I/O, no time dependency)
# ---------------------------------------------------------------------------

def accel_magnitude(ax: float, ay: float, az: float) -> float:
    """Magnitude of a raw MPU6050 accel sample.

    Args:
        ax, ay, az: raw signed ints as returned by ``read_raw_data``.

    Returns:
        Euclidean norm ``sqrt(ax^2 + ay^2 + az^2)`` in the same raw units.
        At rest this is ~1g ~= 16384 LSB (MPU6050 +/-2g full scale).
    """
    return math.sqrt(float(ax) * float(ax) + float(ay) * float(ay) + float(az) * float(az))


def target_speed(target: dict | None) -> float | None:
    """Extract ``|speed|`` from a target dict, or None if unusable."""
    if target is None:
        return None
    try:
        return abs(float(target.get("speed", 0.0)))
    except (TypeError, ValueError):
        return None


def utc_now_iso() -> str:
    """Current UTC time as ISO string (same style as main_pi.py temp upload)."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# FallDetector
# ---------------------------------------------------------------------------

class FallDetector:
    """MPU-spike + LD2450-stillness fusion fall detector.

    All tunables are ``__init__`` params (documented below). State is plain
    instance attributes; inject ``now`` (epoch seconds) in updates for tests.
    """

    def __init__(
        self,
        # -- MPU spike side ---------------------------------------------
        # accel_baseline: rest magnitude ~= 1g = 16384 LSB at +/-2g scale.
        #   Reference, not a gate; documents what "jump vs 1g" means.
        accel_baseline: float = 16384.0,
        # spike_threshold: magnitude above this = impact spike.
        #   Default 25000.0 mirrors main_pi.py sensor_loop step_threshold
        #   = 25000, so behaviour is consistent with the existing code's
        #   sense of "a vigorous motion event" in raw LSB units.
        spike_threshold: float = 25000.0,
        # spike_cooldown: seconds to ignore further spikes after one fires.
        #   Debounce so a single impact (ringing samples) counts once.
        #   Longer than the 0.3 s step debounce because falls need only one.
        spike_cooldown: float = 1.0,
        # -- radar stillness side ----------------------------------------
        # active_speed_threshold: motion speed at/above this = person moving.
        #   Motion speed prefers coordinate-displacement (sees lateral
        #   walking; LD2450 'speed' field is radial-only and reads ~0 for
        #   tangential movement). Default 30.0 cm/s implements the
        #   ">0.3 m/s counts as motion" rule calibrated 2026-10-13 from a
        #   90 s live trace: radial speed stayed <=8 cm/s while the target
        #   demonstrably moved (dist 0.5-0.9 m, changing every phase).
        active_speed_threshold: float = 30.0,
        # still_speed_threshold: motion speed below this = (near-)motionless.
        #   Same displacement metric; lying person's breathing residual
        #   stays well under 30 cm/s after EMA smoothing (trace: noise),
        #   while sitting jitter stays above 8 and under 30.
        still_speed_threshold: float = 30.0,
        # stillness_duration (N seconds): still speed must persist this long
        #   AFTER a prior active period to count as confirmed stillness.
        #   This is the N-second confirmation window that rejects momentary
        #   pauses and radar dropouts.
        stillness_duration: float = 3.0,
        # -- fusion / alarm ----------------------------------------------
        # alarm_mode: "fusion" (default; MPU impact spike + stillness) or
        #   "transition" (radar-only speed-drop edge: motion >= still
        #   threshold within ``transition_window`` seconds before confirmed
        #   stillness = FALL, no MPU evidence needed). Rules key
        #   ``alarm_mode`` overrides the constructor default.
        alarm_mode: str = "fusion",
        # transition_window: max seconds between the last motion frame and the
        #   stillness EDGE (first still speed frame) in "transition" mode —
        #   the "drop to <0.3 must happen within 3 s of moving" rule. Alarm
        #   fires stillness_duration later; confirmed-then gap is not eaten by
        #   EMA decay because the edge is measured at still-since, not now.
        #   Default 3.0 mirrors the 2026-10-14 spec; longer windows catch slow
        #   collapses but ordinary sitting-down maps to the same signature
        #   (accepted tradeoff of radar-only).
        transition_window: float = 3.0,
        # fusion_window (T seconds): confirmed stillness must complete within
        #   this long AFTER the spike to fuse into FALL (spike THEN stillness).
        #   A lone spike that never leads to stillness stays SUSPECT.
        fusion_window: float = 10.0,
        # alarm_cooldown: minimum seconds between FALL alarms.
        #   Default 10.0 mirrors main_pi.py section 6 guard
        #   (current_time - last_fall_time >= 10.0).
        alarm_cooldown: float = 10.0,
        # alarm_file: JSON-lines log path; each FALL appends one line
        #   {time, type, detail}. Caller may pass an alternate path in tests.
        alarm_file: str = DEFAULT_ALARM_FILE,
        # pos_gate_mm (2026-10-14 identity fix): max mm between the tracked
        #   target and the previous frame's position for the SAME physical
        #   object. Beyond this, the candidate is a DIFFERENT reflection
        #   (slot teleport) and the track is dropped -> ABSENT, never
        #   counted as motion. Walking at 1.2 m/s spans ~160 mm per 0.1 s
        #   frame; LD2450 slot swaps are >=300 mm, so 600 mm separates them.
        pos_gate_mm: float = 600.0,
        # rules: platform-layer config dict as returned by load_rules()
        #   (rules.json next to this file). Applied only for keys present;
        #   an explicitly passed constructor arg always wins over rules;
        #   unknown keys are ignored. None/{} keeps built-in defaults.
        rules: dict | None = None,
    ) -> None:
        rules = dict(rules) if rules else {}
        self.accel_baseline = float(accel_baseline)
        self.spike_threshold = _rule_or_explicit(rules, "spike_threshold", spike_threshold, 25000.0)
        self.spike_cooldown = float(spike_cooldown)
        self.active_speed_threshold = _rule_or_explicit(rules, "active_speed_threshold", active_speed_threshold, 30.0)
        self.still_speed_threshold = _rule_or_explicit(rules, "still_speed_threshold", still_speed_threshold, 30.0)
        self.stillness_duration = _rule_or_explicit(rules, "stillness_duration", stillness_duration, 3.0)
        self.fusion_window = _rule_or_explicit(rules, "fusion_window", fusion_window, 10.0)
        self.alarm_cooldown = _rule_or_explicit(rules, "alarm_cooldown", alarm_cooldown, 10.0)
        self.transition_window = _rule_or_explicit(rules, "transition_window", transition_window, 2.0)
        _mode = rules.get("alarm_mode")
        self.alarm_mode = str(_mode) if _mode in ("fusion", "transition") else str(alarm_mode)
        self.alarm_file = alarm_file
        self.pos_gate_mm = _rule_or_explicit(rules, "pos_gate_mm", pos_gate_mm, 600.0)

        self._last_spike_time: float | None = None   # last accepted spike
        self._last_alarm_time: float = 0.0           # epoch; 0 => first alarm free
        self._had_active: bool = False               # seen motion before stillness
        self._still_since: float | None = None       # start of current still run
        self._last_speed: float | None = None
        # displacement-velocity state (sees lateral motion radial speed misses)
        self._last_pos: tuple[float, float, float] | None = None  # (x, y, t)
        self._disp_speed: float = 0.0                # EMA cm/s, displacement-based
        self._last_active_time: float | None = None  # transition mode: last motion frame

    # -- introspection ----------------------------------------------------
    @property
    def last_spike_time(self) -> float | None:
        return self._last_spike_time

    @property
    def still_since(self) -> float | None:
        return self._still_since

    def stillness_elapsed(self, now: float) -> float | None:
        """Seconds of continuous stillness up to ``now``, or None."""
        if self._still_since is None:
            return None
        return max(0.0, float(now) - self._still_since)

    def reset(self) -> None:
        """Clear fusion state (keeps alarm cooldown + config)."""
        self._last_spike_time = None
        self._had_active = False
        self._still_since = None
        self._last_speed = None
        self._last_pos = None
        self._disp_speed = 0.0
        self._last_active_time = None

    # -- per-sensor updates ------------------------------------------------
    def update_mpu(self, ax: float, ay: float, az: float, now: float | None = None) -> bool:
        """Feed one raw MPU sample. Returns True on a fresh accepted spike.

        A spike is ``accel_magnitude(ax,ay,az) > spike_threshold`` and outside
        ``spike_cooldown`` of the previous accepted spike.
        """
        t = time.time() if now is None else float(now)
        mag = accel_magnitude(ax, ay, az)
        if mag <= self.spike_threshold:
            return False
        if self._last_spike_time is not None and (t - self._last_spike_time) < self.spike_cooldown:
            return False
        self._last_spike_time = t
        return True

    def update_radar(self, targets, now: float | None = None) -> str:
        """Feed one radar frame. Returns ``"ACTIVE"`` / ``"STILL"`` / ``"ABSENT"``.

        Motion speed is displacement-based when coordinates are available:
        LD2450's ``speed`` field is radial-only, so lateral walking reads ~0
        and a radial-speed-only state machine never sees the target as
        active (calibrated 2026-10-13 from a 90 s live trace). Coordinate
        velocity is computed between consecutive frames and smoothed with
        an EMA; the emitted motion speed is the max of displacement and
        |radial| so approach/retreat瞬时 bursts still count.

        * ``ABSENT``: no valid target (all ``dist <= 0``) -- stillness run reset
          and displacement tracker dropped (position identity cannot be held
          across a dropout).
        * ``ACTIVE``: motion speed >= still_speed_threshold. Also marks
          ``_had_active`` when >= active_speed_threshold, and resets any still
          run (motion breaks stillness).
        * ``STILL``: valid target with motion speed < still_speed_threshold --
          starts/continues the still run. Confirmation (elapsed >=
          ``stillness_duration`` with prior ``_had_active``) is checked in
          :meth:`stillness_confirmed`.
        """
        t = time.time() if now is None else float(now)
        # --- identity-aware target pick (2026-10-14 fix) ---
        # Naive "first valid target" tracking teleports between LD2450 slots
        # whenever multiple reflectors swap order (chair/second person/ghost):
        # a 1 m slot jump looks like 10 m/s phantom motion -> false alarms.
        # Nearest-neighbor association instead: follow the target closest to
        # the previously tracked position, but ONLY within ``pos_gate_mm``
        # per frame; beyond the gate the identity is LOST -> ABSENT (never
        # teleport). With no prior position, fall back to first valid.
        valid: list = []
        for t_item in targets:
            try:
                if float(t_item.get("dist", 0)) > 0:
                    valid.append(t_item)
            except (AttributeError, TypeError, ValueError):
                continue
        tgt: dict | None = None
        if self._last_pos is not None:
            best: dict | None = None
            best_d: float | None = None
            for cand in valid:
                cand_x = cand.get("x")
                cand_y = cand.get("y")
                if not (isinstance(cand_x, (int, float)) and isinstance(cand_y, (int, float))):
                    continue
                d = math.hypot(float(cand_x) - self._last_pos[0], float(cand_y) - self._last_pos[1])
                if best_d is None or d < best_d:
                    best_d = d
                    best = cand
            if best is not None and best_d is not None and best_d <= self.pos_gate_mm:
                tgt = best
            # else: identity lost -> tgt stays None -> ABSENT below
        else:
            tgt = valid[0] if valid else None
        spd = target_speed(tgt)
        self._last_speed = spd
        if spd is None:
            self._still_since = None
            self._last_pos = None
            self._disp_speed = 0.0
            return "ABSENT"

        motion_speed: float | None = None  # disp-only; radial kept informational
        x = tgt.get("x")
        y = tgt.get("y")
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            px, py, pt = self._last_pos if self._last_pos else (None, None, None)
            dt = float(t) - float(pt) if pt is not None else 0.0
            if dt > 0.0 and dt < 2.0:  # >2 s gap = identity broken, no teleport burst
                d_mm = math.hypot(float(x) - float(px), float(y) - float(py))
                inst_cm = min(300.0, (d_mm / 10.0) / dt)
            elif dt >= 2.0 or (dt <= 0.0 and self._last_pos is not None):
                inst_cm = 0.0
            else:
                inst_cm = 0.0
            # EMA smoothing: slow attack (0.15) so an ISOLATED noise frame --
            # radial speed pulse or a <=600 mm coordinate glitch from a
            # reflector passing through the gate -- cannot reach the alarm
            # threshold; sustained real walking (>=0.3 s of >=100 cm/s)
            # arms within ~0.3 s of onset. Decay (0.85) clears stillness
            # fast enough for the 3 s stillness window.
            self._disp_speed = (0.15 * inst_cm + 0.85 * self._disp_speed)
            self._last_pos = (float(x), float(y), t)
            # Displacement is the ONLY motion evidence (radial is
            # informational now): it sees lateral walks, and radial-field
            # noise pulses on a stationary person can no longer fabricate
            # motion frames.
            motion_speed = self._disp_speed

        if motion_speed is None:
            self._still_since = None
            return "ABSENT"
        if motion_speed >= self.still_speed_threshold:
            if motion_speed >= self.active_speed_threshold:
                self._had_active = True
            self._still_since = None
            self._last_active_time = t
            return "ACTIVE"
        if self._still_since is None:
            self._still_since = t
        return "STILL"

    def stillness_confirmed(self, now: float | None = None) -> bool:
        """True once stillness has persisted ``stillness_duration`` after activity."""
        t = time.time() if now is None else float(now)
        if not self._had_active or self._still_since is None:
            return False
        return (t - self._still_since) >= self.stillness_duration

    # -- fusion -------------------------------------------------------------
    def check_fusion(self, now: float | None = None) -> dict | None:
        """Evaluate the alarm rule; emit FALL (file + return) or SUSPECT (return).

        Two modes (``alarm_mode``, rules.json switchable):

        * ``fusion`` (default): MPU spike recorded AND stillness confirmed
          within ``fusion_window`` after the spike -> FALL. Spike alone or
          stillness alone -> SUSPECT. Preserves the dual-signal evidence
          chain (impact + then-motionless) against false positives.
        * ``transition`` (2026-10-13/14 spec): radar-only speed-drop edge.
          Speed > still threshold (motion) must drop below the still edge
          ``transition_window`` seconds after the last motion frame; alarm
          fires ``stillness_duration`` after that edge. ``transition_window``
          shapes the tradeoff: strict keeps the "sudden" character of falls;
          longer windows catch slow collapses but ordinary sitting-down maps
          to the same signature, so the false-positive rate must be
          re-measured (P2).
        * neither -> None; SUSPECT is never written to the alarm file nor
          cooldown-bound.

        After a FALL is emitted the alarm state resets (cooldown retained)
        so one event yields one alarm.
        """
        t = time.time() if now is None else float(now)
        if self.alarm_mode == "transition":
            # Edge semantics (2026-10-14 spec ">: >0.3 then <0.3 within 3 s"):
            # the stillness EDGE must appear within ``transition_window``
            # seconds AFTER the last motion frame; the alarm then fires when
            # stillness completes (stillness_duration later). Measuring the
            # edge via ``_still_since`` (first still frame) avoids the EMA
            # decay lag eating windows.
            edge_ok = (
                self._last_active_time is not None
                and self._still_since is not None
                and 0.0 <= (self._still_since - self._last_active_time) <= self.transition_window
            )
            spike_ok = edge_ok
        else:
            spike_ok = (
                self._last_spike_time is not None
                and 0.0 <= (t - self._last_spike_time) <= self.fusion_window
            )
        still_ok = self.stillness_confirmed(t)
        if spike_ok and still_ok:
            detail = (
                "speed-drop to stillness (transition mode)"
                if self.alarm_mode == "transition"
                else "spike-then-stillness fusion"
            )
            event = self._emit_fall(detail, now=t)
            self.reset()
            return event  # None if suppressed by alarm cooldown
        if spike_ok:
            return {
                "time": utc_now_iso(),
                "type": "SUSPECT",
                "detail": (
                    "motion before stillness not yet confirmed (transition)"
                    if self.alarm_mode == "transition"
                    else "MPU spike without confirmed stillness"
                ),
            }
        if still_ok:
            return {
                "time": utc_now_iso(),
                "type": "SUSPECT",
                "detail": "prolonged stillness without MPU spike",
            }
        return None

    def update(self, ax: float, ay: float, az: float, targets, now: float | None = None) -> dict | None:
        """One-call fusion step for ``main_pi.py``: MPU sample + radar frame.

        Returns the FALL event dict (also appended to the alarm file), a
        SUSPECT dict (not logged), or None. Never raises on bad sensor data
        and never performs network I/O.
        """
        t = time.time() if now is None else float(now)
        try:
            self.update_mpu(ax, ay, az, now=t)
        except (TypeError, ValueError):
            pass
        try:
            self.update_radar(targets, now=t)
        except (TypeError, ValueError, AttributeError):
            pass
        return self.check_fusion(now=t)

    # -- alarm output ---------------------------------------------------------
    def _emit_fall(self, detail: str, now: float | None = None) -> dict | None:
        """Build a FALL event; append one JSON line unless in cooldown.

        Event shape ``{time, type, detail}`` matches the required log schema.
        Returns the event dict, or None when suppressed by ``alarm_cooldown``.
        """
        t = time.time() if now is None else float(now)
        if (t - self._last_alarm_time) < self.alarm_cooldown:
            return None
        event = {"time": utc_now_iso(), "type": "FALL", "detail": str(detail)}
        try:
            parent = os.path.dirname(os.path.abspath(self.alarm_file))
            if parent and not os.path.exists(parent):
                os.makedirs(parent, exist_ok=True)
            with open(self.alarm_file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError:
            # Logging must never break detection; event is still returned.
            pass
        self._last_alarm_time = t
        return event


# ---------------------------------------------------------------------------
# Button GPIO17 -- immediate FALL alarm source
# ---------------------------------------------------------------------------

def button_pressed_to_event(detail: str = "button GPIO17 pressed") -> dict:
    """Build the immediate FALL event for a press of the button on GPIO17.

    The button (BCM pin 17, see ``BUTTON_PIN``) is the manual help call: unlike
    the fusion path it needs no spike/stillness evidence and no cooldown is
    applied here -- the caller (``main.py``) decides debouncing and logging via
    ``det._emit_fall(...)``. Pin monitoring itself (GPIO.setup/input) stays in
    ``main.py``; hardware is NOT touched in this module so it stays
    import-safe and testable.

    Returns:
        Event dict ``{time, type: "FALL", detail}`` naming GPIO17 as source.
    """
    return {
        "time": utc_now_iso(),
        "type": "FALL",
        "detail": f"{detail} (immediate alarm source: button GPIO{BUTTON_PIN})",
    }


# ---------------------------------------------------------------------------
# __main__ self-test on synthetic data (no hardware, no network)
# ---------------------------------------------------------------------------

def _rest_sample():
    """MPU at rest: 1g on Z only (~16384 LSB)."""
    return (0, 0, 16384)


def _walk_target(step: int) -> dict:
    """Synthetic walking target: 0.6 m/s LATERAL walk.

    x advances 60 mm per 0.1 s tick (= 60 cm/s displacement) while the
    radial ``speed`` field reads LOW (as the real LD2450 does for
    tangential movement). Tests the displacement-based motion metric.
    """
    return {"dist": 1.5, "x": 100.0 + step * 60.0, "y": 1500.0, "speed": 4.0}


def _still_target() -> dict:
    """Synthetic post-fall target: valid dist, near-zero speed."""
    return {"dist": 1.2, "x": 50.0, "y": 1200.0, "speed": 4.0}


def _run_case(name: str, fn) -> bool:
    try:
        ok, note = fn()
    except Exception as exc:  # noqa: BLE001 -- self-test must report, not crash
        print(f"FAIL: {name} -- raised {type(exc).__name__}: {exc}")
        return False
    print(f"{'PASS' if ok else 'FAIL'}: {name} -- {note}")
    return bool(ok)


def _case_normal_walk():
    """Normal walk: rest-level accel + active radar -> never FALL/SUSPECT-alarm."""
    import tempfile

    path = os.path.join(tempfile.gettempdir(), "fall_fusion_test_walk.jsonl")
    try:
        os.remove(path)
    except OSError:
        pass
    det = FallDetector(alarm_file=path)
    t0 = 1_700_000_000.0
    saw_fall = False
    for i in range(40):  # 4 s @ 10 Hz
        t = t0 + i * 0.1
        ev = det.update(*_rest_sample(), [_walk_target(i)], now=t)
        if ev is not None and ev.get("type") == "FALL":
            saw_fall = True
    logged = os.path.exists(path)
    ok = not saw_fall and not logged
    return ok, "no FALL over 4 s of synthetic walk" + ("" if ok else " (unexpected alarm)")


def _case_spike_then_stillness():
    """Sudden spike + stillness -> must produce a FALL alarm (file + return)."""
    import tempfile

    path = os.path.join(tempfile.gettempdir(), "fall_fusion_test_fall.jsonl")
    try:
        os.remove(path)
    except OSError:
        pass
    det = FallDetector(alarm_file=path)
    t0 = 1_700_000_000.0
    # 1 s of walking first (marks _had_active), then one impact spike ...
    for i in range(10):
        det.update(*_rest_sample(), [_walk_target(i)], now=t0 + i * 0.1)
    spike_t = t0 + 1.0
    det.update(0, 0, 31000, [_walk_target(11)], now=spike_t)  # > 25000 spike
    # ... then near-zero speed held past stillness_duration (3 s default).
    alarm = None
    for k in range(40):  # 4 s @ 10 Hz
        alarm = det.update(*_rest_sample(), [_still_target()], now=spike_t + 0.1 * (k + 1))
        if alarm is not None and alarm.get("type") == "FALL":
            break
    try:
        with open(path, encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        logged = len(lines) == 1 and json.loads(lines[0]).get("type") == "FALL"
    except OSError:
        logged = False
    ok = (
        alarm is not None
        and alarm.get("type") == "FALL"
        and set(("time", "type", "detail")) <= set(alarm)
        and logged
    )
    return ok, "spike+stillness raised FALL and wrote 1 jsonl line" if ok else "expected FALL alarm, got none"


def _case_lone_spike():
    """Lone spike, motion continues -> must NOT alarm (SUSPECT at most)."""
    import tempfile

    path = os.path.join(tempfile.gettempdir(), "fall_fusion_test_lone.jsonl")
    try:
        os.remove(path)
    except OSError:
        pass
    det = FallDetector(alarm_file=path)
    t0 = 1_700_000_000.0
    for i in range(10):
        det.update(*_rest_sample(), [_walk_target(i)], now=t0 + i * 0.1)
    spike_t = t0 + 1.0
    first = det.update(0, 0, 31000, [_walk_target(11)], now=spike_t)
    saw_fall = bool(first and first.get("type") == "FALL")
    # Keep walking: stillness never confirms, so no fusion.
    for k in range(40):
        ev = det.update(*_rest_sample(), [_walk_target(12 + k)], now=spike_t + 0.1 * (k + 1))
        if ev is not None and ev.get("type") == "FALL":
            saw_fall = True
    logged = os.path.exists(path)
    ok = not saw_fall and not logged
    return ok, "lone spike stayed SUSPECT/None, no FALL, no file" if ok else "lone spike wrongly alarmed"


def _case_lateral_walk_activates():
    """Lateral walk with radial speed ~0 must still mark the target active.

    Regression guard for the 2026-10-13 calibration: LD2450 'speed' is
    radial-only, so displacement-based velocity must carry the ACTIVE
    decision or slow walks never prime ``_had_active`` and falls cannot fuse.
    """
    det = FallDetector(alarm_file=os.devnull)
    t0 = 1_700_000_000.0
    states = []
    for i in range(20):  # 2 s of 0.6 m/s lateral walking
        states.append(det.update_radar([_walk_target(i)], now=t0 + i * 0.1))
    ok = "ACTIVE" in states and det._had_active
    return ok, "lateral walk marked ACTIVE and primed had_active" if ok else (
        "states=%r had_active=%r" % (states[-3:], det._had_active))


def _case_transition_drop_without_spike():
    """Transition mode: speed drop 30->still 3s with NO MPU spike = FALL.

    Regression for the 2026-10-13 requirement: alarm must not depend on
    the accelerometer. Same datastream but rest-level accel throughout.
    """
    import tempfile

    path = os.path.join(tempfile.gettempdir(), "fall_fusion_test_trans.jsonl")
    try:
        os.remove(path)
    except OSError:
        pass
    det = FallDetector(alarm_file=path, alarm_mode="transition")
    t0 = 1_700_000_000.0
    for i in range(10):  # 1 s of lateral walking (radial ~0, displacement 60)
        det.update(*_rest_sample(), [_walk_target(i)], now=t0 + i * 0.1)
    alarm = None
    for k in range(45):  # 4.5 s lying still, rest-level accel, no spike ever
        alarm = det.update(*_rest_sample(), [_still_target()], now=t0 + 1.0 + 0.1 * (k + 1))
        if alarm is not None and alarm.get("type") == "FALL":
            break
    ok = alarm is not None and alarm.get("type") == "FALL"
    return ok, "transition mode raised FALL without any MPU spike" if ok else "expected FALL in transition mode, got none"


def _case_transition_old_walk_far_no_fall():
    """Transition mode: stale activity (person gone, returned, lies) -> no FALL.

    Motion must be recent (<= transition_window) when stillness confirms.
    Walk 1 s, walk OUT of radar view for 10 s (ABSENT), then lie still:
    at stillness-confirm time the last ACTIVE frame is ~11 s stale, so no
    FALL may fire (SUSPECT/None only).
    """
    det = FallDetector(alarm_file=os.devnull, alarm_mode="transition")
    t0 = 1_700_000_000.0
    for i in range(10):  # 1 s walking
        det.update(*_rest_sample(), [_walk_target(i)], now=t0 + i * 0.1)
    for k in range(100):  # 10 s with nobody in radar view (ABSENT)
        det.update(*_rest_sample(), [], now=t0 + 1.0 + 0.1 * (k + 1))
    saw_fall = False
    for k in range(50):  # 5 s lying still after returning
        ev = det.update(*_rest_sample(), [_still_target()], now=t0 + 11.0 + 0.1 * (k + 1))
        if ev is not None and ev.get("type") == "FALL":
            saw_fall = True
    return (not saw_fall), "stale activity (gone 10s, lie still) stayed SUSPECT/None" if not saw_fall else "stale activity wrongly fused"


def _case_teleport_decoy_no_fall():
    """Identity fix: far decoy slot-swap must NOT fabricate motion.

    Person tracked at A; a chair-like DECoy reflection appears closer to
    the slot front every frame (1.2 m away from A, beyond 600 mm gate).
    Old first-valid logic would teleport to the decoy -> giant phantom
    speeds -> ACTIVE storm -> false FALL on the next stillness. New
    nearest-neighbor with pos_gate ignores the decoy: stillness on the
    real target keeps accumulating.
    """
    det = FallDetector(
        alarm_file=os.devnull,
        alarm_mode="transition",
        rules={"active_speed_threshold": 100.0, "still_speed_threshold": 100.0, "pos_gate_mm": 600.0},
    )
    t0 = 1_700_000_000.0
    near = {"dist": 0.8, "x": 100.0, "y": 1500.0, "speed": 0.0}
    far_decoy = {"dist": 2.0, "x": 100.0, "y": 2700.0, "speed": 60.0}  # 1200 mm away, "moving"
    states = []
    for i in range(30):  # 3 s: tracked person STILL, decoy noisy
        states.append(det.update_radar([far_decoy, near], now=t0 + i * 0.1))
    ok = all(s == "STILL" for s in states) and not det._had_active
    return ok, "slot teleport ignored, real target stillness preserved" if ok else (
        "states=%r had_active=%r disp=%r" % (states[-3:], det._had_active, det._disp_speed))


def _case_button():
    """GPIO17 helper returns an immediate FALL event naming the pin."""
    ev = button_pressed_to_event()
    ok = (
        ev.get("type") == "FALL"
        and "GPIO17" in str(ev.get("detail", ""))
        and set(("time", "type", "detail")) <= set(ev)
    )
    return ok, "button helper -> FALL mentioning GPIO17" if ok else f"bad button event: {ev!r}"


def _case_cooldown():
    """Second FALL inside 10 s alarm cooldown is suppressed."""
    import tempfile

    path = os.path.join(tempfile.gettempdir(), "fall_fusion_test_cd.jsonl")
    try:
        os.remove(path)
    except OSError:
        pass
    det = FallDetector(alarm_file=path)
    e1 = det._emit_fall("first", now=1000.0)
    e2 = det._emit_fall("second", now=1005.0)  # within default 10 s cooldown
    ok = e1 is not None and e1.get("type") == "FALL" and e2 is None
    return ok, "cooldown suppressed 2nd alarm at +5 s" if ok else "cooldown failed"


def main() -> int:
    print("fall_fusion self-test (synthetic data, no hardware, no network)")
    results = [
        _run_case("normal walk -> no alarm", _case_normal_walk),
        _run_case("spike + stillness -> FALL alarm", _case_spike_then_stillness),
        _run_case("lone spike (motion continues) -> NO alarm", _case_lone_spike),
        _run_case("lateral walk (radial~0) -> ACTIVE", _case_lateral_walk_activates),
        _run_case("transition: drop 30->still3s, no spike -> FALL", _case_transition_drop_without_spike),
        _run_case("transition: stale activity (gone 10s) -> no FALL", _case_transition_old_walk_far_no_fall),
        _run_case("teleport decoy vs real target -> no phantom motion", _case_teleport_decoy_no_fall),
        _run_case("button GPIO17 -> immediate FALL event", _case_button),
        _run_case("10 s alarm cooldown suppresses repeat", _case_cooldown),
    ]
    passed = sum(results)
    print(f"{passed}/{len(results)} cases passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
