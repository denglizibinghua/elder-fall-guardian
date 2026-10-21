"""树莓派老人看护主程序（运行在 /home/admin/food_project，由 food_project.service 托管）。

职责：
  1. 启动 4 个传感器驻留线程（MPU6050+按键 / LD2410 存在 / LD2450 轨迹 / MAX30102 心率血氧）
  2. 每 0.5s 原子写 status.json，供同目录 app.py（:8080）读取
  3. 每 10s 往本地 mosquitto 推一份 status 快照（边缘总线）
  4. 跌倒判定交给 fall_fusion.FallDetector；报警先落 alarms.jsonl 再异步拍照

已移除（2026-10-20 清理）：学长旧后端那套 HTTP 上报全部删除。原来 5 个 URL 常量指向
黑洞端口 127.0.0.1:9，每 5 秒发一次注定 connection-refused 的请求，纯属死代码。
拍照现在是纯本地存盘，不再尝试上传。
"""

import os
import json
import math
import time
import threading
import subprocess
from datetime import datetime, timezone

import RPi.GPIO as GPIO
import smbus2 as smbus
import board
import adafruit_dht
import serial

import max30102
import hrcalc

# --- 跌倒融合算法（导入失败时主循环仍可跑，只是不报警）---
try:
    import fall_fusion
    _FUSION_OK = True
except Exception as _e:
    print(f"fusion import skipped: {_e}")
    _FUSION_OK = False

# --- MQTT 边缘总线（只连本机 broker，绝不阻塞主循环）---
# 预留对外集成接口：本机 mosquitto 只监听 127.0.0.1:1883，当前无订阅者，
# 消息发进黑洞；将来接 Home Assistant / 手机 App 时开 listener 到 0.0.0.0 即可。
try:
    import paho.mqtt.client as _mqttlib
    # paho >= 2.0 要求显式声明回调 API 版本，否则每次建 Client 都打
    # "DeprecationWarning: Callback API version 1 is deprecated"。
    # 本文件不注册任何回调（只用 connect_async/publish），故改 VERSION2 零影响；
    # 但将来若加 on_connect，签名得用新版的 (client, userdata, flags, reason_code, properties)。
    # paho 1.x 没有 CallbackAPIVersion，取不到就退回无参构造。
    _MQTT_CB = getattr(_mqttlib, "CallbackAPIVersion", None)
    _MQTT_OK = True
except Exception as _e:
    print(f"mqtt import skipped: {_e}")
    _MQTT_OK = False
    _MQTT_CB = None

_MQTT_TOPICS = {"alarm": "elder/fall/alarm", "button": "elder/button", "status": "elder/status"}

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_STATUS_JSON = os.path.join(_BASE_DIR, "status.json")
_ALARM_JSONL = os.path.join(_BASE_DIR, "alarms.jsonl")

# --- 硬件引脚配置 ---
BUTTON_PIN = 17          # 求助按键（PUD_UP：空闲 LOW / 按下 HIGH）
LD2410_OUT_PIN = 6       # 存在感应雷达的 OUT 引脚
MPU6050_ADDR = 0x68
PWR_MGMT_1 = 0x6B
ACCEL_XOUT_H = 0x3B

# --- 采样与上传节奏 ---
STEP_THRESHOLD = 25000       # 加速度模长阈值（MPU6050 ±2g，静止 1g ≈ 16384 LSB）
STEP_DEBOUNCE_S = 0.3        # 两步之间最小间隔
DHT_INTERVAL_S = 2.0         # DHT11 采样率上限
STATUS_INTERVAL_S = 0.5      # status.json 写入周期（曲线/前端轮询分辨率）
MQTT_INTERVAL_S = 10.0       # MQTT status 快照周期
PHOTO_TIMEOUT_MS = "500"     # rpicam-jpeg 曝光时长

# --- 共享状态（线程安全）---
class SharedState:
    def __init__(self):
        self.steps = 0
        self.bpm = 0
        self.spo2 = 0
        self.take_photo_flag = False
        self.temperature = None
        self.humidity = None
        self.temp_recorded_at = None
        self.ax = 0
        self.ay = 0
        self.az = 0
        self.button_state = "UNKNOWN"
        self.button_alarm_flag = False
        self.presence_state = False
        self.radar_targets = []
        self.lock = threading.Lock()


