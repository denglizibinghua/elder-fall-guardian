import time
import board
import adafruit_dht

# 初始化传感器
# 如果你接的是 GPIO 4，就用 board.D4
# 如果是其他引脚，比如 GPIO 17，就用 board.D17
try:
    dht_device = adafruit_dht.DHT11(board.D5)
except Exception as e:
    print(f"初始化传感器失败: {e}")
    exit()

print("开始读取温湿度 (按 Ctrl+C 停止)...")

while True:
    try:
        # 读取温度 (摄氏度)
        temperature = dht_device.temperature
        # 读取湿度 (%)
        humidity = dht_device.humidity
        
        if temperature is not None and humidity is not None:
            print(f"温度: {temperature:.1f}°C  |  湿度: {humidity}%")
        else:
            print("读取失败，数据为空")

    except RuntimeError as error:
        # DHT11 很便宜，经常会读取超时或校验错误，这非常正常
        # 只要不是一直报错，偶尔报错可以忽略
        print(f"传感器临时错误: {error.args[0]}")
        time.sleep(2.0)
        continue
        
    except Exception as error:
        dht_device.exit()
        raise error

    # DHT11 采样率很低，必须间隔至少 2 秒
    time.sleep(2.0)