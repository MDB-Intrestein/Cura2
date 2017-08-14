import os
import sys
import random
import time

from unittest.mock import MagicMock, Mock
sys.path.append(os.path.realpath(os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..")))
sys.path.append(os.path.realpath(os.path.join(os.path.dirname(os.path.realpath(__file__)), "..")))

from pyMarlin import FakeMarlinSerialDevice
from pyMarlin import LoggingSerialConnection
from pyMarlin import NoisySerialConnection

class stk500v2:
    class Stk500v2:
        def connect(self, port="COM2", baudrate=250000):
            print(port, baudrate)

        def close(self):
            print("closed")

        def leaveISP(self):
            io = FakeMarlinSerialDevice()
            io = LoggingSerialConnection(io, "test.log")
            io = NoisySerialConnection(io)
            io.setErrorRate(1,20)
            return io

sys.modules["USBPrinting.USBPrinterOutputDeviceManager"] = MagicMock()
sys.modules["USBPrinting.avr_isp.stk500v2"] = stk500v2

from USBPrinting.USBPrinterOutputDevice import USBPrinterOutputDevice
from UM.Application import Application

Application._instance = Mock()

def generate_synthetic_gcode():
  gcode = ""
  non_acting_gcodes = ["G90", "G91", "G92 X0 Y0 Z0", "G92 X123 Y456", "M31", "M114", "M115", "M119"]
  for i in range(1, 10000):
    which = random.randrange(0,len(non_acting_gcodes))
    gcode += non_acting_gcodes[which] + "\n"
  return [gcode]

def test_connect():
    p = USBPrinterOutputDevice("test")
    p.connect()

    time.sleep(5)
    p.printGCode(generate_synthetic_gcode())

    p._connect_thread.join()
    p._listen_thread.join()
    pass

test_connect()
