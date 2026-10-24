"""Raspberry Pi edge runtime.

This module owns hardware sampling and orchestration. MQTT, detection and the
web layer are deliberately kept in separate modules so the Pi entrypoint stays
small and deployable.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone

try:
    import RPi.GPIO as GPIO
    import adafruit_dht
    import board
    import max30102
    import hrcalc
    import serial
    import smbus2 as smbus
    HARDWARE_AVAILABLE = True
except Exception as exc:  # desktop import and unit tests remain possible
    GPIO = None
    adafruit_dht = board = max30102 = hrcalc = serial = smbus = None
    HARDWARE_AVAILABLE = False
    HARDWARE_IMPORT_ERROR = str(exc)

from edge_bus import EdgeBus
from smart_fusion import SmartFallDetector


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATUS_FILE = os.path.join(BASE_DIR, "status.json")
ALARMS_FILE = os.path.join(BASE_DIR, "alarms.jsonl")
OUTBOX_FILE = os.path.join(BASE_DIR, "mqtt_outbox.jsonl")
MODEL_FILE = os.path.join(BASE_DIR, "model.json")
RULES_FILE = os.path.join(BASE_DIR, "rules.json")

ROOM_ID = os.environ.get("ROOM_ID", "living-room")
DEVICE_ID = os.environ.get("DEVICE_ID", "pi-01")
BOOT_ID = uuid.uuid4().hex[:12]
PHOTO_COOLDOWN = 8.0

BUTTON_PIN = 17
LD2410_OUT_PIN = 6
MPU6050_ADDR = 0x68
PWR_MGMT_1 = 0x6B
ACCEL_XOUT_H = 0x3B


class SharedState:
    def __init__(self):
        self.lock = threading.Lock()
        self.steps = 0
        self.bpm = 0
        self.spo2 = 0.0
        self.bpm_valid = False
        self.spo2_valid = False
        self.temperature = None
        self.humidity = None
        self.temp_recorded_at = None
        self.ax = 0
        self.ay = 0
        self.az = 16384
        self.accel_mag = 16384.0
        self.button_state = "UNKNOWN"
        self.button_alarm_flag = False
        self.take_photo_flag = False
        self.presence_state = False
        self.radar_targets = []
        self.detector_state = "WAITING"
        self.detector_confidence = 0.0
        self.detector_model = "smart-v1"
        self.last_event_id = None


state = SharedState()


def _require_hardware() -> None:
    if not HARDWARE_AVAILABLE:
        raise RuntimeError(f"Pi hardware dependencies unavailable: {HARDWARE_IMPORT_ERROR}")


def _read_raw_data(bus, addr: int) -> int:
    high = bus.read_byte_data(MPU6050_ADDR, addr)
    low = bus.read_byte_data(MPU6050_ADDR, addr + 1)
    value = (high << 8) | low
    return value - 65536 if value > 32768 else value


def _sensor_loop() -> None:
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(BUTTON_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    try:
        bus = smbus.SMBus(1)
        bus.write_byte_data(MPU6050_ADDR, PWR_MGMT_1, 0)
    except Exception:
        bus = None
    try:
        dht_device = adafruit_dht.DHT11(board.D5)
    except Exception:
        dht_device = None

    local_steps = 0
    last_step = 0.0
    last_button = GPIO.LOW
    last_dht = 0.0
    while True:
        now = time.time()
        raw_button = GPIO.input(BUTTON_PIN)
        if raw_button == GPIO.HIGH and last_button == GPIO.LOW:
            with state.lock:
                state.take_photo_flag = True
                state.button_alarm_flag = True
        last_button = raw_button
        with state.lock:
            state.button_state = "PRESSED" if raw_button == GPIO.HIGH else "RELEASED"

        if bus is not None:
            try:
                ax = _read_raw_data(bus, ACCEL_XOUT_H)
                ay = _read_raw_data(bus, ACCEL_XOUT_H + 2)
                az = _read_raw_data(bus, ACCEL_XOUT_H + 4)
                magnitude = math.sqrt(ax * ax + ay * ay + az * az)
                with state.lock:
                    state.ax, state.ay, state.az = ax, ay, az
                    state.accel_mag = magnitude
                if magnitude > 25000 and now - last_step >= 0.3:
                    local_steps += 1
                    last_step = now
            except OSError:
                pass

        if dht_device is not None and now - last_dht >= 2.0:
            try:
                temperature = dht_device.temperature
                humidity = dht_device.humidity
                if temperature is not None and humidity is not None:
                    with state.lock:
                        state.temperature = temperature
                        state.humidity = humidity
                        state.temp_recorded_at = datetime.now(timezone.utc).isoformat()
            except Exception:
                pass
            last_dht = now
        with state.lock:
            state.steps = local_steps
        time.sleep(0.02)


def _ld2410_worker() -> None:
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(LD2410_OUT_PIN, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
    try:
        ser = serial.Serial("/dev/ttyAMA4", 256000, timeout=0.1)
    except Exception:
        ser = None
    buffer = b""
    serial_state = 0
    while True:
        try:
            out_high = GPIO.input(LD2410_OUT_PIN) == GPIO.HIGH
            if ser is not None and ser.in_waiting:
                buffer += ser.read(ser.in_waiting)
                while len(buffer) >= 23:
                    head = buffer.find(b"\xF4\xF3\xF2\xF1")
                    if head < 0:
                        buffer = buffer[-4:]
                        break
                    buffer = buffer[head:]
                    if len(buffer) < 23:
                        break
                    if buffer[19] == 0xF8 and buffer[20] == 0xF7:
                        serial_state = buffer[8]
                        buffer = buffer[23:]
                    else:
                        buffer = buffer[1:]
            with state.lock:
                state.presence_state = bool(out_high or serial_state != 0)
        except Exception:
            pass
        time.sleep(0.05)


def _decode_ld2450_value(low: int, high: int) -> float:
    value = low + high * 256
    return float(value - 32768) if high >= 128 else float(-value)


def _ld2450_worker() -> None:
    try:
        ser = serial.Serial("/dev/ttyAMA2", 256000, timeout=0.1)
    except Exception:
        ser = None
    buffer = b""
    while True:
        try:
            if ser is not None and ser.in_waiting:
                buffer += ser.read(ser.in_waiting)
                while len(buffer) >= 30:
                    head = buffer.find(b"\xAA\xFF\x03\x00")
                    if head < 0:
                        buffer = buffer[-3:]
                        break
                    buffer = buffer[head:]
                    if len(buffer) < 30:
                        break
                    if buffer[28] != 0x55 or buffer[29] != 0xCC:
                        buffer = buffer[1:]
                        continue
                    frame = buffer[:30]
                    buffer = buffer[30:]
                    targets = []
                    offset = 4
                    for _ in range(3):
                        x = _decode_ld2450_value(frame[offset], frame[offset + 1])
                        y = _decode_ld2450_value(frame[offset + 2], frame[offset + 3])
                        speed = _decode_ld2450_value(frame[offset + 4], frame[offset + 5])
                        dist = math.sqrt(x * x + y * y) / 1000.0
                        targets.append({"dist": dist, "x": x, "y": y, "speed": speed})
                        offset += 8
                    with state.lock:
                        state.radar_targets = targets
        except Exception:
            pass
        time.sleep(0.03)


def _max30102_worker() -> None:
    try:
        sensor = max30102.MAX30102(channel=4, gpio_pin=4)
    except Exception:
        return
    while True:
        try:
            red, ir = sensor.read_sequential()
            if red and ir:
                hr, hr_valid, spo2, spo2_valid = hrcalc.calc_hr_and_spo2(ir, red)
                valid_hr = bool(hr_valid and 30 <= float(hr) <= 200)
                valid_spo2 = bool(spo2_valid and 70 <= float(spo2) <= 100)
                with state.lock:
                    state.bpm_valid = valid_hr
                    state.spo2_valid = valid_spo2
                    if valid_hr:
                        state.bpm = int(hr)
                    if valid_spo2:
                        state.spo2 = float(spo2)
        except OSError:
            try:
                sensor.reset()
            except Exception:
                pass
            time.sleep(0.5)
        except Exception:
            time.sleep(0.2)
        time.sleep(0.02)


def _take_photo() -> str | None:
    filename = os.path.join(BASE_DIR, f"{datetime.now().strftime('%Y%m%d%H%M%S')}_food.jpg")
    command = ["rpicam-jpeg", "-o", filename, "-t", "500", "--width", "1296", "--height", "972"]
    try:
        subprocess.run(command, check=True, timeout=8)
        return os.path.basename(filename)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None


def run() -> None:
    _require_hardware()
    for target in (_sensor_loop, _ld2410_worker, _ld2450_worker, _max30102_worker):
        threading.Thread(target=target, daemon=True, name=target.__name__).start()

    try:
        with open(RULES_FILE, "r", encoding="utf-8") as fh:
            rules = json.load(fh)
    except (OSError, ValueError):
        rules = {}
    detector = SmartFallDetector(
        alarm_file=ALARMS_FILE,
        rules=rules,
        model_path=MODEL_FILE,
        device_id=DEVICE_ID,
        room_id=ROOM_ID,
    )

    bus = None

    def handle_command(command: dict) -> None:
        action = str(command.get("action", "")).upper()
        if action == "REQUEST_PHOTO":
            with state.lock:
                state.take_photo_flag = True
        elif action == "ACK_ALARM":
            with state.lock:
                state.detector_state = "ACKNOWLEDGED"
        elif action == "PING" and bus is not None:
            bus.publish_json("ack", {
                "schema_version": 1,
                "device_id": DEVICE_ID,
                "action": action,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })

    bus = EdgeBus(
        room_id=ROOM_ID,
        device_id=DEVICE_ID,
        outbox_path=OUTBOX_FILE,
        host=os.environ.get("MQTT_HOST", "127.0.0.1"),
        port=int(os.environ.get("MQTT_PORT", "1883")),
        tls=os.environ.get("MQTT_TLS", "0") == "1",
        username=os.environ.get("MQTT_USER") or None,
        password=os.environ.get("MQTT_PASSWORD") or None,
        on_command=handle_command,
    )
    bus.start()

    def write_status(payload: dict) -> None:
        tmp = STATUS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, STATUS_FILE)

    last_status = 0.0
    last_state = 0.0
    last_heartbeat = 0.0
    last_photo = 0.0
    seq = 0
    started = time.time()
    payload = {"waiting": True, "device_id": DEVICE_ID, "mqtt": bus.connected}
    try:
        while True:
            now = time.time()
            with state.lock:
                button_alarm = state.button_alarm_flag
                state.button_alarm_flag = False
                ax, ay, az = state.ax, state.ay, state.az
                snapshot = {
                    "steps": state.steps,
                    "bpm": state.bpm if state.bpm_valid else 0,
                    "spo2": state.spo2 if state.spo2_valid else 0.0,
                    "bpm_valid": state.bpm_valid,
                    "spo2_valid": state.spo2_valid,
                    "temperature": state.temperature,
                    "humidity": state.humidity,
                    "temp_recorded_at": state.temp_recorded_at,
                    "presence_state": state.presence_state,
                    "radar_targets": list(state.radar_targets),
                    "button": state.button_state,
                }

            event = detector.emit_button() if button_alarm else None
            if event:
                with state.lock:
                    state.last_event_id = event["event_id"]
                    state.take_photo_flag = True
                bus.publish_json("event", event)

            detector_event = detector.update(
                ax, ay, az, snapshot["radar_targets"],
                presence=snapshot["presence_state"],
                now=now,
            )
            if detector_event and detector_event.get("type") == "FALL":
                with state.lock:
                    state.last_event_id = detector_event["event_id"]
                    state.take_photo_flag = True
                bus.publish_json("event", detector_event)

            if now - last_photo >= PHOTO_COOLDOWN:
                with state.lock:
                    photo_requested = state.take_photo_flag
                    state.take_photo_flag = False
                if photo_requested:
                    photo_name = _take_photo()
                    if photo_name:
                        bus.publish_json("ack", {
                            "schema_version": 1,
                            "device_id": DEVICE_ID,
                            "action": "PHOTO_CAPTURED",
                            "photo_ref": photo_name,
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        })
                        last_photo = now

            detector_status = detector.snapshot()
            with state.lock:
                state.detector_state = detector_status["state"]
                state.detector_confidence = detector_status["confidence"]
                state.detector_model = detector_status["model_version"]

            if now - last_status >= 0.5:
                seq += 1
                payload = dict(
                    snapshot,
                    schema_version=1,
                    room_id=ROOM_ID,
                    device_id=DEVICE_ID,
                    boot_id=BOOT_ID,
                    seq=seq,
                    waiting=False,
                    mqtt=bus.connected,
                    outbox_pending=len(bus.outbox),
                    detector=detector_status,
                    health={
                        "uptime_s": round(now - started, 1),
                        "mqtt_connected": bus.connected,
                        "outbox_pending": len(bus.outbox),
                    },
                    updated_at=datetime.now(timezone.utc).isoformat(),
                )
                write_status(payload)
                last_status = now
            if now - last_state >= 10.0:
                bus.publish_json("state", payload)
                last_state = now
            if now - last_heartbeat >= 10.0:
                bus.publish_json("heartbeat", {
                    "schema_version": 1,
                    "device_id": DEVICE_ID,
                    "boot_id": BOOT_ID,
                    "status": "online",
                    "seq": seq,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }, retain=True)
                last_heartbeat = now
            time.sleep(0.1)
    finally:
        bus.stop()
        GPIO.cleanup()


if __name__ == "__main__":
    run()