state = SharedState()


# --- MPU6050 辅助函数 ---
def mpu6050_init(bus):
    try:
        bus.write_byte_data(MPU6050_ADDR, PWR_MGMT_1, 0)
    except OSError:
        pass


def read_raw_data(bus, addr):
    try:
        high = bus.read_byte_data(MPU6050_ADDR, addr)
        low = bus.read_byte_data(MPU6050_ADDR, addr + 1)
        value = (high << 8) | low
        return value - 65536 if value > 32768 else value
    except OSError:
        return 0


# --- LD2410 存在感应驻留线程 ---
def ld2410_worker():
    print("LD2410 Presence thread started...")
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(LD2410_OUT_PIN, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)

    try:
        ser = serial.Serial('/dev/ttyAMA4', 256000, timeout=0.1)
    except Exception as e:
        print(f"LD2410 UART4 Init Error: {e}")
        ser = None

    buffer = b""
    data_state = 0
    while True:
        try:
            # 优先用 GPIO6 硬件电平快速判断，串口解析仅作兜底
            out_high = (GPIO.input(LD2410_OUT_PIN) == GPIO.HIGH)

            if ser and ser.in_waiting:
                buffer += ser.read(ser.in_waiting)
                while len(buffer) >= 23:
                    head = buffer.find(b'\xF4\xF3\xF2\xF1')
                    if head == -1:
                        buffer = buffer[-4:]
                        break
                    buffer = buffer[head:]
                    if len(buffer) >= 23:
                        if buffer[19] == 0xF8 and buffer[20] == 0xF7:
                            data_state = buffer[8]
                            buffer = buffer[23:]
                        else:
                            buffer = buffer[1:]
                    else:
                        break

            with state.lock:
                state.presence_state = out_high or (data_state != 0)
        except Exception:
            pass
        time.sleep(0.05)


# --- LD2450 轨迹雷达驻留线程 ---
def ld2450_worker():
    print("LD2450 Radar thread started...")
    try:
        ser = serial.Serial('/dev/ttyAMA2', 256000, timeout=0.1)
    except Exception as e:
        print(f"LD2450 UART2 Init Error: {e}")
        ser = None

    def calc_val(lb, hb):
        v = lb + (hb * 256)
        return float(v - 32768) if hb >= 128 else float(0 - v)

    buffer = b""
    while True:
        try:
            if ser and ser.in_waiting:
                buffer += ser.read(ser.in_waiting)
                while len(buffer) >= 30:
                    head = buffer.find(b'\xAA\xFF\x03\x00')
                    if head == -1:
                        buffer = buffer[-3:]
                        break
                    buffer = buffer[head:]
                    if len(buffer) >= 30:
                        if buffer[28] == 0x55 and buffer[29] == 0xCC:
                            frame = buffer[:30]
                            buffer = buffer[30:]

                            targets = []
                            offset = 4
                            for _ in range(3):
                                x = calc_val(frame[offset], frame[offset + 1])
                                y = calc_val(frame[offset + 2], frame[offset + 3])
                                speed = calc_val(frame[offset + 4], frame[offset + 5])
                                dist = math.sqrt(x * x + y * y) / 1000.0
                                targets.append({'dist': dist, 'x': x, 'y': y, 'speed': speed})
                                offset += 8

                            with state.lock:
                                state.radar_targets = targets
                        else:
                            buffer = buffer[1:]
                    else:
                        break
        except Exception:
            pass
        time.sleep(0.05)


# --- MAX30102 心率血氧驻留线程 ---
def max30102_worker():
    print("MAX30102 Heart Rate/SpO2 thread started...")

    GPIO.setmode(GPIO.BCM)

    try:
        # MAX30102 挂在软件 I2C 总线 4（GPIO23/24），中断脚 BCM GPIO4
        m = max30102.MAX30102(channel=4, gpio_pin=4)
    except Exception as e:
        print(f"I2C Init Error (MAX30102): {e}")
        return

    while True:
        try:
            red, ir = m.read_sequential()
            if red and ir:
                hr, hr_valid, spo2, spo2_valid = hrcalc.calc_hr_and_spo2(ir, red)
                if hr_valid and spo2_valid:
                    with state.lock:
                        state.bpm = hr
                        state.spo2 = spo2
        except TimeoutError as e:
            # 手指未放好 / 传感器脱落：等待期间不再空转烧 CPU
            print(f"MAX30102 timeout: {e}")
            time.sleep(1.0)
        except OSError:
            time.sleep(0.5)
            try:
                m.reset()
            except Exception:
                pass
        except Exception:
            time.sleep(0.5)


