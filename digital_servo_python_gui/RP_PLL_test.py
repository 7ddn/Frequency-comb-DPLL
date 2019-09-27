from PyQt5 import QtGui, Qt, QtCore, QtWidgets
import sys
import os
import time
import struct
import numpy as np
import pytest

from AsyncSocketComms import AsyncSocketServer
from AsyncSocketComms import AsyncSocketClient

import RP_PLL

class MonitorTCP_mock():

    invalid_read = -1111 # default value if memory has not been written to before


    def __init__(self):
        self.reply_latency = 0

        self.memory_buffer = bytearray(RP_PLL.RP_PLL_device.MAX_SAMPLES_READ_BUFFER)
        self.regs = {}
        self.data_to_send_back = bytearray()


        self.magic_bytes_to_handler = {
            RP_PLL.RP_PLL_device.MAGIC_BYTES_WRITE_REG: self.write_reg_handler,
            RP_PLL.RP_PLL_device.MAGIC_BYTES_READ_REG: self.read_reg_handler,
            RP_PLL.RP_PLL_device.MAGIC_BYTES_READ_BUFFER: self.read_buf_handler,
        }

    def parse_buffer(self, data_buffer):
        # parse the buffer, similar to what monitor-tcp does.
        # This needs to consume the buffer, otherwise the data will just accumulate.

        if len(data_buffer) < 4:
            return None

        (magic_bytes,) = struct.unpack('I', data_buffer[:4])
        handler_func = self.magic_bytes_to_handler.get(magic_bytes, None)
        if handler_func is None:
            print("Error: unrecognized magic_bytes 0x%x" % magic_bytes)
        else:
            (data_to_send_back, bytes_consumed_from_buffer) = handler_func(data_buffer)
            print("parse_buffer(): data_to_send_back=%s, bytes_consumed_from_buffer=%d" % (repr(data_to_send_back), bytes_consumed_from_buffer))
            data_buffer[0:bytes_consumed_from_buffer] = bytearray()
            return data_to_send_back

    # Handlers return a tuple containing: (data_to_send_back, bytes_consumed_from_buffer)
    def write_reg_handler(self, data_buffer):
        words_consumed = 3
        bytes_per_word = 4
        bytes_consumed = words_consumed*bytes_per_word

        # Do we have all the required information yet to handle the request?
        if len(data_buffer) < bytes_consumed:
            return (None, 0)

        (magic_bytes, addr, data) = struct.unpack('=III', data_buffer[:bytes_consumed])
        self.regs[int(addr/4)] = data
        return (None, bytes_consumed)

    def read_reg_handler(self, data_buffer):
        words_consumed = 3
        bytes_per_word = 4
        bytes_consumed = words_consumed*bytes_per_word

        # Do we have all the required information yet to handle the request?
        if len(data_buffer) < bytes_consumed:
            return (None, 0)

        (magic_bytes, addr, reserved) = struct.unpack('=III', data_buffer[:bytes_consumed])

        try:
            data = self.regs[int(addr/4)]
        except KeyError:
            data = self.invalid_read

        bytes_to_send = struct.pack('=i', data)  # signed or unsigned doesn't matter here


        return (bytes_to_send, bytes_consumed)

    def read_buf_handler(self, data_buffer):
        words_consumed = 3
        bytes_per_word = 4
        bytes_consumed = words_consumed*bytes_per_word

        # Do we have all the required information yet to handle the request?
        if len(data_buffer) < bytes_consumed:
            return (None, 0)

        return (self.memory_buffer, bytes_consumed)

    # Removes data from the start of a bytearray and returns it
    def remove_from_queue(self, data_array, bytes_to_remove):
        if len(data_array) < bytes_to_remove:
            raise Exception("Not enough bytes in buffer.")
        data = data_array[:bytes_to_remove]
        data_array[:] = bytearray()
        return data

    # Add data to the end of a bytearray:
    def add_to_queue(self, data_array, bytes_to_add):
        if bytes_to_add is not None:
            data_array += bytes_to_add

    # send/read Interface to mock without using a socket
    def send_mock(self, packet_to_send):
        packet_to_send = bytearray(packet_to_send) # need bytearray since it is mutable, as opposed to bytes()
        print("packet_to_send=%s, type=%s" % (repr(packet_to_send), type(packet_to_send)))
        data_to_send_back = self.parse_buffer(packet_to_send)
        len2 = lambda x: 0 if x is None else len(x)
        print("send_mock(), before: size=%d, to add: %d" % (len2(self.data_to_send_back), len2(data_to_send_back)))
        self.add_to_queue(self.data_to_send_back, data_to_send_back)
        print("send_mock(), after: size=%d" % len2(self.data_to_send_back))

    def read_mock(self, bytes_to_read):
        return self.remove_from_queue(self.data_to_send_back, bytes_to_read)
        
