"""MAX30102 心率血氧传感器驱动（I2C）。

main.py 用法：``MAX30102(channel=4, gpio_pin=4)`` —— 挂在软件 I2C 总线 4
（GPIO23/24 见 config.txt dtoverlay），中断脚 BCM GPIO4。
"""

import time
from time import sleep

import RPi.GPIO as GPIO
import smbus

# register address-es
REG_INTR_STATUS_1 = 0x00
REG_INTR_STATUS_2 = 0x01

REG_INTR_ENABLE_1 = 0x02
REG_INTR_ENABLE_2 = 0x03

REG_FIFO_WR_PTR = 0x04
REG_OVF_COUNTER = 0x05
REG_FIFO_RD_PTR = 0x06
REG_FIFO_DATA = 0x07
REG_FIFO_CONFIG = 0x08

REG_MODE_CONFIG = 0x09
REG_SPO2_CONFIG = 0x0A
REG_LED1_PA = 0x0C

REG_LED2_PA = 0x0D
REG_PILOT_PA = 0x10


class MAX30102():
    # by default, this assumes that physical pin 7 (GPIO 4) is used as interrupt
    # by default, this assumes that the device is at 0x57 on channel 1
    def __init__(self, channel=1, address=0x57, gpio_pin=7):
        print("Channel: {0}, address: 0x{1:x}".format(channel, address))
        self.address = address
        self.channel = channel
        self.bus = smbus.SMBus(self.channel)
        self.interrupt = gpio_pin

        GPIO.setup(self.interrupt, GPIO.IN)

        self.reset()

        sleep(1)  # wait 1 sec

        # read & clear interrupt register (read 1 byte)
        self.bus.read_i2c_block_data(self.address, REG_INTR_STATUS_1, 1)

        self.setup()

    def shutdown(self):
        """
        Shutdown the device.
        """
        self.bus.write_i2c_block_data(self.address, REG_MODE_CONFIG, [0x80])

    def reset(self):
        """
        Reset the device, this will clear all settings,
        so after running this, run setup() again.
        """
        self.bus.write_i2c_block_data(self.address, REG_MODE_CONFIG, [0x40])

    def setup(self, led_mode=0x03):
        """
        This will setup the device with the values written in sample Arduino code.
        """
        # INTR setting
        # 0xc0 : A_FULL_EN and PPG_RDY_EN = Interrupt will be triggered when
        # fifo almost full & new fifo data ready
        self.bus.write_i2c_block_data(self.address, REG_INTR_ENABLE_1, [0xc0])
        self.bus.write_i2c_block_data(self.address, REG_INTR_ENABLE_2, [0x00])

        # FIFO_WR_PTR[4:0]
        self.bus.write_i2c_block_data(self.address, REG_FIFO_WR_PTR, [0x00])
        # OVF_COUNTER[4:0]
        self.bus.write_i2c_block_data(self.address, REG_OVF_COUNTER, [0x00])
        # FIFO_RD_PTR[4:0]
        self.bus.write_i2c_block_data(self.address, REG_FIFO_RD_PTR, [0x00])

        # 0b 0100 1111
        # sample avg = 4, fifo rollover = false, fifo almost full = 17
        self.bus.write_i2c_block_data(self.address, REG_FIFO_CONFIG, [0x4f])

        # 0x02 for read-only, 0x03 for SpO2 mode, 0x07 multimode LED
        self.bus.write_i2c_block_data(self.address, REG_MODE_CONFIG, [led_mode])
        # 0b 0010 0111
        # SPO2_ADC range = 4096nA, SPO2 sample rate = 100Hz, LED pulse-width = 411uS
        self.bus.write_i2c_block_data(self.address, REG_SPO2_CONFIG, [0x27])

        # choose value for ~7mA for LED1
        self.bus.write_i2c_block_data(self.address, REG_LED1_PA, [0x24])
        # choose value for ~7mA for LED2
        self.bus.write_i2c_block_data(self.address, REG_LED2_PA, [0x24])
        # choose value fro ~25mA for Pilot LED
        self.bus.write_i2c_block_data(self.address, REG_PILOT_PA, [0x7f])

    # this won't validate the arguments!
    # use when changing the values from default
    def set_config(self, reg, value):
        self.bus.write_i2c_block_data(self.address, reg, value)

    def read_fifo(self):
        """
        This function will read the data register.
        """
        # read 1 byte from interrupt status registers (clears them, value discarded)
        self.bus.read_i2c_block_data(self.address, REG_INTR_STATUS_1, 1)
        self.bus.read_i2c_block_data(self.address, REG_INTR_STATUS_2, 1)

        # read 6-byte data from the device
        d = self.bus.read_i2c_block_data(self.address, REG_FIFO_DATA, 6)

        # mask MSB [23:18]
        red_led = (d[0] << 16 | d[1] << 8 | d[2]) & 0x03FFFF
        ir_led = (d[3] << 16 | d[4] << 8 | d[5]) & 0x03FFFF

        return red_led, ir_led

    def read_sequential(self, amount=100, timeout_s=10.0):
        """Read red/ir `amount` times; blocks until the interrupt pin goes low.

        The interrupt pin is active-low: it goes LOW when new FIFO data is
        ready and returns HIGH after the FIFO is read. Waiting therefore means
        "wait for the pin to FALL back to 0".

        A bare ``while GPIO.input(...) == 1: pass`` busy-loop pegs a core at
        100% (observed on the Pi), so poll with a short sleep instead.

        Raises:
            TimeoutError: no data ready within ``timeout_s`` (sensor detached?).
        """
        red_buf = []
        ir_buf = []
        deadline = time.monotonic() + timeout_s
        for _ in range(amount):
            while GPIO.input(self.interrupt) == 1:
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        f"MAX30102: no data ready on GPIO{self.interrupt} within {timeout_s}s"
                    )
                time.sleep(0.001)

            red, ir = self.read_fifo()
            red_buf.append(red)
            ir_buf.append(ir)

        return red_buf, ir_buf
