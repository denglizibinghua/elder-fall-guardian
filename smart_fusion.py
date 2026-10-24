"""Quality-gated, explainable fall scoring for Raspberry Pi.

The detector keeps the proven displacement-based transition rule as a hard
gate, then applies a tiny logistic model to the short temporal window.  It
does not require numpy, scikit-learn, cloud access, or a second board.
"""

from __future__ import annotations

import json
import math
import os
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _sigmoid(value: float) -> float:
    if value >= 35:
        return 1.0
    if value <= -35:
        return 0.0
    return 1.0 / (1.0 + math.exp(-value))


class SmartFallDetector:
    """Two-stage transition gate plus a lightweight logistic scorer."""

    DEFAULT_MODEL = {
        "model_version": "smart-v1",
        "type": "logistic",
        "threshold": 0.60,
        "bias": -2.25,
        "weights": {
            "speed_peak_3s": 1.05,
            "drop_slope": 0.85,
            "still_duration": 0.95,
            "still_variance": -0.65,
            "track_quality": 0.75,
            "presence": 0.35,
            "imu_delta": 0.12,
        },
        "normalization": {
            "speed_peak_3s": 150.0,
            "drop_slope": 1000.0,
            "still_duration": 5.0,
            "still_variance": 2500.0,
            "track_quality": 1.0,
            "presence": 1.0,
            "imu_delta": 16000.0,
        },
    }

    def __init__(
        self,
        *,
        alarm_file: str,
        rules: dict | None = None,
        model_path: str | None = None,
        device_id: str = "pi-01",
        room_id: str = "living-room",
    ):
        rules = dict(rules or {})
        self.alarm_file = alarm_file
        self.device_id = device_id
        self.room_id = room_id
        self.active_speed_threshold = _safe_float(rules.get("active_speed_threshold"), 100.0)
        self.still_speed_threshold = _safe_float(rules.get("still_speed_threshold"), 50.0)
        self.stillness_duration = _safe_float(rules.get("stillness_duration"), 3.0)
        self.transition_window = _safe_float(rules.get("transition_window"), 3.0)
        self.alarm_cooldown = _safe_float(rules.get("alarm_cooldown"), 30.0)
        self.pos_gate_mm = _safe_float(rules.get("pos_gate_mm"), 600.0)
        self.model = self._load_model(model_path)
        self.model_version = str(self.model.get("model_version", "smart-v1"))

        self._last_pos: tuple[float, float, float] | None = None
        self._disp_speed = 0.0
        self._last_active_time: float | None = None
        self._still_since: float | None = None
        self._had_active = False
        self._speed_peak = 0.0
        self._speed_at_edge = 0.0
        self._good_frames = 0
        self._lost_frames = 0
        self._still_positions: deque[tuple[float, float]] = deque(maxlen=60)
        self._last_alarm_time = 0.0
        self._seq = 0
        self.last_state = "WAITING"
        self.last_score = 0.0
        self.last_evidence: dict[str, Any] = {}

    @classmethod
    def _load_model(cls, path: str | None) -> dict:
        model = dict(cls.DEFAULT_MODEL)
        if path:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    candidate = json.load(fh)
                if isinstance(candidate, dict):
                    for key in ("model_version", "type", "threshold", "bias", "weights", "normalization"):
                        if key in candidate:
                            model[key] = candidate[key]
            except (OSError, ValueError):
                pass
        return model

    @property
    def state(self) -> str:
        return self.last_state

    def snapshot(self) -> dict:
        return {
            "state": self.last_state,
            "confidence": round(self.last_score, 4),
            "model_version": self.model_version,
            # This is the same nearest-neighbor + EMA speed used by the
            # detector. The dashboard should render this value instead of
            # estimating speed from browser delivery timing.
            "motion_speed_cm_s": round(max(0.0, self._disp_speed), 2),
            "stillness_elapsed": (
                None if self._still_since is None else round(max(0.0, time.time() - self._still_since), 3)
            ),
            "evidence": dict(self.last_evidence),
        }

    def reset(self, *, keep_cooldown: bool = True) -> None:
        self._clear_tracking()
        self.last_state = "WAITING"
        self.last_score = 0.0
        self.last_evidence = {}
        if not keep_cooldown:
            self._last_alarm_time = 0.0

    def _clear_tracking(self) -> None:
        self._last_pos = None
        self._disp_speed = 0.0
        self._last_active_time = None
        self._still_since = None
        self._had_active = False
        self._speed_peak = 0.0
        self._speed_at_edge = 0.0
        self._good_frames = 0
        self._lost_frames = 0
        self._still_positions.clear()

    def _pick_target(self, targets: list[dict]) -> dict | None:
        valid = []
        for item in targets or []:
            if not isinstance(item, dict):
                continue
            if _safe_float(item.get("dist"), 0.0) <= 0:
                continue
            if not isinstance(item.get("x"), (int, float)) or not isinstance(item.get("y"), (int, float)):
                continue
            valid.append(item)
        if not valid:
            return None
        if self._last_pos is None:
            return valid[0]
        previous_x, previous_y, _ = self._last_pos
        best = min(
            valid,
            key=lambda item: math.hypot(float(item["x"]) - previous_x, float(item["y"]) - previous_y),
        )
        distance = math.hypot(float(best["x"]) - previous_x, float(best["y"]) - previous_y)
        return best if distance <= self.pos_gate_mm else None

    def _features(self, *, still_duration: float, presence: bool, imu_delta: float) -> dict[str, float]:
        if self._still_positions:
            mean_x = sum(point[0] for point in self._still_positions) / len(self._still_positions)
            mean_y = sum(point[1] for point in self._still_positions) / len(self._still_positions)
            variance = sum(
                (point[0] - mean_x) ** 2 + (point[1] - mean_y) ** 2
                for point in self._still_positions
            ) / len(self._still_positions)
        else:
            variance = 0.0
        total = self._good_frames + self._lost_frames
        quality = self._good_frames / total if total else 0.0
        edge_gap = max(0.05, (self._still_since or 0.0) - (self._last_active_time or 0.0))
        drop_slope = max(0.0, (self._speed_at_edge - self._disp_speed) / edge_gap)
        return {
            "speed_peak_3s": min(300.0, self._speed_peak),
            "drop_slope": min(3000.0, drop_slope),
            "still_duration": max(0.0, still_duration),
            "still_variance": min(10000.0, variance),
            "track_quality": max(0.0, min(1.0, quality)),
            "presence": 1.0 if presence else 0.0,
            "imu_delta": min(32000.0, max(0.0, imu_delta)),
        }

    def _score(self, features: dict[str, float]) -> float:
        weights = self.model.get("weights", {})
        normalization = self.model.get("normalization", {})
        value = _safe_float(self.model.get("bias"), -2.25)
        for name, raw in features.items():
            scale = max(1e-9, _safe_float(normalization.get(name), 1.0))
            normalized = max(0.0, min(1.0, raw / scale))
            value += _safe_float(weights.get(name), 0.0) * normalized
        return _sigmoid(value)

    def update(
        self,
        ax: float,
        ay: float,
        az: float,
        targets: list[dict],
        *,
        presence: bool = False,
        now: float | None = None,
    ) -> dict | None:
        t = time.time() if now is None else float(now)
        magnitude = math.sqrt(float(ax) ** 2 + float(ay) ** 2 + float(az) ** 2)
        imu_delta = abs(magnitude - 16384.0)
        target = self._pick_target(targets)
        if target is None:
            self._lost_frames += 1
            self._still_since = None
            self._had_active = False
            self._last_pos = None
            self._disp_speed = 0.0
            self.last_state = "ABSENT"
            self.last_score = 0.0
            return None

        x, y = float(target["x"]), float(target["y"])
        self._good_frames += 1
        inst_speed = 0.0
        if self._last_pos is not None:
            previous_x, previous_y, previous_t = self._last_pos
            dt = t - previous_t
            if 0.0 < dt < 2.0:
                inst_speed = min(300.0, math.hypot(x - previous_x, y - previous_y) / 10.0 / dt)
        self._disp_speed = 0.25 * inst_speed + 0.75 * self._disp_speed
        self._last_pos = (x, y, t)

        # A valid tracked target is a useful gate fallback when LD2410 GPIO
        # data lags by one or two frames; the feature still records the real
        # LD2410 value for model explainability.
        presence_feature = bool(presence)
        if self._disp_speed >= self.still_speed_threshold:
            self._had_active = self._had_active or self._disp_speed >= self.active_speed_threshold
            self._last_active_time = t
            self._speed_peak = max(self._speed_peak, self._disp_speed)
            self._still_since = None
            self._still_positions.clear()
            self._speed_at_edge = self._disp_speed
            self.last_state = "ACTIVE" if self._had_active else "MOVING"
            self.last_score = 0.0
            self.last_evidence = {"motion_speed_cm_s": round(self._disp_speed, 2)}
            return None

        if not self._had_active:
            self.last_state = "STILL"
            self.last_score = 0.0
            return None
        if self._still_since is None:
            self._still_since = t
            self._speed_at_edge = max(self._speed_at_edge, self._disp_speed)
        self._still_positions.append((x, y))
        elapsed = max(0.0, t - self._still_since)
        self.last_state = "STILL"

        if elapsed < self.stillness_duration:
            return None
        edge_gap = (self._still_since or t) - (self._last_active_time or t)
        if edge_gap < 0.0 or edge_gap > self.transition_window:
            self._clear_tracking()
            self.last_state = "SUPPRESSED"
            self.last_score = 0.0
            self.last_evidence = {"reason": "transition_window_expired"}
            return None

        features = self._features(still_duration=elapsed, presence=presence_feature, imu_delta=imu_delta)
        score = self._score(features)
        self.last_score = score
        self.last_evidence = {
            key: round(value, 4) for key, value in features.items()
        }
        threshold = _safe_float(self.model.get("threshold"), 0.60)
        if score < threshold:
            self._clear_tracking()
            self.last_state = "SUPPRESSED"
            return {
                "time": utc_now_iso(),
                "type": "SUSPECT",
                "detail": f"smart score below threshold ({score:.2f}<{threshold:.2f})",
                "confidence": round(score, 4),
                "model_version": self.model_version,
                "evidence": features,
            }

        event = self._emit_fall(
            detail=f"smart transition confirmed (score={score:.2f})",
            confidence=score,
            evidence=features,
        )
        self.reset()
        return event

    def emit_button(self, detail: str = "button GPIO17 help request", now: float | None = None) -> dict | None:
        return self._emit_fall(
            detail=detail,
            confidence=1.0,
            evidence={"source": "GPIO17", "explicit_help": True},
            now=now,
        )

    def _emit_fall(
        self,
        *,
        detail: str,
        confidence: float,
        evidence: dict[str, Any],
        now: float | None = None,
    ) -> dict | None:
        t = time.time() if now is None else float(now)
        if t - self._last_alarm_time < self.alarm_cooldown:
            return None
        self._seq += 1
        event_id = f"{self.device_id}-{int(t * 1000)}-{self._seq}"
        event_time = utc_now_iso()
        event = {
            "time": event_time,
            "occurred_at": event_time,
            "type": "FALL",
            "event_type": "FALL",
            "detail": str(detail),
            "event_id": event_id,
            "correlation_key": f"{self.room_id}:FALL:{int(t // 10)}",
            "device_id": self.device_id,
            "confidence": round(float(confidence), 4),
            "model_version": self.model_version,
            "evidence": evidence,
            "photo_ref": None,
        }
        parent = os.path.dirname(os.path.abspath(self.alarm_file))
        try:
            os.makedirs(parent, exist_ok=True)
            with open(self.alarm_file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError:
            pass
        self._last_alarm_time = t
        self.last_state = "CONFIRMED"
        self.last_score = float(confidence)
        return event


def _synthetic_target(x: float, y: float = 1500.0) -> dict:
    return {"dist": 1.5, "x": x, "y": y, "speed": 0.0}


def self_test() -> int:
    import tempfile

    path = os.path.join(tempfile.gettempdir(), "smart_fusion_test.jsonl")
    try:
        os.remove(path)
    except OSError:
        pass
    detector = SmartFallDetector(
        alarm_file=path,
        rules={
            "active_speed_threshold": 50,
            "still_speed_threshold": 50,
            "stillness_duration": 1.0,
            "transition_window": 2.0,
            "alarm_cooldown": 30,
        },
    )
    t0 = 1_700_000_000.0
    for i in range(15):
        detector.update(0, 0, 16384, [_synthetic_target(i * 140)], presence=True, now=t0 + i * 0.1)
    event = None
    for i in range(20):
        event = detector.update(0, 0, 16384, [_synthetic_target(1960)], presence=True, now=t0 + 1.5 + i * 0.1)
        if event and event.get("type") == "FALL":
            break
    ok = bool(event and event.get("type") == "FALL")
    print("smart_fusion self-test:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(self_test())