# --- 按键 + MPU6050 + DHT11 驻留线程 ---
def sensor_loop():
    print("Sensor thread started...")

    GPIO.setmode(GPIO.BCM)
    GPIO.setup(BUTTON_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    try:
        bus = smbus.SMBus(1)
        mpu6050_init(bus)
    except Exception as e:
        print(f"I2C Init Error (MPU6050): {e}")
        bus = None

    local_step_count = 0
    last_step_time = 0
    last_btn_state = GPIO.LOW

    try:
        dht_device = adafruit_dht.DHT11(board.D5)
    except Exception as e:
        print(f"DHT Init Error: {e}")
        dht_device = None

    last_dht_read_time = 0

    while True:
        current_time = time.time()

        # 1. 按键：上升沿触发求助报警 + 拍照
        btn_raw = GPIO.input(BUTTON_PIN)
        if btn_raw == GPIO.HIGH and last_btn_state == GPIO.LOW:
            print("Button Pressed - Requesting Photo")
            with state.lock:
                state.take_photo_flag = True
                state.button_alarm_flag = True
            time.sleep(0.5)
        last_btn_state = btn_raw
        with state.lock:
            state.button_state = "RELEASED" if btn_raw == GPIO.LOW else "PRESSED"

        # 2. MPU6050 计步
        if bus:
            acc_x = read_raw_data(bus, ACCEL_XOUT_H)
            acc_y = read_raw_data(bus, ACCEL_XOUT_H + 2)
            acc_z = read_raw_data(bus, ACCEL_XOUT_H + 4)

            curr_mag = math.sqrt(acc_x ** 2 + acc_y ** 2 + acc_z ** 2)
            with state.lock:
                state.ax = acc_x
                state.ay = acc_y
                state.az = acc_z
            if curr_mag > STEP_THRESHOLD and (current_time - last_step_time) > STEP_DEBOUNCE_S:
                local_step_count += 1
                last_step_time = current_time

        # 3. DHT11 温湿度（每 ~2s）
        if dht_device and (current_time - last_dht_read_time) >= DHT_INTERVAL_S:
            try:
                temp = dht_device.temperature
                hum = dht_device.humidity
                if temp is not None and hum is not None:
                    with state.lock:
                        state.temperature = temp
                        state.humidity = hum
                        state.temp_recorded_at = datetime.now(timezone.utc).isoformat()
                last_dht_read_time = current_time
            except RuntimeError as error:
                print(f"DHT Temporary Error: {error}")
                last_dht_read_time = current_time
            except Exception as e:
                print(f"DHT Error: {e}")
                dht_device = None

        with state.lock:
            state.steps = local_step_count

        time.sleep(0.02)


# --- 拍照（纯本地存盘，不再上传任何后端）---
def take_photo(filename):
    print(f"Taking photo: {filename}")
    cmd = ["rpicam-jpeg", "-o", filename, "-t", PHOTO_TIMEOUT_MS,
           "--width", "1920", "--height", "1080"]
    try:
        subprocess.run(cmd, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("Camera Error (or not found)")
        return False


def capture_photo_task():
    """后台线程拍照。存到项目目录，失败只打印不抛。"""
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    filename = os.path.join(_BASE_DIR, f"{timestamp}_food.jpg")
    take_photo(filename)


if __name__ == "__main__":
    threading.Thread(target=sensor_loop, daemon=True).start()
    threading.Thread(target=max30102_worker, daemon=True).start()
    threading.Thread(target=ld2410_worker, daemon=True).start()
    threading.Thread(target=ld2450_worker, daemon=True).start()

    print("System Running. Press Button to capture. Fall detection via fusion.")

    _rules = fall_fusion.load_rules() if _FUSION_OK else {}
    _detector = fall_fusion.FallDetector(alarm_file=_ALARM_JSONL, rules=_rules) if _FUSION_OK else None
    if _detector is None:
        print("fusion disabled")
    else:
        print("fusion enabled ->", _ALARM_JSONL)

    _last_status_time = 0.0
    _last_mqtt_time = 0.0
    _mqttc = None
    if _MQTT_OK:
        try:
            _mqttc = _mqttlib.Client(_MQTT_CB.VERSION2) if _MQTT_CB else _mqttlib.Client()
            _mqttc.connect_async("127.0.0.1", 1883, 60)
            _mqttc.loop_start()
            print("mqtt enabled -> 127.0.0.1:1883")
        except Exception as _e:
            print(f"mqtt connect skipped: {_e}")
            _mqttc = None

    try:
        while True:
            current_time = time.time()

            # --- 1. 求助按键：立即报警（不进 cooldown，报警优先）---
            _btn = False
            with state.lock:
                if state.button_alarm_flag:
                    state.button_alarm_flag = False
                    _btn = True

            _bev = None
            if _btn:
                _detail = "button GPIO17 \u6c42\u52a9\u6309\u952e"
                try:
                    if _detector is not None:
                        _bev = _detector._emit_fall(_detail)
                    if _bev is None:
                        _bev = {"time": datetime.now(timezone.utc).isoformat(),
                                "type": "FALL", "detail": _detail}
                        with open(_ALARM_JSONL, "a", encoding="utf-8") as _fh:
                            _fh.write(json.dumps(_bev, ensure_ascii=False) + "\n")
                except Exception as _e:
                    print(f"button alarm warn: {_e}")
                    _bev = None
            if _bev is not None:
                print(f"!!! BUTTON ALARM !!! {_bev.get('detail','')}")
                if _mqttc is not None:
                    try:
                        _mqttc.publish(_MQTT_TOPICS["button"], json.dumps(_bev, ensure_ascii=False))
                    except Exception:
                        pass

            # --- 2. 拍照请求（按键或跌倒后由 fall_fusion 置位）---
            with state.lock:
                pending_photo = state.take_photo_flag
                if pending_photo:
                    state.take_photo_flag = False
            if pending_photo:
                threading.Thread(target=capture_photo_task, daemon=True).start()

            # --- 3. 跌倒融合判定 + status.json + MQTT 快照 ---
            try:
                with state.lock:
                    _ax, _ay, _az = state.ax, state.ay, state.az
                    _bw = state.button_state
                    _st = dict(steps=state.steps, bpm=int(state.bpm), spo2=float(state.spo2),
                               temperature=state.temperature, humidity=state.humidity,
                               temp_recorded_at=state.temp_recorded_at,
                               presence_state=state.presence_state,
                               radar_targets=list(state.radar_targets))

                if _detector is not None:
                    _ev = _detector.update(_ax, _ay, _az, _st["radar_targets"])
                    if _ev and _ev.get("type") == "FALL":
                        print(f"!!! FUSION FALL !!! {_ev.get('detail','')}")
                        if _mqttc is not None:
                            try:
                                _mqttc.publish(_MQTT_TOPICS["alarm"], json.dumps(_ev, ensure_ascii=False))
                            except Exception:
                                pass
                        with state.lock:
                            state.take_photo_flag = True

                _payload = dict(_st, waiting=False, button=_bw, mqtt=(_mqttc is not None),
                                take_photo_flag=False,
                                updated_at=datetime.now(timezone.utc).isoformat())

                if current_time - _last_status_time >= STATUS_INTERVAL_S:
                    _tmp = _STATUS_JSON + ".tmp"
                    with open(_tmp, "w", encoding="utf-8") as _fh:
                        json.dump(_payload, _fh, ensure_ascii=False)
                    os.replace(_tmp, _STATUS_JSON)
                    _last_status_time = current_time

                if _mqttc is not None and (current_time - _last_mqtt_time >= MQTT_INTERVAL_S):
                    try:
                        _mqttc.publish(_MQTT_TOPICS["status"], json.dumps(_payload, ensure_ascii=False))
                    except Exception:
                        pass
                    _last_mqtt_time = current_time
            except Exception as _e:
                print(f"fusion/status warn: {_e}")

            time.sleep(0.1)

    except KeyboardInterrupt:
        print("Stopping...")
        GPIO.cleanup()
