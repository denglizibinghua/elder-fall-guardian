import RPi.GPIO as GPIO
import smbus2 as smbus
import time
import math
import subprocess
import requests
import os
import json
import threading
import board
import adafruit_dht
import serial  
from datetime import datetime, timezone
import max30102
import hrcalc

# --- Configuration ---
# Legacy remote upload endpoint (dead by default, kept for reference).
# Local fall detection + status.json/alarms.jsonl work offline and do not need it.
# Override with env BACKEND_BASE_URL if you have your own server.
BACKEND_BASE_URL = os.environ.get("BACKEND_BASE_URL", "http://192.0.2.1:8000")
UPLOAD_URL = f"{BACKEND_BASE_URL}/api/upload"
STEPS_API_URL = f"{BACKEND_BASE_URL}/api/health/steps"
HEART_RATE_API_URL = f"{BACKEND_BASE_URL}/api/health/heart-rate"
TEMP_HUMIDITY_API_URL = f"{BACKEND_BASE_URL}/api/health/temperature_humidity"

# --- 新增配置 ---
PRESENCE_API_URL = f"{BACKEND_BASE_URL}/api/presence"
LOCATION_API_URL = f"{BACKEND_BASE_URL}/api/location"
FALL_API_URL = f"{BACKEND_BASE_URL}/api/fall_detection"
FALL_SPEED_THRESHOLD = 100.0  

USER_ID = 1  
DEVICE_ID = 'device_0001'
TEMP_HUM_UPLOAD_INTERVAL = 60  

# --- GPIO & Hardware Config (不修改原有引脚) ---
BUTTON_PIN = 17
MPU6050_ADDR = 0x68
PWR_MGMT_1 = 0x6B
ACCEL_XOUT_H = 0x3B

LD2410_OUT_PIN = 6  # 新增

# --- Shared State (Thread Safe) ---
class SharedState:
    def __init__(self):
        self.steps = 0
        self.bpm = 0
        self.spo2 = 0 # 新增：血氧数据
        self.take_photo_flag = False
        self.temperature = None
        self.humidity = None
        self.temp_recorded_at = None
        
        # 新增
        self.presence_state = False  
        self.radar_targets = []      
        
        self.lock = threading.Lock()

state = SharedState()

# --- Sensor Helper Functions ---
def mpu6050_init(bus):
    try:
        bus.write_byte_data(MPU6050_ADDR, PWR_MGMT_1, 0)
    except OSError:
        pass

def read_raw_data(bus, addr):
    try:
        high = bus.read_byte_data(MPU6050_ADDR, addr)
        low = bus.read_byte_data(MPU6050_ADDR, addr+1)
        value = ((high << 8) | low)
        if value > 32768:
            value = value - 65536
        return value
    except OSError:
        return 0

