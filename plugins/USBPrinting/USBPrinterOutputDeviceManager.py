# Copyright (c) 2017 Ultimaker B.V.
# Cura is released under the terms of the AGPLv3 or higher.

from UM.Signal import Signal, signalemitter
from .USBPrinterOutputDevice import USBPrinterOutputDevice
from UM.Application import Application
from UM.Resources import Resources
from UM.Logger import Logger
from UM.PluginRegistry import PluginRegistry
from UM.OutputDevice.OutputDevicePlugin import OutputDevicePlugin
from cura.PrinterOutputDevice import ConnectionState
from UM.Qt.ListModel import ListModel
from UM.Message import Message

from cura.CuraApplication import CuraApplication

import threading
import platform
import glob
import time
import os.path
import serial.tools.list_ports
from UM.Extension import Extension

from PyQt5.QtQml import QQmlComponent, QQmlContext
from PyQt5.QtCore import QUrl, QObject, pyqtSlot, pyqtProperty, pyqtSignal, Qt
from UM.i18n import i18nCatalog
i18n_catalog = i18nCatalog("cura")


##  Manager class that ensures that a usbPrinteroutput device is created for every connected USB printer.
@signalemitter
class USBPrinterOutputDeviceManager(QObject, OutputDevicePlugin, Extension):
    def __init__(self, parent = None):
        super().__init__(parent = parent)
        self._serial_port_list = []
        self._usb_output_devices = {}
        self._usb_output_devices_model = None
        self._update_thread = threading.Thread(target = self._updateThread)
        self._update_thread.setDaemon(True)

        self._check_updates = True
        self._firmware_view = None

        Application.getInstance().applicationShuttingDown.connect(self.stop)
        self.addUSBOutputDeviceSignal.connect(self.addOutputDevice) #Because the model needs to be created in the same thread as the QMLEngine, we use a signal.

    addUSBOutputDeviceSignal = Signal()
    connectionStateChanged = pyqtSignal()

    progressChanged = pyqtSignal()
    firmwareUpdateChange = pyqtSignal()

    @pyqtProperty(float, notify = progressChanged)
    def progress(self):
        progress = 0
        for printer_name, device in self._usb_output_devices.items(): # TODO: @UnusedVariable "printer_name"
            progress += device.progress
        return progress / len(self._usb_output_devices)

    @pyqtProperty(int, notify = progressChanged)
    def errorCode(self):
        for printer_name, device in self._usb_output_devices.items(): # TODO: @UnusedVariable "printer_name"
            if device._error_code:
                return device._error_code
        return 0

    ##  Return True if all printers finished firmware update
    @pyqtProperty(float, notify = firmwareUpdateChange)
    def firmwareUpdateCompleteStatus(self):
        complete = True
        for printer_name, device in self._usb_output_devices.items(): # TODO: @UnusedVariable "printer_name"
            if not device.firmwareUpdateFinished:
                complete = False
        return complete

    def start(self):
        self._check_updates = True
        self._update_thread.start()

    def stop(self):
        self._check_updates = False

    def _updateThread(self):
        while self._check_updates:
            result = self.getSerialPortList(only_list_usb = True)
            self._addRemovePorts(result)
            time.sleep(5)

    ##  Show firmware interface.
    #   This will create the view if its not already created.
    def spawnFirmwareInterface(self, serial_port):
        if self._firmware_view is None:
            path = QUrl.fromLocalFile(os.path.join(PluginRegistry.getInstance().getPluginPath("USBPrinting"), "FirmwareUpdateWindow.qml"))
            component = QQmlComponent(Application.getInstance()._engine, path)

            self._firmware_context = QQmlContext(Application.getInstance()._engine.rootContext())
            self._firmware_context.setContextProperty("manager", self)
            self._firmware_view = component.create(self._firmware_context)

        self._firmware_view.show()

    @pyqtSlot(str, bool)
    def updateAllFirmware(self, file_name, update_eeprom):
        if file_name.startswith("file://"):
            file_name = QUrl(file_name).toLocalFile() # File dialogs prepend the path with file://, which we don't need / want
        if not self._usb_output_devices:
            Message(i18n_catalog.i18nc("@info", "Unable to update firmware because there are no printers connected.")).show()
            return

        for printer_connection in self._usb_output_devices:
            self._usb_output_devices[printer_connection].resetFirmwareUpdate()
        self.spawnFirmwareInterface("")
        for printer_connection in self._usb_output_devices:
            try:
                self._usb_output_devices[printer_connection].updateFirmware(file_name, update_eeprom)
            except FileNotFoundError:
                # Should only happen in dev environments where the resources/firmware folder is absent.
                self._usb_output_devices[printer_connection].setProgress(100, 100)
                Logger.log("w", "No firmware found for printer %s called '%s'", printer_connection, file_name)
                Message(i18n_catalog.i18nc("@info",
                    "Could not find firmware required for the printer at %s.") % printer_connection).show()
                self._firmware_view.close()

                continue

    @pyqtSlot(str, str, result = bool)
    def updateFirmwareBySerial(self, serial_port, file_name):
        if serial_port in self._usb_output_devices:
            self.spawnFirmwareInterface(self._usb_output_devices[serial_port].getSerialPort())
            try:
                self._usb_output_devices[serial_port].updateFirmware(file_name)
            except FileNotFoundError:
                self._firmware_view.close()
                Logger.log("e", "Could not find firmware required for this machine called '%s'", file_name)
                return False
            return True
        return False

    ##  Return the singleton instance of the USBPrinterManager
    @classmethod
    def getInstance(cls, engine = None, script_engine = None):
        # Note: Explicit use of class name to prevent issues with inheritance.
        if USBPrinterOutputDeviceManager._instance is None:
            USBPrinterOutputDeviceManager._instance = cls()

        return USBPrinterOutputDeviceManager._instance

    @pyqtSlot(result = str)
    def getDefaultFirmwareName(self):
        # Check if there is a valid global container stack
        global_container_stack = Application.getInstance().getGlobalContainerStack()
        if not global_container_stack:
            Logger.log("e", "There is no global container stack. Can not update firmware.")
            self._firmware_view.close()
            return ""

        # The bottom of the containerstack is the machine definition
        machine_id = global_container_stack.getBottom().id

        machine_has_heated_bed = global_container_stack.getProperty("machine_heated_bed", "value")

        if platform.system() == "Linux":
            baudrate = 115200
        else:
            baudrate = 250000

        # NOTE: The keyword used here is the id of the machine. You can find the id of your machine in the *.json file, eg.
        # https://github.com/Ultimaker/Cura/blob/master/resources/machines/ultimaker_original.json#L2
        # The *.hex files are stored at a seperate repository:
        # https://github.com/Ultimaker/cura-binary-data/tree/master/cura/resources/firmware
        machine_without_extras  = {"bq_witbox"                : "MarlinWitbox.hex",
                                   "bq_hephestos_2"           : "MarlinHephestos2.hex",
                                   "ultimaker_original"       : "MarlinUltimaker-{baudrate}.hex",
                                   "ultimaker_original_plus"  : "MarlinUltimaker-UMOP-{baudrate}.hex",
                                   "ultimaker_original_dual"  : "MarlinUltimaker-{baudrate}-dual.hex",
                                   "ultimaker2"               : "MarlinUltimaker2.hex",
                                   "ultimaker2_go"            : "MarlinUltimaker2go.hex",
                                   "ultimaker2_plus"          : "MarlinUltimaker2plus.hex",
                                   "ultimaker2_extended"      : "MarlinUltimaker2extended.hex",
                                   "ultimaker2_extended_plus" : "MarlinUltimaker2extended-plus.hex",
                                   }
        machine_with_heated_bed = {"ultimaker_original"       : "MarlinUltimaker-HBK-{baudrate}.hex",
                                   "ultimaker_original_dual"  : "MarlinUltimaker-HBK-{baudrate}-dual.hex",
                                   }
        lulzbot_machines = {
            "lulzbot_mini":                     "Marlin_Mini_SingleExtruder_1.1.5.70_578391eff.hex",
            "lulzbot_mini_flexy":                 "Marlin_Mini_Flexystruder_1.1.5.70_578391eff.hex",
            "lulzbot_mini_aerostruder":            "Marlin_Mini_Aerostruder_1.1.5.70_578391eff.hex",

            "lulzbot_taz5":                     "Marlin_TAZ5_SingleExtruder_1.1.5.70_578391eff.hex",
            "lulzbot_taz5_flexy_v2":              "Marlin_TAZ5_Flexystruder_1.1.5.70_578391eff.hex",
            "lulzbot_taz5_moarstruder":            "Marlin_TAZ5_Moarstruder_1.1.5.70_578391eff.hex",
            "lulzbot_taz5_dual_v2":             "Marlin_TAZ5_DualExtruderV2_1.1.5.70_578391eff.hex",
            "lulzbot_taz5_flexy_dually_v2":        "Marlin_TAZ5_FlexyDually_1.1.5.70_578391eff.hex",
            "lulzbot_taz5_dual_v3":             "Marlin_TAZ5_DualExtruderV3_1.1.5.70_578391eff.hex",
            "lulzbot_taz5_aerostruder":            "Marlin_TAZ5_Aerostruder_1.1.5.70_578391eff.hex",

            "lulzbot_taz6":                     "Marlin_TAZ6_SingleExtruder_1.1.5.70_578391eff.hex",
            "lulzbot_taz6_flexy_v2":              "Marlin_TAZ6_Flexystruder_1.1.5.70_578391eff.hex",
            "lulzbot_taz6_moarstruder":            "Marlin_TAZ6_Moarstruder_1.1.5.70_578391eff.hex",
            "lulzbot_taz6_dual_v2":             "Marlin_TAZ6_DualExtruderV2_1.1.5.70_578391eff.hex",
            "lulzbot_taz6_flexy_dually_v2":        "Marlin_TAZ6_FlexyDually_1.1.5.70_578391eff.hex",
            "lulzbot_taz6_dual_v3":             "Marlin_TAZ6_DualExtruderV3_1.1.5.70_578391eff.hex",
            "lulzbot_taz6_aerostruder":            "Marlin_TAZ6_Aerostruder_1.1.5.70_578391eff.hex",
        }

        lulzbot_lcd_machines = {
            "lulzbot_mini":                  "Marlin_MiniLCD_SingleExtruder_1.1.5.70_578391eff.hex",
            "lulzbot_mini_flexy":              "Marlin_MiniLCD_Flexystruder_1.1.5.70_578391eff.hex",
            "lulzbot_mini_aerostruder":         "Marlin_MiniLCD_Aerostruder_1.1.5.70_578391eff.hex",
        }

        ##TODO: Add check for multiple extruders
        hex_file = None
        if machine_id in machine_without_extras.keys():  # The machine needs to be defined here!
            if machine_id in machine_with_heated_bed.keys() and machine_has_heated_bed:
                Logger.log("d", "Choosing firmware with heated bed enabled for machine %s.", machine_id)
                hex_file = machine_with_heated_bed[machine_id]  # Return firmware with heated bed enabled
            else:
                Logger.log("d", "Choosing basic firmware for machine %s.", machine_id)
                hex_file = machine_without_extras[machine_id]  # Return "basic" firmware
        elif machine_id in lulzbot_machines.keys():
            machine_has_lcd = global_container_stack.getProperty("machine_has_lcd", "value")
            if machine_id in lulzbot_lcd_machines.keys() and machine_has_lcd:
                Logger.log("d", "Found firmware with LCD for machine %s.", machine_id)
                hex_file = lulzbot_lcd_machines[machine_id]
            else:
                Logger.log("d", "Found firmware for machine %s.", machine_id)
                hex_file = lulzbot_machines[machine_id]
        else:
            Logger.log("w", "There is no firmware for machine %s.", machine_id)

        if hex_file:
            return Resources.getPath(CuraApplication.ResourceTypes.Firmware, hex_file.format(baudrate=baudrate))
        else:
            Logger.log("w", "Could not find any firmware for machine %s.", machine_id)
            return ""

    ##  Helper to identify serial ports (and scan for them)
    def _addRemovePorts(self, serial_ports):
        if len(self._serial_port_list) == 0 and len(serial_ports) > 0:
            # Hack to ensure its created in main thread
            self.addUSBOutputDeviceSignal.emit(USBPrinterOutputDevice.SERIAL_AUTODETECT_PORT)
        elif len(serial_ports) == 0:
            for port, device in self._usb_output_devices.items():
                device.close()
            self._usb_output_devices = {}
            self.getOutputDeviceManager().removeOutputDevice(USBPrinterOutputDevice.SERIAL_AUTODETECT_PORT)
        self._serial_port_list = list(serial_ports)

    ##  Because the model needs to be created in the same thread as the QMLEngine, we use a signal.
    def addOutputDevice(self, serial_port):
        device = USBPrinterOutputDevice(serial_port)
        device.connectionStateChanged.connect(self._onConnectionStateChanged)
        #device.connect()
        device.progressChanged.connect(self.progressChanged)
        device.firmwareUpdateChange.connect(self.firmwareUpdateChange)
        self._usb_output_devices[serial_port] = device
        self.getOutputDeviceManager().addOutputDevice(device)

    ##  If one of the states of the connected devices change, we might need to add / remove them from the global list.
    def _onConnectionStateChanged(self, serial_port):
        try:
            self.connectionStateChanged.emit()
        except KeyError:
            Logger.log("w", "Connection state of %s changed, but it was not found in the list")

    @pyqtProperty(QObject , notify = connectionStateChanged)
    def connectedPrinterList(self):
        self._usb_output_devices_model = ListModel()
        self._usb_output_devices_model.addRoleName(Qt.UserRole + 1, "name")
        self._usb_output_devices_model.addRoleName(Qt.UserRole + 2, "printer")
        for connection in self._usb_output_devices:
            if self._usb_output_devices[connection].connectionState == ConnectionState.connected:
                self._usb_output_devices_model.appendItem({"name": connection, "printer": self._usb_output_devices[connection]})
        return self._usb_output_devices_model

    ##  Create a list of serial ports on the system.
    #   \param only_list_usb If true, only usb ports are listed
    def getSerialPortList(self, only_list_usb = False):
        base_list = []
        if platform.system() == "Windows":
            import winreg # type: ignore @UnresolvedImport
            try:
                key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,"HARDWARE\\DEVICEMAP\\SERIALCOMM")
                i = 0
                while True:
                    values = winreg.EnumValue(key, i)
                    if not only_list_usb or "USBSER" or "VCP" in values[0]:
                        base_list += [values[1]]
                    i += 1
            except Exception as e:
                pass
        else:
            if only_list_usb:
                base_list = base_list + glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*") + glob.glob("/dev/cu.usb*") + glob.glob("/dev/tty.wchusb*") + glob.glob("/dev/cu.wchusb*")
                base_list = filter(lambda s: "Bluetooth" not in s, base_list) # Filter because mac sometimes puts them in the list
            else:
                base_list = base_list + glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*") + glob.glob("/dev/cu.*") + glob.glob("/dev/tty.usb*") + glob.glob("/dev/tty.wchusb*") + glob.glob("/dev/cu.wchusb*") + glob.glob("/dev/rfcomm*") + glob.glob("/dev/serial/by-id/*")
        return list(base_list)

    @pyqtProperty("QVariantList", constant=True)
    def portList(self):
        return self.getSerialPortList()

    @pyqtSlot(str, result=bool)
    def sendCommandToCurrentPrinter(self, command):
        try:
            printer = Application.getInstance().getMachineManager().printerOutputDevices[0]
        except:
            return False
        if type(printer) != USBPrinterOutputDevice:
            return False
        printer.sendCommand(command)
        return True

    @pyqtSlot(result=bool)
    def connectToCurrentPrinter(self):
        try:
            printer = Application.getInstance().getMachineManager().printerOutputDevices[0]
        except:
            return False
        if type(printer) != USBPrinterOutputDevice:
            return False
        printer.connect()
        return True

    _instance = None    # type: "USBPrinterOutputDeviceManager"