class ServerThread(QtCore.QThread):
    statusUpdate = QtCore.pyqtSignal(str)
    dataReceived = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super(ServerThread, self).__init__(parent)

        self.statusUpdate.emit("thread started")
        print("ServerThread::__init__()")

        self.server = AsyncSocketServer(port_number=5000, bStartListening=False)
        self.monitor_tcp = MonitorTCP_mock()
        self.bReadEnable = False
        self.server.bVerbose = False

    def run(self):
        print("ServerThread::run()")

        while 1:
            print(".", end="")
            time.sleep(100e-3)

            if not self.bReadEnable:
                continue

            self.server.run(bParseBufferIntoLines=False)

            data_to_send_back = self.monitor_tcp.parse_buffer(self.server.read_buffer)

            # reply after the prescribed delay
            if data_to_send_back is not None:
                print("run(): data_to_send_back = %s" % repr(data_to_send_back))
                time.sleep(self.monitor_tcp.reply_latency)
                self.server.sock_conn.sendall(data_to_send_back)

    @QtCore.pyqtSlot()
    def startListening(self):
        self.server.startListening()

    @QtCore.pyqtSlot()
    def closeConnection(self):
        self.server.sock_conn = None


    @QtCore.pyqtSlot(float)
    def setReplyLatency(self, latency_in_seconds):
        self.monitor_tcp.reply_latency = latency_in_seconds



    @QtCore.pyqtSlot(bool)
    def setReadLoopState(self, bReadEnable):
        self.bReadEnable = bReadEnable


def start_qt():
    # Start Qt:
    app = QtCore.QCoreApplication.instance()
    if app is None:
        # print("QCoreApplication not running yet. creating.")
        bEventLoopWasRunningAlready = False
        app = QtWidgets.QApplication(sys.argv)
    else:
        bEventLoopWasRunningAlready = True
        # print("QCoreApplication already running.")

    return app

class objSocketMock(QtCore.QObject):
    startListening = QtCore.pyqtSignal()
    closeConnection = QtCore.pyqtSignal()
    setReplyLatency = QtCore.pyqtSignal(float)
    setReadLoopState = QtCore.pyqtSignal(bool)

    statusUpdate = QtCore.pyqtSignal(str)
    dataReceived = QtCore.pyqtSignal(str)

    @QtCore.pyqtSlot()
    def statusUpdate(self, strUpdate):
        # could do something else with this...
        print(strUpdate)

    def __init__(self):
        super().__init__()

        # create thread object
        self.thread = ServerThread()
        # connect signals to slots in new thread
        self.startListening.connect(self.thread.startListening)
        self.closeConnection.connect(self.thread.closeConnection)
        self.setReplyLatency.connect(self.thread.setReplyLatency)
        self.setReadLoopState.connect(self.thread.setReadLoopState)

        self.startListening.emit()
        self.setReadLoopState.emit(True)

        self.thread.start()

    def timerQuit(self):
        print("timer")
        self.thread.exit()
        QtCore.QCoreApplication.instance().quit()
        raise "timerQuit(): Test took too long."

def check_readreg(dev):
    reg_in = 10; reg_addr = 100*4; reg_out = 0
    dev.write_Zynq_register_uint32(address_uint32=reg_addr, data_uint32=reg_in)
    reg_out = dev.read_Zynq_register_uint32(address_uint32=reg_addr)
    assert reg_in == reg_out
    assert dev.read_Zynq_register_int32(address_uint32=2334234+2) == MonitorTCP_mock.invalid_read

# this essentially tests our MonitorTCP_mock class
@pytest.mark.skip(reason="just for speed, remove this skip later")
def test_monitorTCP_mock():
    dev = RP_PLL.RP_PLL_device()
    # connect mocks
    monitor_tcp = MonitorTCP_mock()
    dev.send = monitor_tcp.send_mock
    dev.read = monitor_tcp.read_mock

    # actual test
    check_readreg(dev)

def test1():
    app = start_qt()

    server_thread = objSocketMock()
    time.sleep(0.1)

    timerMaxTestDuration = QtCore.QTimer()
    timerMaxTestDuration.singleShot(1000, server_thread.timerQuit)

    dev = RP_PLL.RP_PLL_device()

    dev.OpenTCPConnection(HOST="127.0.0.1")
    assert dev.valid_socket

    # actual test
    server_thread.setReplyLatency.emit(1.0)
    check_readreg(dev)

@pytest.mark.skip(reason="used only while developping tests")
def test_socket():
    app = start_qt()

    server_thread = objSocketMock()

    timer = QtCore.QTimer()
    timer.singleShot(3000, server_thread.timerQuit)

    client = AsyncSocketClient(5000)
    print("client connected.")

    for k in range(10):
        client.send_text('test')
        print("sent test")
        time.sleep(0.1)

    app.exec_()
    assert(0)

if __name__ == '__main__':
    # server = ServerThread()
    # server.startListening()
    # server.bReadEnable = True
    # server.server.bVerbose = False
    # server.run()

    # test_socketMock()
    test1()

    pass

