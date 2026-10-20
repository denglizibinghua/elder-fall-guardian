import time
import sys
import max30102
import hrcalc
import RPi.GPIO as GPIO

def test_sensor():
    print("========================================")
    print("MAX30102 心率与血氧传感器测试程序")
    print("正在 I2C 总线 4 上初始化传感器...")
    print("注意: 请确保传感器的 INT 引脚已连接到树莓派的 物理引脚 7 (GPIO 4)")
    print("========================================")
    
    try:
        # 指定使用 I2C channel 4，内部默认 INT 是指物理引脚 7
        m = max30102.MAX30102(channel=4)
    except Exception as e:
        print(f"初始化失败: {e}")
        sys.exit(1)
        
    print("初始化成功！\n请将手指平稳地放在 MAX30102 传感器上，正在采集中...\n")

    try:
        while True:
            try:
                # 连续读取 100 个样本点，这大约需要几秒钟（依赖内置的采样率和中断信号）
                red, ir = m.read_sequential()
                
                # 传入红外光和红光数组进行心率和血氧解算
                hr, hr_valid, spo2, spo2_valid = hrcalc.calc_hr_and_spo2(ir, red)
                
                # 控制台输出解析结果
                if hr_valid and spo2_valid:
                    print(f"[成功] 心率 (HR): {hr:5.1f} bpm | 血氧 (SpO2): {spo2:5.1f} %")
                elif hr_valid:
                    print(f"[计算中] 心率 (HR): {hr:5.1f} bpm | 血氧数据不足...")
                elif spo2_valid:
                    print(f"[计算中] 心率数据不足... | 血氧 (SpO2): {spo2:5.1f} %")
                else:
                    print("[未稳定] 正在读取原始数据，请保持手指平稳且接触良好...")
                    
            except OSError as e:
                # 捕获 I2C / IO 报错，防止程序崩溃
                print(f"[警告] I2C 读取数据丢失或通信中断 (Errno 5)，正在重试... 错误: {e}")
                time.sleep(0.5)
                # 尝试重新复位一下以防总线死锁
                try: m.reset() 
                except: pass
                
    except KeyboardInterrupt:
        print("\n\n测试已由用户手动停止。")
    finally:
        # 清理 GPIO 状态
        GPIO.cleanup()

if __name__ == "__main__":
    test_sensor()