# --- 新增：LD2410 (存在数据) 驻留线程 ---
def ld2410_worker():
    print("LD2410 Presence thread started...")
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(LD2410_OUT_PIN, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
    
    try:
        ser = serial.Serial('/dev/ttyAMA4', 256000, timeout=0.1)  # 刚才配置接线的 UART4
    except Exception as e:
        print(f"LD2410 UART4 Init Error: {e}")
        ser = None
        
    buffer = b""
    data_state = 0
    while True:
        try:
            # 1. 优先使用 GPIO 6 硬件引脚的高低电平快速判断雷达存在状态
            out_high = (GPIO.input(LD2410_OUT_PIN) == GPIO.HIGH)
            
            # 2. 串口解析后备状态兜底
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
        except Exception as e:
            pass
        time.sleep(0.05)

# --- 新增：LD2450 (轨迹与跌倒数据) 驻留线程 ---
def ld2450_worker():
    print("LD2450 Radar thread started...")
    try:
        ser = serial.Serial('/dev/ttyAMA2', 256000, timeout=0.1)  # 刚才配置的接线 UART2
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
                                x = calc_val(frame[offset], frame[offset+1])
                                y = calc_val(frame[offset+2], frame[offset+3])
                                speed = calc_val(frame[offset+4], frame[offset+5])
                                dist = math.sqrt(x*x + y*y) / 1000.0
                                targets.append({'dist': dist, 'x': x, 'y': y, 'speed': speed})
                                offset += 8
                                
                            with state.lock:
                                state.radar_targets = targets
                        else:
                            buffer = buffer[1:]
                    else:
                        break
        except Exception as e:
            pass
        time.sleep(0.05)


# --- 新增：MAX30102 (心率血氧) 驻留线程 ---
def max30102_worker():
    print("MAX30102 Heart Rate/SpO2 thread started...")
    
    # 强制让所有线程统一使用 BCM 编号，防止冲突
    GPIO.setmode(GPIO.BCM)
    
    try:
        # MAX30102 连接在新增的 I2C 总线 4 (GPIO 23 & 24)
        # 注意: 这里的 gpio_pin=4 指的是 BCM GPIO 4 (如果是物理引脚则是7)
        m = max30102.MAX30102(channel=4, gpio_pin=4)
    except Exception as e:
        print(f"I2C Init Error (MAX30102): {e}")
        return

    while True:
        try:
            # 获取数据，这会阻塞直到收集满所需样本
            red, ir = m.read_sequential()
            if red and ir:
                hr, hr_valid, spo2, spo2_valid = hrcalc.calc_hr_and_spo2(ir, red)
                if hr_valid and spo2_valid:
                    with state.lock:
                        state.bpm = hr
                        state.spo2 = spo2
        except OSError as e:
            time.sleep(0.5)
            try: m.reset()
            except: pass
        except Exception as e:
            time.sleep(0.5)
# --- Sensor Thread Function ---
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
    last_mag = 0
    step_threshold = 25000
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
        
        # 1. READ BUTTON 
        btn_raw = GPIO.input(BUTTON_PIN)
        if btn_raw == GPIO.HIGH and last_btn_state == GPIO.LOW:
            print("Button Pressed - Requesting Photo")
            with state.lock:
                state.take_photo_flag = True
            time.sleep(0.5) 
        last_btn_state = btn_raw

        # 2. READ STEPS
        if bus:
            acc_x = read_raw_data(bus, ACCEL_XOUT_H)
            acc_y = read_raw_data(bus, ACCEL_XOUT_H + 2)
            acc_z = read_raw_data(bus, ACCEL_XOUT_H + 4)
            
            curr_mag = math.sqrt(acc_x**2 + acc_y**2 + acc_z**2)
            if curr_mag > step_threshold and (current_time - last_step_time) > 0.3:
                local_step_count += 1
                last_step_time = current_time

        # 4. READ DHT11 (every ~2s)
        if dht_device and (current_time - last_dht_read_time) >= 2.0:
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
            
        time.sleep(0.01)

# --- Main Logic Functions ---
def take_photo(filename):
    print(f"Taking photo: {filename}")
    cmd = ["rpicam-jpeg", "-o", filename, "-t", "500", "--width", "1920", "--height", "1080"]
    try:
        subprocess.run(cmd, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("Camera Error (or not found)")
        return False

def process_photo_task():
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    filename = f"{timestamp}_food.jpg"
    
    if take_photo(filename):
        print("Uploading photo...")
        if not os.path.exists(filename): return
        
        try:
            with open(filename, 'rb') as f:
                files = {'file': f}
                data = {'device_id': DEVICE_ID, 'user_id': USER_ID}
                res = requests.post(UPLOAD_URL, files=files, data=data)
                if res.status_code == 200:
                    print(f"Analysis: {res.text}")
                else:
                    print(f"Upload Failed: {res.status_code}")
        except Exception as e:
            print(f"Upload Error: {e}")

def process_sensor_upload():
    bpm_val = 0
    spo2_val = 0
    steps_val = 0
    with state.lock:
        bpm_val = int(state.bpm)
        spo2_val = float(state.spo2)
        steps_val = state.steps
    try:
        payload = {'count': steps_val, 'user_id': USER_ID, 'device_id': DEVICE_ID}
        requests.post(STEPS_API_URL, json=payload, timeout=2)
    except Exception as e:
        print(f"Step Upload Warning: {e}")
    try:
        # 修改上传负载，对接最新的心率和血氧接口格式
        payload = {
            'bpm': bpm_val, 
            'blood_oxygen': round(spo2_val, 1), 
            'device_id': DEVICE_ID
        }
        res = requests.post(HEART_RATE_API_URL, json=payload, timeout=2)
        if res.status_code == 200 or res.status_code == 201:
            print(f"[Upload OK] 心率: {bpm_val}, 血氧: {payload['blood_oxygen']}")
        else:
            print(f"[Upload Failed] 后端返回错误: {res.text}")
    except Exception as e:
        print(f"HR/SPO2 Upload Warning: {e}")

def process_temp_humidity_upload():
    with state.lock:
        temp = state.temperature
        hum = state.humidity
        rec = state.temp_recorded_at

    if temp is None or hum is None: return
    if not rec:
        rec = datetime.now(timezone.utc).isoformat()

    payload = {
        'device_id': DEVICE_ID, 'temperature': f"{temp:.1f}", 'humidity': str(hum), 'recorded_at': rec
    }
    try:
        requests.post(TEMP_HUMIDITY_API_URL, json=payload, timeout=3)
    except Exception as e:
        pass

if __name__ == "__main__":
    t = threading.Thread(target=sensor_loop, daemon=True)
    t.start()
    
    # 启动 MAX30102 线程
    t_max = threading.Thread(target=max30102_worker, daemon=True)
    t_max.start()
    
    # 启动新增雷达线程
    t_2410 = threading.Thread(target=ld2410_worker, daemon=True)
    t_2410.start()
    t_2450 = threading.Thread(target=ld2450_worker, daemon=True)
    t_2450.start()
    
    print("System Running. Press Button to capture. Auto-upload sensors every 5s.")
    
    last_upload_time = time.time()
    last_temp_upload_time = time.time()
    
    # --- 新增的计时器与持久状态 ---
    last_presence_time = 0
    last_location_time = 0
    last_fall_time = 0
    last_presence_state = False
    
    try:
        while True:
            current_time = time.time()
            
            # --- 1. 原有照片逻辑 ---
            pending_photo = False
            with state.lock:
                if state.take_photo_flag:
                    pending_photo = True
                    state.take_photo_flag = False
            if pending_photo:
                process_photo_task()
            
            # --- 2. 原有 5 秒心率步数 ---
            if current_time - last_upload_time >= 5.0:
                process_sensor_upload()
                last_upload_time = current_time

            # --- 3. 原有 60 秒温湿度 ---
            if current_time - last_temp_upload_time >= TEMP_HUM_UPLOAD_INTERVAL:
                process_temp_humidity_upload()
                last_temp_upload_time = current_time
                
            # ================= 新增传感器循环上报区域 =================
            with state.lock:
                curr_presence = state.presence_state
                curr_targets = list(state.radar_targets) 

            # 4. 在场检测：状态改变或每5秒心跳发一次
            if (curr_presence != last_presence_state) or (current_time - last_presence_time >= 5.0):
                try:
                    payload = {"is_present": curr_presence, "location": "Living Room", "device_id": DEVICE_ID}
                    requests.post(PRESENCE_API_URL, json=payload, timeout=2)
                    last_presence_state = curr_presence
                    last_presence_time = current_time
                except Exception: pass

            valid_targets = [t for t in curr_targets if t['dist'] > 0]
            
            # 5. 雷达轨迹定点上传：限制每 5 秒推送一次
            if valid_targets and (current_time - last_location_time >= 5.0):
                try:
                    t = valid_targets[0] 
                    payload = {"location_x": t['x'], "location_y": t['y'], "speed": t['speed'], "device_id": DEVICE_ID}
                    requests.post(LOCATION_API_URL, json=payload, timeout=2)
                    last_location_time = current_time
                except Exception: pass
            
            # 6. 跌倒判定保护：冷却期 10 秒以上
            if current_time - last_fall_time >= 10.0:
                for t in valid_targets:
                    if t['speed'] > FALL_SPEED_THRESHOLD:
                        print(f"!!! FALL DETECTED !!! Speed: {t['speed']} cm/s")
                        try:
                            payload = {
                                "alert_type": "FALL", 
                                "message": "检测到跌倒 (速度异常)", 
                                "location_x": t['x'], 
                                "location_y": t['y'], 
                                "device_id": DEVICE_ID
                            }
                            requests.post(FALL_API_URL, json=payload, timeout=2)
                            last_fall_time = current_time
                        except Exception as e: 
                            pass
                        break
                        
            time.sleep(0.1)
            
    except KeyboardInterrupt:
        print("Stopping...")
        GPIO.cleanup()
