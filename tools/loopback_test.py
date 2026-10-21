import serial
import time

PORT = '/dev/serial0'
BAUD = 115200

print(f"开始 UART 环回测试 (Loopback Test)")
print(f"端口: {PORT}, 波特率: {BAUD}")

try:
    ser = serial.Serial(PORT, BAUD, timeout=1)
    
    # 清空缓冲区
    ser.reset_input_buffer()
    ser.reset_output_buffer()
    
    test_msg = b"Hello, Raspberry Pi UART is working!\n"
    print(f"发送数据: {test_msg.decode('utf-8').strip()}")
    
    # 发送数据
    ser.write(test_msg)
    ser.flush() # 确保数据发送完毕
    
    # 稍微等待数据接收
    time.sleep(0.2)
    
    # 读取数据
    if ser.in_waiting > 0:
        recv_msg = ser.read(ser.in_waiting)
        print(f"接收数据: {recv_msg.decode('utf-8', errors='ignore').strip()}")
        
        if test_msg == recv_msg:
            print("\n✅ 测试通过！树莓派的 TX (GPIO 14) 和 RX (GPIO 15) 硬件功能完全正常。")
        else:
            print("\n❌ 测试失败！接收到的数据与发送的数据不一致。")
    else:
        print("\n❌ 测试失败！没有接收到任何数据。请确保短接线接触良好，并且连接的是正确的 Pin 8 (TX) 和 Pin 10 (RX)。")
        
    ser.close()

except Exception as e:
    print(f"串口错误: {e}")
