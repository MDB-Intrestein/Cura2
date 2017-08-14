# Copyright (c) 2016 Ultimaker B.V.
# Cura is released under the terms of the AGPLv3 or higher.

DEBUG_SERIAL_RETRY = True

from .avr_isp import stk500v2, ispBase, intelHex
import serial   # type: ignore
import threading
import time
import queue
import re
import functools

from enum import Enum

from UM.Application import Application
from UM.Logger import Logger
from cura.PrinterOutputDevice import PrinterOutputDevice, ConnectionState
from UM.Message import Message

from PyQt5.QtWidgets import QMessageBox
from PyQt5.QtCore import QUrl, pyqtSlot, pyqtSignal, pyqtProperty

from pyMarlin import MarlinSerialProtocol
from pyMarlin import NoisySerialConnection
from pyMarlin import LoggingSerialConnection

from UM.i18n import i18nCatalog
catalog = i18nCatalog("cura")

class Error(Enum):
    SUCCESS = 0
    PRINTER_BUSY = 1
    PRINTER_NOT_CONNECTED = 2

class USBPrinterOutputDevice(PrinterOutputDevice):
    SERIAL_AUTODETECT_PORT = "Autodetect"

    def __init__(self, serial_port):
        super().__init__(serial_port)
        self.setName(catalog.i18nc("@item:inmenu", "USB printing"))
        self.setShortDescription(catalog.i18nc("@action:button Preceded by 'Ready to'.", "Print via USB"))
        self.setDescription(catalog.i18nc("@info:tooltip", "Print via USB"))
        self.setIconName("print")
        self._autodetect_port = (serial_port == USBPrinterOutputDevice.SERIAL_AUTODETECT_PORT)
        if self._autodetect_port:
            serial_port = None
            self.setConnectionText(catalog.i18nc("@info:status", "USB device available"))
        else:
            self.setConnectionText(catalog.i18nc("@info:status", "Connect to %s" % serial_port))

        self._serial = None
        self._serial_port = serial_port
        self._serial_buffered = None
        self._error_state = None

        self._connect_thread = threading.Thread(target = self._connect_thread_function)
        self._connect_thread.daemon = True

        self._end_stop_thread = None
        self._poll_endstop = False

        # The baud checking is done by sending a number of m105 commands to the printer and waiting for a readable
        # response. If the baudrate is correct, this should make sense, else we get giberish.
        self._required_responses_auto_baud = 3

        self._listen_thread = threading.Thread(target=self._listen)
        self._listen_thread.daemon = True

        self._update_firmware_thread = threading.Thread(target= self._updateFirmware)
        self._update_firmware_thread.daemon = True
        self.firmwareUpdateComplete.connect(self._onFirmwareUpdateComplete)

        # self._heatup_wait_start_time = time.time()
        self._heatup_state = False

        ## Queue for commands that need to be send. Used when command is sent when a print is active.
        # self._command_queue = queue.Queue()
        # self._printer_buffer = []

        self._write_requested = False
        self._is_printing = False
        self._is_paused = False

        ## Set when print is started in order to check running time.
        self._print_start_time = None
        self._print_start_time_100 = None

        ## Keep track where in the provided g-code the print is
        self._gcode_position = 0

        # List of gcode lines to be printed
        self._gcode = []

        # Check if endstops are ever pressed (used for first run)
        self._x_min_endstop_pressed = False
        self._y_min_endstop_pressed = False
        self._z_min_endstop_pressed = False

        self._x_max_endstop_pressed = False
        self._y_max_endstop_pressed = False
        self._z_max_endstop_pressed = False

        # In order to keep the connection alive we request the temperature every so often from a different extruder.
        # This index is the extruder we requested data from the last time.
        self._temperature_requested_extruder_index = 0

        self._current_z = 0

        self._updating_firmware = False

        self._firmware_file_name = None
        self._firmware_update_finished = False

        self._error_message = None
        self._error_code = 0

    onError = pyqtSignal()

    firmwareUpdateComplete = pyqtSignal()
    firmwareUpdateChange = pyqtSignal()

    endstopStateChanged = pyqtSignal(str ,bool, arguments = ["key","state"])

    def _setTargetBedTemperature(self, temperature):
        Logger.log("d", "Setting bed temperature to %s", temperature)
        self.sendCommand("M140 S%s" % temperature)

    def _setTargetHotendTemperature(self, index, temperature):
        if index == -1:
            index = self._current_hotend
        Logger.log("d", "Setting hotend %s temperature to %s", index, temperature)
        self.sendCommand("M104 T%s S%s" % (index, temperature))

    def _setTargetHotendTemperatureAndWait(self, index, temperature):
        if index == -1:
            index = self._current_hotend
        Logger.log("d", "Setting hotend %s temperature to %s", index, temperature)
        self.sendCommand("M109 T%s S%s" % (index, temperature))

    def _setHeadPosition(self, x, y , z, speed):
        self.sendCommand("G0 X%s Y%s Z%s F%s" % (x, y, z, speed))

    def _setHeadX(self, x, speed):
        self.sendCommand("G0 X%s F%s" % (x, speed))

    def _setHeadY(self, y, speed):
        self.sendCommand("G0 Y%s F%s" % (y, speed))

    def _setHeadZ(self, z, speed):
        self.sendCommand("G0 Z%s F%s" % (z, speed))

    def _homeHead(self):
        self.sendCommand("G28")

    def _homeX(self):
        self.sendCommand("G28 X")

    def _homeY(self):
        self.sendCommand("G28 Y")

    def _homeBed(self):
        self.sendCommand("G28 Z")

    ##  A name for the device.
    @pyqtProperty(str, constant = True)
    def name(self):
        return self.getName()

    ##  The address of the device.
    @pyqtProperty(str, constant = True)
    def address(self):
        return self._serial_port

    def startPrint(self):
        self.writeStarted.emit(self)
        gcode_list = getattr( Application.getInstance().getController().getScene(), "gcode_list")
        self._updateJobState("printing")
        self.printGCode(gcode_list)

    def _wipeNozzle(self):
        code = Application.getInstance().getGlobalContainerStack().getProperty("machine_wipe_gcode", "value")
        if not code:
            Logger.log("w", "This device doesn't support wiping")
            QMessageBox.critical(None, "Error wiping nozzle", "This device doesn't support wiping" )
            return
        code = code.replace("{material_wipe_temperature}", str(Application.getInstance().getGlobalContainerStack().getProperty("material_wipe_temperature", "value"))).split("\n")
        self.writeStarted.emit(self)
        self._updateJobState("printing")
        result=self.printGCode(code)

        if result == Error.PRINTER_BUSY:
            QMessageBox.critical(None, "Error wiping nozzle", "Printer is busy, aborting print" )

        if result == Error.PRINTER_NOT_CONNECTED:
            QMessageBox.critical(None, "Error wiping nozzle", "Printer is not connected  " )

    def _moveHead(self, x, y, z, speed):
        self.sendCommand("G91")
        self.sendCommand("G0 X%s Y%s Z%s F%s" % (x, y, z, speed))
        self.sendCommand("G90")

    def _extrude(self, e, speed):
        self.sendCommand("G91")
        self.sendCommand("G0 E%s F%s" % (e, speed))
        self.sendCommand("G90")

    def _setHotend(self, num):
        self.sendCommand("T%i" % num)

    ##  Start a print based on a g-code.
    #   \param gcode_list List with gcode (strings).
    def printGCode(self, gcode_list):
        result = Error.SUCCESS

        Logger.log("d", "Started printing g-code")
        if self._progress:
            self._error_message = Message(catalog.i18nc("@info:status", "Unable to start a new job because the printer is busy."))
            self._error_message.show()
            Logger.log("d", "Printer is busy, aborting print")
            self.writeError.emit(self)
            result = Error.PRINTER_BUSY
            return result

        if self._connection_state != ConnectionState.connected:
            self._error_message = Message(catalog.i18nc("@info:status", "Unable to start a new job because the printer is not connected."))
            self._error_message.show()
            Logger.log("d", "Printer is not connected, aborting print")
            self.writeError.emit(self)
            result = Error.PRINTER_NOT_CONNECTED
            return result

        self._gcode.clear()
        for layer in gcode_list:
            self._gcode.extend(layer.split("\n"))

        # Reset line number. If this is not done, first line is sometimes ignored
        self._gcode.insert(0, "M110")
        self._gcode_position = 0
        self._print_start_time_100 = None
        self._print_start_time = time.time()
        self.setTimeTotal(0)
        self.setTimeElapsed(0)
        self._printingStarted()

        # for i in range(0, 4):  # Push first 4 entries before accepting other inputs
        #    self._sendNextGcodeLine()

        self.writeFinished.emit(self)
        # Returning Error.SUCCESS here, currently is unused
        return result

    ## Called when print is starting
    def _printingStarted(self):
        Application.getInstance().preventComputerFromSleeping(True)
        self._is_printing = True

    ## Called when print is finished or cancelled
    def _printingStopped(self):
        Application.getInstance().preventComputerFromSleeping(False)
        self._is_printing = False
        self._is_paused = False
        self._updateJobState("ready")
        self.setTimeElapsed(0)
        self.setTimeTotal(0)

    ##  Get the serial port string of this connection.
    #   \return serial port
    def getSerialPort(self):
        return self._serial_port

    ##  Try to connect the serial. This simply starts the thread, which runs _connect.
    @pyqtSlot()
    def _connect(self):
        if not self._updating_firmware and not self._connect_thread.isAlive() and self._connection_state in [ConnectionState.closed, ConnectionState.error]:
            self._connect_thread.start()

    ##  Private function (threaded) that actually uploads the firmware.
    def _updateFirmware(self):
        Logger.log("d", "Attempting to update firmware")
        self._error_code = 0
        self.setProgress(0, 100)
        self._firmware_update_finished = False

        if self._connection_state != ConnectionState.closed:
            self.close()
        port = Application.getInstance().getGlobalContainerStack().getProperty("machine_port", "value")
        if port != "AUTO":
            self._serial_port = port
        else:
            self._detectSerialPort()
        hex_file = intelHex.readHex(self._firmware_file_name)

        if len(hex_file) == 0:
            Logger.log("e", "Unable to read provided hex file. Could not update firmware")
            self._updateFirmwareFailedMissingFirmware()
            return

        programmer = stk500v2.Stk500v2()
        programmer.progress_callback = self.setProgress

        try:
            programmer.connect(self._serial_port)
        except Exception:
            programmer.close()
            pass

        # Give programmer some time to connect. Might need more in some cases, but this worked in all tested cases.
        time.sleep(1)

        if not programmer.isConnected():
            Logger.log("e", "Unable to connect with serial. Could not update firmware")
            self._updateFirmwareFailedCommunicationError()
            return

        self._updating_firmware = True

        try:
            programmer.programChip(hex_file)
            self._updating_firmware = False
        except serial.SerialException as e:
            Logger.log("e", "SerialException while trying to update firmware: <%s>" %(repr(e)))
            self._updateFirmwareFailedIOError()
            return
        except Exception as e:
            Logger.log("e", "Exception while trying to update firmware: <%s>" %(repr(e)))
            self._updateFirmwareFailedUnknown()
            return
        programmer.close()

        self._updateFirmwareCompletedSucessfully()
        self._serial_port = None
        return

    ##  Private function which makes sure that firmware update process has failed by missing firmware
    def _updateFirmwareFailedMissingFirmware(self):
        return self._updateFirmwareFailedCommon(4)

    ##  Private function which makes sure that firmware update process has failed by an IO error
    def _updateFirmwareFailedIOError(self):
        return self._updateFirmwareFailedCommon(3)

    ##  Private function which makes sure that firmware update process has failed by a communication problem
    def _updateFirmwareFailedCommunicationError(self):
        return self._updateFirmwareFailedCommon(2)

    ##  Private function which makes sure that firmware update process has failed by an unknown error
    def _updateFirmwareFailedUnknown(self):
        return self._updateFirmwareFailedCommon(1)

    ##  Private common function which makes sure that firmware update process has completed/ended with a set progress state
    def _updateFirmwareFailedCommon(self, code):
        if not code:
            raise Exception("Error code not set!")

        self._error_code = code

        self._firmware_update_finished = True
        self.resetFirmwareUpdate(update_has_finished = True)
        self.progressChanged.emit()
        self.firmwareUpdateComplete.emit()

        return

    ##  Private function which makes sure that firmware update process has successfully completed
    def _updateFirmwareCompletedSucessfully(self):
        self.setProgress(100, 100)
        self._firmware_update_finished = True
        self.resetFirmwareUpdate(update_has_finished = True)
        self.firmwareUpdateComplete.emit()

        return

    ##  Upload new firmware to machine
    #   \param filename full path of firmware file to be uploaded
    def updateFirmware(self, file_name):
        if self._autodetect_port:
            self._detectSerialPort()
        Logger.log("i", "Updating firmware of %s using %s", self._serial_port, file_name)
        self._firmware_file_name = file_name
        self._update_firmware_thread.start()

    @property
    def firmwareUpdateFinished(self):
        return self._firmware_update_finished

    def resetFirmwareUpdate(self, update_has_finished = False):
        self._firmware_update_finished = update_has_finished
        self.firmwareUpdateChange.emit()

    @pyqtSlot()
    def startPollEndstop(self):
        if not self._poll_endstop:
            self._poll_endstop = True
            if self._end_stop_thread is None:
                self._end_stop_thread = threading.Thread(target=self._pollEndStop)
                self._end_stop_thread.daemon = True
            self._end_stop_thread.start()

    @pyqtSlot()
    def stopPollEndstop(self):
        self._poll_endstop = False
        self._end_stop_thread = None

    def _pollEndStop(self):
        while self._connection_state == ConnectionState.connected and self._poll_endstop:
            self.sendCommand("M119")
            time.sleep(0.5)

    def _detectSerialPort(self):
        # Deferred import due to circular dependency
        from .USBPrinterOutputDeviceManager import USBPrinterOutputDeviceManager

        ports = USBPrinterOutputDeviceManager.getSerialPortList(True)
        for port in ports:
            programmer = stk500v2.Stk500v2()
            try:
                programmer.connect(port) # Connect with the serial, if this succeeds, it's an arduino based usb device.
                programmer.close()
                self._serial_port = port
                break
            except ispBase.IspError as e:
                Logger.log("i", "Could not establish connection on %s: %s. Device is not arduino based." %(port,str(e)))
            except Exception as e:
                Logger.log("i", "Could not establish connection on %s, unknown reasons.  Device is not arduino based." % port)

    ##  Private connect function run by thread. Can be started by calling connect.
    def _connect_thread_function(self):
        port = Application.getInstance().getGlobalContainerStack().getProperty("machine_port", "value")
        if port != "AUTO":
            self._serial_port = port
            self._autodetect_port = False

        Logger.log("d", "Attempting to connect to %s", self._serial_port)
        self.setConnectionState(ConnectionState.connecting)

        if self._autodetect_port:
            self.setConnectionText(catalog.i18nc("@info:status", "Scanning available serial ports for printers"))
            self._detectSerialPort()
            if self._serial_port == None:
                self.setConnectionText(catalog.i18nc("@info:status", "Failed to find a printer via USB"))
                return
        else:
            self.setConnectionText(catalog.i18nc("@info:status", "Connecting to USB device"))
        programmer = stk500v2.Stk500v2()
        try:
            programmer.connect(self._serial_port) # Connect with the serial, if this succeeds, it's an arduino based usb device.
            self._serial = programmer.leaveISP()
        except ispBase.IspError as e:
            programmer.close()
            Logger.log("i", "Could not establish connection on %s: %s. Device is not arduino based." %(self._serial_port,str(e)))
        except Exception as e:
            programmer.close()
            Logger.log("i", "Could not establish connection on %s, unknown reasons.  Device is not arduino based." % self._serial_port)

        baud_rate = Application.getInstance().getGlobalContainerStack().getProperty("machine_baudrate", "value")
        if baud_rate != "AUTO":
            self.setConnectionText(catalog.i18nc("@info:status", "Connecting"))
            Logger.log("d", "Attempting to connect to printer with serial %s on baud rate %s", self._serial_port, baud_rate)
            if self._serial is None:
                try:
                    self._serial = serial.Serial(str(self._serial_port), baud_rate, timeout=3, writeTimeout=10000)
                    time.sleep(10)
                except serial.SerialException:
                    Logger.log("d", "Could not open port %s" % self._serial_port)
            else:
                self.setBaudRate(baud_rate)

            time.sleep(1.5)
            timeout_time = time.time() + 5
            self._serial.write(b"\n")
            self._sendCommand("M105")
            while timeout_time > time.time():
                line = self._readline()
                if line is None:
                    Logger.log("d", "No response from serial connection received.")
                    # Something went wrong with reading, could be that close was called.
                    self.setConnectionState(ConnectionState.closed)
                    self.setConnectionText(catalog.i18nc("@info:status", "Connection to USB device failed"))
                    self._serial_port = None
                    return

                if b"T:" in line:
                    Logger.log("d", "Correct response for connection")
                    self._serial.timeout = 2  # Reset serial timeout
                    self.setConnectionState(ConnectionState.connected)
                    self.setConnectionText(catalog.i18nc("@info:status", "Connected via USB"))
                    self._listen_thread.start()  # Start listening
                    Logger.log("i", "Established printer connection on port %s" % self._serial_port)
                    if self._write_requested:
                        self.startPrint()
                    self._write_requested = False
                    return

        self.setConnectionText(catalog.i18nc("@info:status", "Autodetecting Baudrate"))
        # If the programmer connected, we know its an atmega based version.
        # Not all that useful, but it does give some debugging information.
        for baud_rate in self._getBaudrateList(): # Cycle all baud rates (auto detect)
            Logger.log("d", "Attempting to connect to printer with serial %s on baud rate %s", self._serial_port, baud_rate)
            if self._serial is None:
                try:
                    self._serial = serial.Serial(str(self._serial_port), baud_rate, timeout = 3, writeTimeout = 10000)
                    time.sleep(10)
                except serial.SerialException:
                    Logger.log("d", "Could not open port %s" % self._serial_port)
                    continue
            else:
                if not self.setBaudRate(baud_rate):
                    continue  # Could not set the baud rate, go to the next

            time.sleep(1.5) # Ensure that we are not talking to the bootloader. 1.5 seconds seems to be the magic number
            sucesfull_responses = 0
            timeout_time = time.time() + 5
            self._serial.write(b"\n")
            self._sendCommand("M105")  # Request temperature, as this should (if baudrate is correct) result in a command with "T:" in it
            while timeout_time > time.time():
                line = self._readline()
                if line is None:
                    Logger.log("d", "No response from serial connection received.")
                    # Something went wrong with reading, could be that close was called.
                    self.setConnectionState(ConnectionState.closed)
                    self.setConnectionText(catalog.i18nc("@info:status", "Connection to USB device failed"))
                    self._serial_port = None
                    return

                if b"T:" in line:
                    Logger.log("d", "Correct response for auto-baudrate detection received.")
                    self._serial.timeout = 0.5
                    sucesfull_responses += 1
                    if sucesfull_responses >= self._required_responses_auto_baud:
                        self._serial.timeout = 2 # Reset serial timeout
                        self.setConnectionState(ConnectionState.connected)
                        self.setConnectionText(catalog.i18nc("@info:status", "Connected via USB"))
                        self._listen_thread.start()  # Start listening
                        Logger.log("i", "Established printer connection on port %s" % self._serial_port)
                        if self._write_requested:
                            self.startPrint()
                        self._write_requested = False
                        return

                self.sendCommand("M105")  # Send M105 as long as we are listening, otherwise we end up in an undefined state

        Logger.log("e", "Baud rate detection for %s failed", self._serial_port)
        self.close()  # Unable to connect, wrap up.
        self.setConnectionState(ConnectionState.closed)
        self.setConnectionText(catalog.i18nc("@info:status", "Baud rate detection failed"))
        self._serial_port = None

    ##  Set the baud rate of the serial. This can cause exceptions, but we simply want to ignore those.
    def setBaudRate(self, baud_rate):
        try:
            self._serial.baudrate = baud_rate
            return True
        except Exception as e:
            return False

    ##  Close the printer connection
    def _close(self):
        Logger.log("d", "Closing the USB printer connection.")
        self._printingStopped()
        if self._connect_thread.isAlive():
            try:
                # TODO: to avoid waiting indefinitely, notify the thread that it needs
                # to return immediatly.
                self._connect_thread.join()
            except Exception as e:
                Logger.log("d", "PrinterConnection.close: %s (expected)", e)
                pass # This should work, but it does fail sometimes for some reason

        self._connect_thread = threading.Thread(target = self._connect_thread_function)
        self._connect_thread.daemon = True

        self.setConnectionState(ConnectionState.closed)
        self.setConnectionText(catalog.i18nc("@info:status", "Connection closed"))
        if self._serial is not None:
            try:
                self._listen_thread.join()
            except:
                pass
            self._serial.close()

        self._listen_thread = threading.Thread(target = self._listen)
        self._listen_thread.daemon = True
        self._serial = None
        self._serial_port = None
      # while not self._command_queue.empty():
      #     self._command_queue.get()
      # self._printer_buffer.clear()
        self._is_printing = False
        self._is_paused = False

    def _isInvalidCommand(self, cmd):
        """Sending a M109 or M190 with a temperature of zero may lead to an indefinite wait"""
        if cmd.startswith("M109") or cmd.startswith("M190"):
            search = re.search("[RS](-?[0-9\.]+)", cmd)
            return True if search and int(search.group(1)) == 0 else False

    def _serialWriteWithRetry(self, data):
        """Writes data to the serial port, but retries once if a serial timeout occurs"""
        try:
            self._serial.write(data)
        except serial.SerialTimeoutException:
            Logger.log("w","Serial timeout while writing to serial port, trying again.")
            time.sleep(0.5)
            self._serial.write(data)

    ##  Directly send the command, withouth checking connection state (eg; printing).
    #   \param cmd string with g-code
    def _sendCommand(self, cmd):
        if self._serial is None:
            return

        try:
            command = (cmd + "\n").encode()
            self._serial.write(b"\n")
            self._serial.write(command)
        except serial.SerialTimeoutException:
            Logger.log("w","Serial timeout while writing to serial port, trying again.")
            try:
                time.sleep(0.5)
                self._serial.write((cmd + "\n").encode())
            except Exception as e:
                Logger.log("e","Unexpected error while writing serial port %s " % e)
                self._setErrorState("Unexpected error while writing serial port %s " % e)
                self.close()
        except Exception as e:
            Logger.log("e","Unexpected error while writing serial port %s" % e)
            self._setErrorState("Unexpected error while writing serial port %s " % e)
            self.close()

    ##  Send a command to printer.
    #   \param cmd string with g-code
    @pyqtSlot(str)
    def sendCommand(self, cmd):
      # if "M108" in cmd:
      #     self._sendCommand(cmd)
      # self._command_queue.put(cmd)
        if self._serial is None or self._isInvalidCommand(cmd):
            return
        try:
            self._serialWriteWithRetry(("\n" + cmd + "\n").encode())
        except Exception as e:
            Logger.log("e","Unexpected error while writing serial port %s" % e)
            self._setErrorState("Unexpected error while writing serial port %s " % e)
            self.close()

    ##  Set the error state with a message.
    #   \param error String with the error message.
    def _setErrorState(self, error):
        self._updateJobState("error")
        self._error_state = error
        self.onError.emit()

    ##  Request the current scene to be sent to a USB-connected printer.
    #
    #   \param nodes A collection of scene nodes to send. This is ignored.
    #   \param file_name \type{string} A suggestion for a file name to write.
    #   This is ignored.
    #   \param filter_by_machine Whether to filter MIME types by machine. This
    #   is ignored.
    #   \param kwargs Keyword arguments.
    def requestWrite(self, nodes, file_name = None, filter_by_machine = False, file_handler = None, **kwargs):
        container_stack = Application.getInstance().getGlobalContainerStack()
        if container_stack.getProperty("machine_gcode_flavor", "value") == "UltiGCode":
            self._error_message = Message(catalog.i18nc("@info:status", "This printer does not support USB printing because it uses UltiGCode flavor."))
            self._error_message.show()
            return
        elif not container_stack.getMetaDataEntry("supports_usb_connection"):
            self._error_message = Message(catalog.i18nc("@info:status", "Unable to start a new job because the printer does not support usb printing."))
            self._error_message.show()
            return

        Application.getInstance().showPrintMonitor.emit(True)
        if self._connection_state == ConnectionState.connected:
            self.startPrint()
        elif self._connection_state == ConnectionState.closed:
            self._write_requested = True
            self.close()
            self.connect()
        else:
            self._write_requested = True

    def _setEndstopState(self, endstop_key, value):
        if endstop_key == b"x_min":
            if self._x_min_endstop_pressed != value:
                self.endstopStateChanged.emit("x_min", value)
            self._x_min_endstop_pressed = value
        elif endstop_key == b"y_min":
            if self._y_min_endstop_pressed != value:
                self.endstopStateChanged.emit("y_min", value)
            self._y_min_endstop_pressed = value
        elif endstop_key == b"z_min":
            if self._z_min_endstop_pressed != value:
                self.endstopStateChanged.emit("z_min", value)
            self._z_min_endstop_pressed = value

    messageFromPrinter = pyqtSignal(str)
    errorFromPrinter = pyqtSignal(str)

    ##  Listen thread function.
    def _listen(self):
        Logger.log("i", "Printer connection listen thread started for %s" % self._serial_port)

        # Wrap a MarlinSerialProtocol object around the serial port
        # for serial error correction.
        try:
            if DEBUG_SERIAL_RETRY:
                # Corrupt some fraction of the GCODEs checksum so that we can exercise the
                # error correction code.
                noisy = NoisySerialConnection(self._serial)
                noisy.setWriteErrorRate(1,150)
                log = LoggingSerialConnection(noisy, "gcode.log")
                self._serial_buffered = MarlinSerialProtocol(log)
            else:
                self._serial_buffered = MarlinSerialProtocol(self._serial)
        except Exception as e:
            Logger.log("e","Unexpected error while initialing MarlinSerialProtocol %s" % e)
            self._setErrorState("Unexpected error while initialing MarlinSerialProtocol %s " % e)
            self.close()

        temperature_request_timeout = time.time()
        #ok_timeout = time.time()
        while self._connection_state == ConnectionState.connected:

            try:
                if self._is_printing and self._serial_buffered.clearToSend():
                    self._sendNextGcodeLine()
                line = self._serial_buffered.readline()
            except Exception as e:
                Logger.log("e","Unexpected error while accessing serial port %s" % e)
                self._setErrorState("Unexpected error while accessing serial port %s " % e)
                self.close()
                break

            if line is None:
                break  # None is only returned when something went wrong. Stop listening

            if b"PROBE FAIL CLEAN NOZZLE" in line:
               self.errorFromPrinter.emit( "Wipe nozzle failed." )
               Logger.log("d", "---------------PROBE FAIL CLEAN NOZZLE" )
               self._error_message = Message(catalog.i18nc("@info:status", "Wipe nozzle failed."))
               self._error_message.show()
               return

            if time.time() > temperature_request_timeout and not self._heatup_state:
                if self._num_extruders > 1:
                    self._temperature_requested_extruder_index = (self._temperature_requested_extruder_index + 1) % self._num_extruders
                    self.sendCommand("M105 T%d" % (self._temperature_requested_extruder_index))
                else:
                    self.sendCommand("M105")
                temperature_request_timeout = time.time() + 5

            if line.startswith(b"Error:"):
                #if b"PROBE FAIL CLEAN NOZZLE" in line:
                #   self._error_message = Message(catalog.i18nc("@info:status", "Wipe nozzle failed."))
                #   self._error_message.show()
                #   QMessageBox.critical(None, "Error wiping nozzle", "Probe fail clean nozzle"

                # Oh YEAH, consistency.
                # Marlin reports a MIN/MAX temp error as "Error:x\n: Extruder switched off. MAXTEMP triggered !\n"
                # But a bed temp error is reported as "Error: Temperature heated bed switched off. MAXTEMP triggered !!"
                # So we can have an extra newline in the most common case. Awesome work people.
                if re.match(b"Error:[0-9]\n", line):
                    line = line.rstrip() + self._readline()

                # Skip the communication errors, as those get corrected.
                if b"Extruder switched off" in line or b"Temperature heated bed switched off" in line or b"Something is wrong, please turn off the printer." in line:
                    if not self.hasError():
                        self._setErrorState(line[6:])

            elif b" T:" in line or line.startswith(b"T:"):  # Temperature message
                try:
                    self._setHotendTemperature(self._temperature_requested_extruder_index, float(re.search(b"T: *([0-9\.]*)", line).group(1)))
                except:
                    pass
                if b"B:" in line:  # Check if it's a bed temperature
                    try:
                        self._setBedTemperature(float(re.search(b"B: *([0-9\.]*)", line).group(1)))
                    except Exception as e:
                        pass
                #TODO: temperature changed callback
            elif b"_min" in line or b"_max" in line:
                tag, value = line.split(b":", 1)
                self._setEndstopState(tag,(b"H" in value or b"TRIGGERED" in value))
            if line not in [b"", b"ok\n"]:
                #self.messageFromPrinter.emit(line.decode("utf-8").replace("\n", ""))
                self.messageFromPrinter.emit(line.decode("latin-1").replace("\n", ""))

            # first_cmd = self._printer_buffer[0] if len(self._printer_buffer) > 0 else ""
            # if (first_cmd.startswith("M109") or first_cmd.startswith("M190")) and time.time() > self._heatup_wait_start_time + 600:
            #     line = b"ok"
            # elif line == b"" and time.time() > ok_timeout and len(self._printer_buffer) > 0:
            #     line = b"ok"  # Force a timeout (basically, send next command)

