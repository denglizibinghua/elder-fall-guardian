import RPi.GPIO as GPIO
import time
import math
import serial
import threading

# --- 硬件配置 ---
LD2410_OUT_PIN = 6          # LD2410 高低电平数据引脚
UART4_PORT = '/dev/ttyAMA4' # LD2410 串口路线
UART2_PORT = '/dev/ttyAMA2' # LD2450 串口路线

def ld2410_worker():
    """解析 LD2410 (存在感应雷达) 的数据"""
    print("[LD2410] 存在雷达线程启动... (UART4)")
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    GPIO.setup(LD2410_OUT_PIN, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
    
    try:
        ser = serial.Serial(UART4_PORT, 256000, timeout=0.1)
    except Exception as e:
        print(f"[LD2410] 串口 {UART4_PORT} 初始化失败: {e}")
        ser = None
        
    buffer = b""
    while True:
        try:
            # 1. 硬件引脚读取
            out_high = (GPIO.input(LD2410_OUT_PIN) == GPIO.HIGH)
            data_state = 0
            
            # 2. 串口深度解析
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
                        
            presence_state = out_high or (data_state != 0)
            print(f"[LD2410-存在] 状态: {'👉 有人' if presence_state else '⚪ 无人'} (OUT引脚: {'高' if out_high else '低'}, UART状态位: {data_state})")
            
        except Exception as e:
            pass
            
        time.sleep(0.5)

def ld2450_worker():
    """解析 LD2450 (轨迹雷达) 的数据"""
    print("[LD2450] 轨迹雷达线程启动... (UART2)")
    try:
        ser = serial.Serial(UART2_PORT, 256000, timeout=0.1)
    except Exception as e:
        print(f"[LD2450] 串口 {UART2_PORT} 初始化失败: {e}")
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
                            for i in range(3):
                                x = calc_val(frame[offset], frame[offset+1])
                                y = calc_val(frame[offset+2], frame[offset+3])
                                speed = calc_val(frame[offset+4], frame[offset+5])
                                dist = math.sqrt(x*x + y*y) / 1000.0
                                
                                if x != 0 or y != 0:
                                    targets.append({
                                        'id': i+1,
                                        'dist_m': round(dist, 2), 
                                        'x_mm': x, 
                                        'y_mm': y, 
                                        'speed': speed
                                    })
                                offset += 8
                                
                            if len(targets) > 0:
                                print(f"[LD2450-轨迹] 侦测到 {len(targets)} 个活动目标:")
                                for t in targets:
                                    print(f"      -> 目标{t['id']}: 距离 {t['dist_m']}m, 坐标(X:{t['x_mm']}, Y:{t['y_mm']}), 速度: {t['speed']}")
                        else:
                            buffer = buffer[1:]
                    else:
                        break
        except Exception as e:
             pass
             
        time.sleep(0.3) 

if __name__ == "__main__":
    print("### 海凌科 LD2410 & LD2450 联合侦测测试 (按 Ctrl+C 退出) ###")
    
    t_2410 = threading.Thread(target=ld2410_worker, daemon=True)
    t_2410.start()
    
    t_2450 = threading.Thread(target=ld2450_worker, daemon=True)
    t_2450.start()
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n### 退出测试 ###")
        GPIO.cleanup()
