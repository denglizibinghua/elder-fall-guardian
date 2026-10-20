import RPi.GPIO as GPIO
import smbus
import time
import math

# GPIO Defines
BUTTON_PIN = 17
HEART_RATE_PIN = 23

# MPU6050 Registers and Address
MPU6050_ADDR = 0x68
PWR_MGMT_1 = 0x6B
ACCEL_XOUT_H = 0x3B

# Variables for Step Counting
step_count = 0
last_mag = 0
step_threshold = 25000 # Threshold for step detection (adjust based on sensitivity)
last_step_time = 0

# Variables for Heart Rate (Modeled after PulseSensor Arduino Library)
last_beat_time = 0
ibi = 0.6         # Inter-Beat Interval (Delta time between beats), default 600ms (100 BPM)
bpm = 0
pulse = False
last_input = 0

def mpu6050_init(bus):
    # Wake up the MPU6050 (it starts in sleep mode)
    try:
        bus.write_byte_data(MPU6050_ADDR, PWR_MGMT_1, 0)
    except Exception as e:
        print(f"I2C Write Error: {e}")

def read_raw_data(bus, addr):
    try:
        # Read two bytes of data
        high = bus.read_byte_data(MPU6050_ADDR, addr)
        low = bus.read_byte_data(MPU6050_ADDR, addr+1)
        
        # Combine high and low for 16-bit value
        value = ((high << 8) | low)
        
        # Get signed value
        if(value > 32768):
            value = value - 65536
        return value
    except Exception as e:
        return 0

def setup():
    GPIO.setmode(GPIO.BCM)
    
    # Setup Button
    # Based on feedback, we just read logic level. 
    # Use PUD_UP to be safe, but code logic handles the inversion.
    GPIO.setup(BUTTON_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    
    # Setup Heart Rate Sensor
    # KEY CHANGE: Change from PUD_UP to PUD_OFF
    # Because this is likely an analog sensor, the internal pull-up might be interfering 
    # with the weak analog voltage signal. disabling it allows the sensor to drive the pin cleanly.
    GPIO.setup(HEART_RATE_PIN, GPIO.IN, pull_up_down=GPIO.PUD_OFF) 

def calculate_bpm_arduino_style(current_time, signal_state):
    global last_beat_time, ibi, bpm, pulse
    
    # Arduino Logic Adaptation:
    # The Arduino code uses analog values (0-1023) and a threshold (550).
    # Since we are on Digital GPIO, our "value" is only 0 or 1.
    # Our "Threshold" is effectively 0.5.
    # So: High(1) is > Threshold, Low(0) is < Threshold.
    
    # "isInsideBeat" logic
    if signal_state == 1 and not pulse: 
        # RISING EDGE (Signal crossed threshold up)
        pulse = True
        
        # Calculate IBI (Inter-Beat Interval)
        prev_time = last_beat_time
        last_beat_time = current_time
        
        # Avoid first-run garbage
        if prev_time == 0:
            return 0
            
        current_ibi = current_time - prev_time
        
        # Filter: IBI must be reasonable (e.g., < 220 BPM and > 30 BPM)
        # 0.27s < IBI < 2.0s
        if 0.27 < current_ibi < 2.0:
            ibi = current_ibi
            bpm = 60.0 / ibi
            return bpm
            
    elif signal_state == 0 and pulse:
        # FALLING EDGE (Signal dropped below threshold)
        pulse = False
        
    return bpm

def detect_step(acc_x, acc_y, acc_z):
    global step_count, last_mag, last_step_time
    
    # Calculate Magnitude
    current_mag = math.sqrt(acc_x**2 + acc_y**2 + acc_z**2)
    
    current_time = time.time()
    
    # Step detection: detecting peaks above threshold
    # Ensure minimum time between steps (0.3s)
    if current_mag > step_threshold and (current_time - last_step_time) > 0.3:
        step_count += 1
        last_step_time = current_time
        return True
    
    last_mag = current_mag
    return False

def loop():
    global bpm
    
    # Setup I2C
    try:
        bus = smbus.SMBus(1) # Bus 1 for Pi 2/3/4/5
        mpu6050_init(bus)
        print("MPU6050 Initialized")
    except Exception as e:
        print(f"Error initializing MPU6050: {e}")

    print("Sensors Active... Press Ctrl+C to exit.")
    print(f"{'BUTTON':<15} | {'HEART RATE':<15} | {'STEPS':<10} | {'ACCEL MAG':<10}")
    print("-" * 65)

    last_heart_pin_state = GPIO.input(HEART_RATE_PIN)
    
    while True:
        # 1. Read Button
        # User Feedback: "Not pressing is pressed (LOW)" -> So we treat LOW as RELEASED now?
        # User Request: "现在按下是released，不按是pressed。" -> Current state.
        # Desired: "Pressed" when Pressed.
        # Logic: If Logic LOW means Unpressed (per user saying "Not pressing is pressed" and code saying Pressed on LOW)
        # Wait, if "Not pressing is pressed" and code printed "PRESSED" when LOW, then LOW = Unpressed hardware-wise?
        # Let's assume:
        # Pressed -> HIGH
        # Released -> LOW
        btn_raw = GPIO.input(BUTTON_PIN)
        if btn_raw == GPIO.HIGH:
             btn_str = "PRESSED"
        else:
             btn_str = "RELEASED"
            
        # 2. Heart Rate (Arduino Logic Port)
        hr_raw = GPIO.input(HEART_RATE_PIN)
        current_time = time.time()
        
        # Run the logic every loop
        calculate_bpm_arduino_style(current_time, hr_raw)
        
        # Timeout Reset: If no beat for 2.5s (likely finger removed)
        if (current_time - last_beat_time) > 2.5:
             bpm = 0

        # 3. MPU6050 Steps
        acc_x = read_raw_data(bus, ACCEL_XOUT_H)
        acc_y = read_raw_data(bus, ACCEL_XOUT_H + 2)
        acc_z = read_raw_data(bus, ACCEL_XOUT_H + 4)
        
        is_step = detect_step(acc_x, acc_y, acc_z)
        mag_display = int(math.sqrt(acc_x**2 + acc_y**2 + acc_z**2))

        # Output Update
        # Visualizer for raw pin state: _ = Low, ^ = High
        # This helps user calibrate the potentiometer on the sensor
        raw_graph = "^" if hr_raw else "_"
        
        # Only print every ~100ms to avoid console spam flickering, but sample much faster
        # We use a simple counter or time check for printing
        if (current_time * 1000) % 100 < 20: 
             print(f"\r{btn_str:<15} | {int(bpm):<3} BPM [{raw_graph}]  | {step_count:<10} | {mag_display:<10}", end="")
        
        # Very fast loop for better sampling resolution
        time.sleep(0.01)

if __name__ == "__main__":
    try:
        setup()
        loop()
    except KeyboardInterrupt:
        print("\nTest stopped by user.")
    except Exception as e:
        print(f"\nAn error occurred: {e}")
    finally:
        GPIO.cleanup()
