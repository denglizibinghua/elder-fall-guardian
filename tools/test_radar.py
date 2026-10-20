import serial
import time

# 树莓派默认硬件串口
SERIAL_PORT = '/dev/serial0'
BAUD_RATE = 115200

# 状态记录（用于避免相同状态疯狂刷屏）
last_fall_state = -1
last_target_num = -1

def calculate_crc16(data_bytes):
    """
    根据厂家协议实现的 CRC-16-XMODEM 0x1021 校验算法
    """
    crc = 0
    for b in data_bytes:
        code = (crc >> 8) & 0xFFFF
        code ^= b
        code ^= (code >> 4) & 0xFFFF
        crc = (crc << 8) & 0xFFFF
        crc ^= code
        code = (code << 5) & 0xFFFF
        crc ^= code
        code = (code << 7) & 0xFFFF
        crc ^= code
    return crc & 0xFFFF

def parse_a3_payload(payload):
    """
    解析 0xA3 指令（雷达数据上报）的负载(Payload)
    """
    global last_fall_state, last_target_num
    
    offset = 0
    target_num = 0
    fall_state = 0
    
    # 按照 TLV 格式循环解析 Payload
    while offset + 3 <= len(payload): # 至少要有 Type(1)+Length(2)
        tlv_type = payload[offset]
        # Length 是小端模式的 2 字节，表示整个 TLV 的总长度
        tlv_len = payload[offset+1] | (payload[offset+2] << 8)
        
        # 防止协议长度错误导致的死循环
        if tlv_len < 3 or offset + tlv_len > len(payload):
            break
            
        # 判断类型
        if tlv_type == 0x02:
            # Type 0x02: 人员跌倒状态 (第4个字节是 Value)
            fall_state = payload[offset+3]
        elif tlv_type == 0x03:
            # Type 0x03: 人员数量 (第4个字节是 Value)
            target_num = payload[offset+3]
            
        # 跳到下一个 TLV 数据块
        offset += tlv_len

    # 如果状态发生了改变，或者是处于跌倒报警状态，则打印出来
    if fall_state != last_fall_state or target_num != last_target_num or fall_state == 1:
        if fall_state == 1:
            print(f"\n🚨🚨🚨 严重警告：检测到跌倒！ (区域内人数: {target_num}) 🚨🚨🚨")
        else:
            if target_num > 0:
                print(f"🟢 状态正常：检测到活动，无人跌倒 (区域内人数: {target_num})")
            else:
                print(f"⚪ 场景无人监控中...")
                
        last_fall_state = fall_state
        last_target_num = target_num


def main():
    buffer = bytearray()
    
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0.1)
        print(f"成功打开串口: {SERIAL_PORT}")
        print("已启动跌倒状态解析，请在雷达前方活动...")
        print("=========================================")

        while True:
            # 1. 批量读取串口数据存入缓存
            if ser.in_waiting > 0:
                buffer.extend(ser.read(ser.in_waiting))
                
            # 2. 从缓存中寻找并提取完整的帧
            # 一个最短的帧至少包含 STX(1)+LEN(1)+FLAG(1)+CMD(1)+CRC(2) = 6字节
            while len(buffer) >= 6:
                # 寻找帧头 0x5A
                if buffer[0] != 0x5A:
                    buffer.pop(0) # 丢弃错误的数据
                    continue
                    
                payload_len = buffer[1]
                # 计算完整的帧总长度
                total_frame_len = 1 + 1 + 1 + 1 + payload_len + 2
                
                # 如果缓存中的数据还不够一整帧，先跳出循环等待后续接收
                if len(buffer) < total_frame_len:
                    break
                    
                # 提取完整的一帧数据
                frame = buffer[:total_frame_len]
                del buffer[:total_frame_len] # 从缓存中移除已提取的帧
                
                # 3. 校验 CRC16
                calc_crc = calculate_crc16(frame[:-2]) # 计算 STX 到 Payload 的 CRC
                recv_crc = frame[-2] | (frame[-1] << 8) # 提取末尾两字节的小端 CRC
                
                if calc_crc != recv_crc:
                    # CRC 校验失败，可能是干扰导致的错误包，忽略
                    continue
                    
                # 4. 解析指令
                cmd = frame[3]
                if cmd == 0xA3: # 0xA3 是雷达检测信息上报
                    payload = frame[4 : 4+payload_len]
                    parse_a3_payload(payload)

            time.sleep(0.01)

    except serial.SerialException as e:
        print(f"串口错误: {e}")
    except KeyboardInterrupt:
        print("\n测试终止。")
    finally:
        if 'ser' in locals() and ser.is_open:
            ser.close()

if __name__ == '__main__':
    main()