#             if b"ok" in line:
#                 if len(self._printer_buffer) > 0:
#                     self._printer_buffer.pop(0)
#                 if self._is_paused:
#                     line = b""  # Force getting temperature as keep alive
#                 elif self._is_printing:
#                     self._sendNextGcodeLine()
#
#             elif b"resend" in line.lower() or b"rs" in line:  # Because a resend can be asked with "resend" and "rs"
#                 self._printer_buffer.clear()
#                 while not self._command_queue.empty():
#                     self._command_queue.get()
#                 try:
#                     self._gcode_position = int(line.replace(b"N:",b" ").replace(b"N",b" ").replace(b":",b" ").split()[-1])
#                 except:
#                     if b"rs" in line:
#                         self._gcode_position = int(line.split()[1])
#                 if self._is_printing:
#                     for i in range(3):
#                         self._sendNextGcodeLine()
#
#             while not self._command_queue.empty() and \
#                     (len(self._printer_buffer) < 3):
#                 cmd = self._command_queue.get()
#                 if cmd.startswith("M109") or cmd.startswith("M190"):
#                     self._heatup_wait_start_time = time.time()
#
#                     search = re.search("R(-?[0-9\.]+)", cmd)
#                     if search is None:
#                         search = re.search("S(-?[0-9\.]+)", cmd)
#
#                     if search is not None and int(search.group(1)) == 0:
#                         cmd = None
#                 if cmd is not None:
#                     self._printer_buffer.append(cmd)
#                     self._sendCommand(cmd)
                    # ok_timeout = time.time() + 30
                    # if cmd.startswith("G28") or cmd.startswith("G29"):
                    #     ok_timeout = time.time() + 600
            # Request the temperature on comm timeout (every 2 seconds) when we are not printing.)
            if line == b"":
                if self._num_extruders > 1:
                    self._temperature_requested_extruder_index = (self._temperature_requested_extruder_index + 1) % self._num_extruders
                    self.sendCommand("M105 T%d" % self._temperature_requested_extruder_index)
                else:
                    self.sendCommand("M105")

        Logger.log("i", "Printer connection listen thread stopped for %s" % self._serial_port)

    ##  Send next Gcode in the gcode list
    def _sendNextGcodeLine(self):
        if self._gcode_position >= len(self._gcode):
            return
        if self._gcode_position == 100:
            self._print_start_time_100 = time.time()
        if self._gcode_position % 100 == 0:
            elapsed = time.time() - self._print_start_time
            self.setTimeElapsed(elapsed)
            progress = self._gcode_position / len(self._gcode)
            if progress > 0:
                self.setTimeTotal(elapsed / progress)
        line = self._gcode[self._gcode_position]

        # if ";" in line:
        #     line = line[:line.find(";")]
        # line = line.strip()

        # Don't send empty lines. But we do have to send something, so send
        # m105 instead.
        # Don't send the M0 or M1 to the machine, as M0 and M1 are handled as
        # an LCD menu pause.
        # if line == "":
        #     line = "M105"
        try:
            if line == "M0" or line == "M1":
                self._setJobState("pause")
                line = "M105"  # Don't send the M0 or M1 to the machine, as M0 and M1 are handled as an LCD menu pause.
            if ("G0" in line or "G1" in line) and "Z" in line:
                self._current_z = float(re.search("Z([-0-9\.]*)", line).group(1))
        except Exception as e:
            Logger.log("e", "Unexpected error with printer connection, could not parse current Z: %s: %s" % (e, line))
            self._setErrorState("Unexpected error: %s" %e)
      # checksum = functools.reduce(lambda x,y: x^y, map(ord, "N%d%s" % (self._gcode_position, line)))
      #
      # self.sendCommand("N%d%s*%d" % (self._gcode_position, line, checksum))
        self._serial_buffered.enqueueCommand(line)
        self._gcode_position += 1
        self.setProgress((self._gcode_position / len(self._gcode)) * 100)
        self.progressChanged.emit()

    ##  Set the state of the print.
    #   Sent from the print monitor
    def _setJobState(self, job_state):
        if job_state == "pause":
            self._pausePrint()
            self._is_paused = True
            self._updateJobState("paused")
        elif job_state == "print":
            self._resumePrint()
            self._is_paused = False
            self._updateJobState("printing")
        elif job_state == "abort":
            self.cancelPrint()

    def _pausePrint(self):
        if not self._is_printing or self._is_paused:
            return

        settings = Application.getInstance().getGlobalContainerStack()
        machine_width = settings.getProperty("machine_width", "value")
        machine_depth = settings.getProperty("machine_depth", "value")
        machine_height = settings.getProperty("machine_height", "value")

        start_gcode = settings.getProperty("machine_start_gcode", "value")
        start_gcode_lines = len(start_gcode.split("\n")) + 10
        parkX = machine_width - 10
        parkY = machine_depth - 10
        maxZ = machine_height - 10
        retract_amount = settings.getProperty("retraction_amount", "value")
        moveZ = 10.0

        Logger.log("d", "Pausing print")
        if self._gcode_position - 5 > start_gcode_lines:  # Substract 5 because of the marlin queue
            x = None
            y = None
            e = None
            f = None
            for i in range(self._gcode_position - 1, start_gcode_lines, -1):
                line = self._gcode[i]
                if ('G0' in line or 'G1' in line) and 'X' in line and x is None:
                    x = float(re.search('X(-?[0-9\.]*)', line).group(1))
                if ('G0' in line or 'G1' in line) and 'Y' in line and y is None:
                    y = float(re.search('Y(-?[0-9\.]*)', line).group(1))
                if ('G0' in line or 'G1' in line) and 'E' in line and e is None:
                    e = float(re.search('E(-?[0-9\.]*)', line).group(1))
                if ('G0' in line or 'G1' in line) and 'F' in line and f is None:
                    f = float(re.search('F(-?[0-9\.]*)', line).group(1))
                if x is not None and y is not None and f is not None and e is not None:
                    break
            if f is None:
                f = 1200

            if x is not None and y is not None:
                # Set E relative positioning
                self.sendCommand("M83")

                # Retract 1mm
                retract = ("E-%f" % retract_amount)

                # Move the toolhead up
                newZ = self._current_z + moveZ
                if maxZ < newZ:
                    newZ = maxZ

                if newZ > self._current_z:
                    move = ("Z%f " % newZ)
                else:  # No z movement, too close to max height
                    move = ""
                retract_and_move = "G1 {} {}F120".format(retract, move)
                self.sendCommand(retract_and_move)

                # Move the head away
                self.sendCommand("G1 X%f Y%f F9000" % (parkX, parkY))

                # Disable the E steppers
                self.sendCommand("M84 E0")
                # Set E absolute positioning
                self.sendCommand("M82")

                self._pausePosition = (x, y, self._current_z, f, e)

    def _resumePrint(self):
        if not self._is_printing or not self._is_paused:
            return
        if self._pausePosition:
            settings = Application.getInstance().getGlobalContainerStack()
            retract_amount = settings.getProperty("retraction_amount", "value")
            # Set E relative positioning
            self.sendCommand("M83")

            # Prime the nozzle when changing filament
            self.sendCommand("G1 E%f F120" % retract_amount)  # Push the filament out
            self.sendCommand("G1 E-%f F120" % retract_amount)  # retract again

            # Position the toolhead to the correct position again
            self.sendCommand("G1 X%f Y%f Z%f F%d" % self._pausePosition[0:4])

            # Prime the nozzle again
            self.sendCommand("G1 E%f F120" % retract_amount)
            # Set proper feedrate
            self.sendCommand("G1 F%d" % (self._pausePosition[3]))
            # Set E absolute position to cancel out any extrude/retract that occured
            self.sendCommand("G92 E%f" % (self._pausePosition[4]))
            # Set E absolute positioning
            self.sendCommand("M82")
        Logger.log("d", "Print resumed")
        self._pausePosition = None


    ##  Set the progress of the print.
    #   It will be normalized (based on max_progress) to range 0 - 100
    def setProgress(self, progress, max_progress = 100):
        self._progress = (progress / max_progress) * 100  # Convert to scale of 0-100
        if self._progress == 100:
            # Printing is done, reset progress
            self._gcode_position = 0
            self.setProgress(0)
            self._printingStopped()
        self.progressChanged.emit()

    ##  Cancel the current print. Printer connection wil continue to listen.
    def cancelPrint(self):
        self._printingStopped()
        self._gcode_position = 0
        self.setProgress(0)
        self._gcode = []

      # while not self._command_queue.empty():
      #     self._command_queue.get()
      # self._printer_buffer.clear()

        # Turn off temperatures, fan and steppers
        self.sendCommand("M140 S0")
        self.sendCommand("M104 S0")
        self.sendCommand("M107")
        self.sendCommand("M84")
        Application.getInstance().showPrintMonitor.emit(False)

    ##  Check if the process did not encounter an error yet.
    def hasError(self):
        return self._error_state is not None

    ##  private read line used by printer connection to listen for data on serial port.
    def _readline(self):
        if self._serial is None:
            return None
        try:
            ret = self._serial.readline()
        except Exception as e:
            Logger.log("e", "Unexpected error while reading serial port. %s" % e)
            self._setErrorState("Printer has been disconnected")
            self.close()
            return None
        return ret

    ##  Create a list of baud rates at which we can communicate.
    #   \return list of int
    def _getBaudrateList(self):
        ret = [115200, 250000, 230400, 57600, 38400, 19200, 9600]
        return ret

    def _onFirmwareUpdateComplete(self):
        self._update_firmware_thread.join()
        self._update_firmware_thread = threading.Thread(target = self._updateFirmware)
        self._update_firmware_thread.daemon = True

        self.connect()

    ##  Pre-heats the heated bed of the printer, if it has one.
    #
    #   \param temperature The temperature to heat the bed to, in degrees
    #   Celsius.
    #   \param duration How long the bed should stay warm, in seconds. This is
    #   ignored because there is no g-code to set this.
    @pyqtSlot(float, float)
    def preheatBed(self, temperature, duration):
        Logger.log("i", "Pre-heating the bed to %i degrees.", temperature)
        self._setTargetBedTemperature(temperature)
        self.preheatBedRemainingTimeChanged.emit()

    ##  Cancels pre-heating the heated bed of the printer.
    #
    #   If the bed is not pre-heated, nothing happens.
    @pyqtSlot()
    def cancelPreheatBed(self):
        Logger.log("i", "Cancelling pre-heating of the bed.")
        self._setTargetBedTemperature(0)
        self.preheatBedRemainingTimeChanged.emit()
