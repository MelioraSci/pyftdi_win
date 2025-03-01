# Copyright (C) 2010-2020 Emmanuel Blot <emmanuel.blot@free.fr>
# Copyright (c) 2016 Emmanuel Bouaziz <ebouaziz@free.fr>
# Copyright (c) 2023 Meliora Scientific Inc <info@meliorasci.com>
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#     * Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#     * Neither the name of the Neotion nor the names of its contributors may
#       be used to endorse or promote products derived from this software
#       without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL NEOTION BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA,
# OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
# LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
# NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE,
# EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

"""FTDI core driver."""

from binascii import hexlify
from collections import OrderedDict
from enum import IntEnum, unique
from errno import ENODEV
from logging import getLogger, DEBUG
from struct import unpack as sunpack
from sys import platform
from typing import Callable, Optional, List, Sequence, TextIO, Tuple, Union
from .misc import to_bool, to_int
from ctypes import c_uint32, c_uint16, c_uint8, c_ulong, c_void_p, c_bool, c_char, c_char_p, create_string_buffer, byref
import ctypes.util
from urllib.parse import SplitResult, urlsplit, urlunsplit

#pylint: disable-msg=invalid-name
#pylint: disable-msg=too-many-arguments
#pylint: disable=too-many-arguments
#pylint: disable=too-many-branches
#pylint: disable=too-many-statements
#pylint: disable=too-many-nested-blocks
#pylint: disable=too-many-instance-attributes
#pylint: disable=too-many-nested-blocks
#pylint: disable=too-many-public-methods
#pylint: disable=too-many-locals
#pylint: disable=too-many-lines


class FtdiLibraryNotFoundException(OSError):
    pass

class FtdiError(IOError):
    """Base class error for all FTDI device"""


class FtdiFeatureError(FtdiError):
    """Requested feature is not available on FTDI device"""


class FtdiMpsseError(FtdiFeatureError):
    """MPSSE mode not supported on FTDI device"""


class FtdiEepromError(FtdiError):
    """FTDI EEPROM access errors"""


class FtdiDeviceListInfoNode(ctypes.Structure):
    _fields_ = [("Flags", c_ulong),
                ("Type", c_ulong),
                ("ID", c_ulong),
                ("LocId", c_ulong),
                ("SerialNumber", c_char * 16),
                ("Description", c_char * 64),
                ("ftHandle", c_void_p)]


class Ftdi:
    """FTDI device driver"""

    SCHEME = 'ftdi'
    """URL scheme for :py:class:`UsbTools`."""

    FTDI_VENDOR = 0x403
    """USB VID for FTDI chips."""

    VENDOR_IDS = {'ftdi': FTDI_VENDOR}
    """Supported vendors, only FTDI.
       To add third parties vendors see :py:meth:`add_custom_vendor`.
    """

    PRODUCT_IDS = {
        FTDI_VENDOR: OrderedDict((
            # use an ordered dict so that the first occurence of a PID takes
            # precedence when generating URLs - order does matter.
            ('232', 0x6001),
            ('232r', 0x6001),
            ('232h', 0x6014),
            ('2232', 0x6010),
            ('2232c', 0x6010),
            ('2232d', 0x6010),
            ('2232h', 0x6010),
            ('4232', 0x6011),
            ('4232h', 0x6011),
            ('ft-x', 0x6015),
            ('230x', 0x6015),
            ('231x', 0x6015),
            ('234x', 0x6015),
            ('ft232', 0x6001),
            ('ft232r', 0x6001),
            ('ft232h', 0x6014),
            ('ft2232', 0x6010),
            ('ft2232c', 0x6010),
            ('ft2232d', 0x6010),
            ('ft2232h', 0x6010),
            ('ft4232', 0x6011),
            ('ft4232h', 0x6011),
            ('ft230x', 0x6015),
            ('ft231x', 0x6015),
            ('ft234x', 0x6015)))
        }
    """Supported products, only FTDI officials ones.
       To add third parties and customized products, see
       :py:meth:`add_custom_product`.
    """

    DEFAULT_VENDOR = FTDI_VENDOR
    """Default vendor: FTDI."""

    DEVICE_NAMES = {
        0x0200: 'ft232am',
        0x0400: 'ft232bm',
        0x0500: 'ft2232c',
        0x0600: 'ft232r',
        0x0700: 'ft2232h',
        0x0800: 'ft4232h',
        0x0900: 'ft232h',
        0x1000: 'ft-x'}
    """Common names of FTDI supported devices."""

    PORT_COUNTS = {
        0x0200: 1,
        0x0400: 1,
        0x0500: 2,
        0x0600: 1,
        0x0700: 2,
        0x0800: 4,
        0x0900: 1,
        0x1000: 1}
    """Port counts of FTDI supported devices."""

    # Note that the FTDI datasheets contradict themselves, so
    # the following values may not be the right ones...
    FIFO_SIZES = {
        0x0200: (128, 128),    # FT232AM: TX: 128, RX: 128
        0x0400: (128, 384),    # FT232BM: TX: 128, RX: 384
        0x0500: (128, 384),    # FT2232C: TX: 128, RX: 384
        0x0600: (256, 128),    # FT232R:  TX: 256, RX: 128
        0x0700: (4096, 4096),  # FT2232H: TX: 4KiB, RX: 4KiB
        0x0800: (2048, 2048),  # FT4232H: TX: 2KiB, RX: 2KiB
        0x0900: (1024, 1024),  # FT232H:  TX: 1KiB, RX: 1KiB
        0x1000: (512, 512),    # FT-X:    TX: 512, RX: 512
    }
    """FTDI chip internal FIFO sizes

       Note that 'TX' and 'RX' are inverted with the datasheet terminology:
       Values here are seen from the host perspective, whereas datasheet
       values are defined from the device perspective
    """

    @unique
    class BitMode(IntEnum):
        """Function selection."""

        RESET = 0x00    # switch off altnerative mode (default to UART)
        BITBANG = 0x01  # classical asynchronous bitbang mode
        MPSSE = 0x02    # MPSSE mode, available on 2232x chips
        SYNCBB = 0x04   # synchronous bitbang mode
        MCU = 0x08      # MCU Host Bus Emulation mode,
        OPTO = 0x10     # Fast Opto-Isolated Serial Interface Mode
        CBUS = 0x20     # Bitbang on CBUS pins of R-type chips
        SYNCFF = 0x40   # Single Channel Synchronous FIFO mode

    # MPSSE Commands
    WRITE_BYTES_PVE_MSB = 0x10
    WRITE_BYTES_NVE_MSB = 0x11
    WRITE_BITS_PVE_MSB = 0x12
    WRITE_BITS_NVE_MSB = 0x13
    WRITE_BYTES_PVE_LSB = 0x18
    WRITE_BYTES_NVE_LSB = 0x19
    WRITE_BITS_PVE_LSB = 0x1a
    WRITE_BITS_NVE_LSB = 0x1b
    READ_BYTES_PVE_MSB = 0x20
    READ_BYTES_NVE_MSB = 0x24
    READ_BITS_PVE_MSB = 0x22
    READ_BITS_NVE_MSB = 0x26
    READ_BYTES_PVE_LSB = 0x28
    READ_BYTES_NVE_LSB = 0x2c
    READ_BITS_PVE_LSB = 0x2a
    READ_BITS_NVE_LSB = 0x2e
    RW_BYTES_PVE_NVE_MSB = 0x31
    RW_BYTES_NVE_PVE_MSB = 0x34
    RW_BITS_PVE_PVE_MSB = 0x32
    RW_BITS_PVE_NVE_MSB = 0x33
    RW_BITS_NVE_PVE_MSB = 0x36
    RW_BITS_NVE_NVE_MSB = 0x37
    RW_BYTES_PVE_NVE_LSB = 0x39
    RW_BYTES_NVE_PVE_LSB = 0x3c
    RW_BITS_PVE_PVE_LSB = 0x3a
    RW_BITS_PVE_NVE_LSB = 0x3b
    RW_BITS_NVE_PVE_LSB = 0x3e
    RW_BITS_NVE_NVE_LSB = 0x3f
    WRITE_BITS_TMS_PVE = 0x4a
    WRITE_BITS_TMS_NVE = 0x4b
    RW_BITS_TMS_PVE_PVE = 0x6a
    RW_BITS_TMS_PVE_NVE = 0x6b
    RW_BITS_TMS_NVE_PVE = 0x6e
    RW_BITS_TMS_NVE_NVE = 0x6f
    SEND_IMMEDIATE = 0x87
    WAIT_ON_HIGH = 0x88
    WAIT_ON_LOW = 0x89
    READ_SHORT = 0x90
    READ_EXTENDED = 0x91
    WRITE_SHORT = 0x92
    WRITE_EXTENDED = 0x93
    # -H series only
    DISABLE_CLK_DIV5 = 0x8a
    ENABLE_CLK_DIV5 = 0x8b

    # Modem status
    MODEM_CTS = (1 << 4)    # Clear to send
    MODEM_DSR = (1 << 5)    # Data set ready
    MODEM_RI = (1 << 6)     # Ring indicator
    MODEM_RLSD = (1 << 7)   # Carrier detect
    MODEM_DR = (1 << 8)     # Data ready
    MODEM_OE = (1 << 9)     # Overrun error
    MODEM_PE = (1 << 10)    # Parity error
    MODEM_FE = (1 << 11)    # Framing error
    MODEM_BI = (1 << 12)    # Break interrupt
    MODEM_THRE = (1 << 13)  # Transmitter holding register
    MODEM_TEMT = (1 << 14)  # Transmitter empty
    MODEM_RCVE = (1 << 15)  # Error in RCVR FIFO

    # FTDI MPSSE commands
    SET_BITS_LOW = 0x80     # Change LSB GPIO output
    SET_BITS_HIGH = 0x82    # Change MSB GPIO output
    GET_BITS_LOW = 0x81     # Get LSB GPIO output
    GET_BITS_HIGH = 0x83    # Get MSB GPIO output
    LOOPBACK_START = 0x84   # Enable loopback
    LOOPBACK_END = 0x85     # Disable loopback
    SET_TCK_DIVISOR = 0x86  # Set clock
    # -H series only
    ENABLE_CLK_3PHASE = 0x8c       # Enable 3-phase data clocking (I2C)
    DISABLE_CLK_3PHASE = 0x8d      # Disable 3-phase data clocking
    CLK_BITS_NO_DATA = 0x8e        # Allows JTAG clock to be output w/o data
    CLK_BYTES_NO_DATA = 0x8f       # Allows JTAG clock to be output w/o data
    CLK_WAIT_ON_HIGH = 0x94        # Clock until GPIOL1 is high
    CLK_WAIT_ON_LOW = 0x95         # Clock until GPIOL1 is low
    ENABLE_CLK_ADAPTIVE = 0x96     # Enable JTAG adaptive clock for ARM
    DISABLE_CLK_ADAPTIVE = 0x97    # Disable JTAG adaptive clock
    CLK_COUNT_WAIT_ON_HIGH = 0x9c  # Clock byte cycles until GPIOL1 is high
    CLK_COUNT_WAIT_ON_LOW = 0x9d   # Clock byte cycles until GPIOL1 is low
    # FT232H only
    DRIVE_ZERO = 0x9e       # Drive-zero mode

    # Reset arguments
    SIO_RESET_SIO = 0        # Reset device
    SIO_RESET_PURGE_RX = 1   # Drain USB RX buffer (host-to-ftdi)
    SIO_RESET_PURGE_TX = 2   # Drain USB TX buffer (ftdi-to-host)

    # Flow control arguments
    SIO_DISABLE_FLOW_CTRL = 0x0
    SIO_RTS_CTS_HS = (0x1 << 8)
    SIO_DTR_DSR_HS = (0x2 << 8)
    SIO_XON_XOFF_HS = (0x4 << 8)
    SIO_SET_DTR_MASK = 0x1
    SIO_SET_DTR_HIGH = (SIO_SET_DTR_MASK | (SIO_SET_DTR_MASK << 8))
    SIO_SET_DTR_LOW = (0x0 | (SIO_SET_DTR_MASK << 8))
    SIO_SET_RTS_MASK = 0x2
    SIO_SET_RTS_HIGH = (SIO_SET_RTS_MASK | (SIO_SET_RTS_MASK << 8))
    SIO_SET_RTS_LOW = (0x0 | (SIO_SET_RTS_MASK << 8))

    # Parity bits
    PARITY_NONE, PARITY_ODD, PARITY_EVEN, PARITY_MARK, PARITY_SPACE = range(5)
    # Number of stop bits
    STOP_BIT_1, STOP_BIT_15, STOP_BIT_2 = range(3)
    # Number of bits
    BITS_7, BITS_8 = [7+i for i in range(2)]
    # Break type
    BREAK_OFF, BREAK_ON = range(2)

    # cts:  Clear to send
    # dsr:  Data set ready
    # ri:   Ring indicator
    # dcd:  Data carrier detect
    # dr:   Data ready
    # oe:   Overrun error
    # pe:   Parity error
    # fe:   Framing error
    # bi:   Break interrupt
    # thre: Transmitter holding register empty
    # temt: Transmitter empty
    # err:  Error in RCVR FIFO
    MODEM_STATUS = [('', '', '', '', 'cts', 'dsr', 'ri', 'dcd'),
                    ('dr', 'overrun', 'parity', 'framing',
                     'break', 'thre', 'txe', 'rcve')]

    ERROR_BITS = (0x00, 0x8E)
    TX_EMPTY_BITS = 0x60

    # Clocks and baudrates
    BUS_CLOCK_BASE = 6.0E6  # 6 MHz
    BUS_CLOCK_HIGH = 30.0E6  # 30 MHz
    BAUDRATE_REF_BASE = int(3.0E6)  # 3 MHz
    BAUDRATE_REF_HIGH = int(12.0E6)  # 12 MHz
    BITBANG_BAUDRATE_RATIO_BASE = 16
    BITBANG_BAUDRATE_RATIO_HIGH = 5
    BAUDRATE_TOLERANCE = 3.0  # acceptable clock drift for UART, in %

    FRAC_DIV_CODE = (0, 3, 2, 4, 1, 5, 6, 7)

    # Latency
    LATENCY_MIN = 12
    LATENCY_MAX = 255
    LATENCY_EEPROM_FT232R = 77

    # EEPROM Properties
    EXT_EEPROM_SIZES = (128, 256) # in bytes (93C66 seen as 93C56)

    INT_EEPROMS = {
        0x0600: 0x80,  # FT232R: 128 bytes, 1024 bits
        0x1000: 0x400  # FT230*X: 1KiB
    }

    def __init__(self):
        self.log = getLogger('pyftdi.ftdi')
        self._debug_log = False
        self._usb_read_timeout = 5000
        self._usb_write_timeout = 5000
        self._baudrate = -1
        self._readbuffer = bytearray()
        self._readoffset = 0
        self._readbuffer_chunksize = 4 << 10  # 4KiB
        self._raw_readbuffer = (c_uint8 * self._readbuffer_chunksize)()
        self._writebuffer_chunksize = 4 << 10  # 4KiB
        self._max_packet_size = 0
        self._interface = None
        self._index = None
        self._in_ep = None
        self._out_ep = None
        self._bitmode = Ftdi.BitMode.RESET
        self._latency = 0
        self._latency_count = 0
        self._latency_min = self.LATENCY_MIN
        self._latency_max = self.LATENCY_MAX
        self._latency_threshold = None  # disable dynamic latency
        self._event_char = 0
        self._event_char_enabled = False
        self._error_char = 0
        self._error_char_enabled = False
        self._cbus_pins = (0, 0)
        self._cbus_out = 0
        self._tracer = None

    # --- Public API -------------------------------------------------------

    @classmethod
    def create_from_url(cls, url: str) -> 'Ftdi':
        """Create an Ftdi instance from an URL

           URL scheme: ftdi://[vendor[:product[:index|:serial]]]/interface

           :param url: FTDI device selector
           :return: a fresh, open Ftdi instance
        """
        device = Ftdi()
        device.open_from_url(url)
        return device

    @classmethod
    def list_devices(cls, url: Optional[str] = None) -> \
            List[Tuple[str, int]]:
        """List of URLs of connected FTDI devices.

           :param url: a pattern URL to restrict the search
           :return: list of (UsbDeviceDescriptor, interface)
        """
        d2xx = cls._load_backend()
        numDevices = ctypes.c_ulong()
        r = d2xx.FT_CreateDeviceInfoList(ctypes.byref(numDevices))
        if r != 0 or numDevices.value == 0:
            return []

        devInfo = (FtdiDeviceListInfoNode * numDevices.value)()
        r = d2xx.FT_GetDeviceInfoList(byref(devInfo), byref(numDevices))
        if r != 0:
            return []

        devices = []
        for i in range(numDevices.value):
            info = devInfo[i]
            if info.Type == 3:  # Unknown type
                continue

            serNum = str(info.SerialNumber, 'ascii')
            interface = '1'
            if serNum and (info.Type == 4 or info.Type == 6 or info.Type == 7):
                port = serNum[-1]
                serNum = serNum[:-1]
                interface = chr(ord(port) - ord('A') + ord('1'))

            devices.append("ftdi://::" + serNum + '/' + interface)

        return devices

    @classmethod
    def show_devices(cls, url: Optional[str] = None,
                     out: Optional[TextIO] = None) -> None:
        """Print the URLs and descriptors of connected FTDI devices.

           :param url: a pattern URL to restrict the search
           :param out: output stream, default to stdout
        """
        raise NotImplementedError()


    @classmethod
    def get_identifiers(cls, url: str) -> Tuple[str, int]:
        """Extract the identifiers of an FTDI device from URL, if any

           :param url: input URL to parse
        """

        urlparts = urlsplit(url)
        if urlparts.scheme != cls.SCHEME:
            raise FtdiError("Invalid URL: %s" % url)

        try:
            if not urlparts.path:
                raise FtdiError('URL string is missing device port')
            path = urlparts.path.strip('/')
            if path == '?' or (not path and urlstr.endswith('?')):
                return None

            interface = to_int(path)
        except (IndexError, ValueError):
            raise FtdiError('Invalid device URL: %s' % url)

        specifiers = urlparts.netloc.split(':')
        if len(specifiers) < 3:
            raise FtdiError('Invalid device URL: %s' % url)

        return specifiers[2], interface

    @classmethod
    def get_device(cls, url: str) -> str:
        """Get a USB device from its URL, without opening an instance.

           :param url: input URL to parse
           :return: the USB device that match the specified URL
        """
        devdesc, _ = cls.get_identifiers(url)
        return devdesc

    @classmethod
    def add_custom_vendor(cls, vid: int, vidname: str = '') -> None:
        """Add a custom USB vendor identifier.

           It can be useful to use a pretty URL for opening FTDI device

           :param vid: Vendor ID (USB 16-bit identifier)
           :param vidname: Vendor name (arbitrary string)
           :raise ValueError: if the vendor id is already referenced
        """
        pass

    @classmethod
    def add_custom_product(cls, vid: int, pid: int, pidname: str = '') -> None:
        """Add a custom USB product identifier.

           It is required for opening FTDI device with non-standard VID/PID
           USB identifiers.

           :param vid: Vendor ID (USB 16-bit identifier)
           :param pid: Product ID (USB 16-bit identifier)
           :param pidname: Product name (arbitrary string)
           :raise ValueError: if the product id is already referenced
        """
        pass

    @classmethod
    def decode_modem_status(cls, value: bytes, error_only: bool = False) -> \
            Tuple[str, ...]:
        """Decode the FTDI modem status bitfield into short strings.

           :param value: 2-byte modem status
           :param error_only: only decode error flags
           :return: a tuple of status identifiers
        """
        status = []
        for pos, (byte_, ebits) in enumerate(zip(value, cls.ERROR_BITS)):
            for bit, _ in enumerate(cls.MODEM_STATUS[pos]):
                if error_only:
                    byte_ &= ebits
                if byte_ & (1 << bit):
                    status.append(cls.MODEM_STATUS[pos][bit])
        return tuple(status)

    @staticmethod
    def find_all(vps: Sequence[Tuple[int, int]], nocache: bool = False) -> \
            List[Tuple[str, int]]:
        """Find all devices that match the vendor/product pairs of the vps
           list.

           :param vps: a sequence of 2-tuple (vid, pid) pairs
           :type vps: tuple(int, int)
           :param bool nocache: bypass cache to re-enumerate USB devices on
                                the host
           :return: a list of 5-tuple (vid, pid, sernum, iface, description)
                    device descriptors
           :rtype: list(tuple(int,int,str,int,str))
        """
        return []

    @property
    def is_connected(self) -> bool:
        """Tells whether this instance is connected to an actual USB slave.

           :return: the slave connection status
        """
        return bool(self._handle)

    def open_from_url(self, url: str) -> None:
        """Open a new interface to the specified FTDI device.

           :param str url: a FTDI URL selector
        """
        devdesc, interface = self.get_identifiers(url)
        self.open_from_device(devdesc, interface)

    def open(self, vendor: int, product: int, bus: Optional[int] = None,
             address: Optional[int] = None, index: int = 0,
             serial: Optional[str] = None,
             interface: int = 1)  -> None:
        """Open a new interface to the specified FTDI device.

           If several FTDI devices of the same kind (vid, pid) are connected
           to the host, either index or serial argument should be used to
           discriminate the FTDI device.

           index argument is not a reliable solution as the host may enumerate
           the USB device in random order. serial argument is more reliable
           selector and should always be prefered.

           Some FTDI devices support several interfaces/ports (such as FT2232H
           and FT4232H). The interface argument selects the FTDI port to use,
           starting from 1 (not 0).

           :param int vendor: USB vendor id
           :param int product: USB product id
           :param int bus: optional selector,  USB bus
           :param int address: optional selector, USB address on bus
           :param int index: optional selector, specified the n-th matching
                             FTDI enumerated USB device on the host
           :param str serial: optional selector, specified the FTDI device
                              by its serial number
           :param str interface: FTDI interface/port
        """
        self.open_from_device(serial, interface)

    def open_from_device(self, device: str,
                         interface: int = 1) -> None:
        """Open a new interface from an existing USB device.

           :param device: FTDI serial number string
           :param interface: FTDI interface to use (integer starting from 1)
        """
        self._base_serial_num = device
        self._index = interface

        self._d2xx = self._load_backend()

        sn = device
        sn += chr(ord('A') + interface - 1)

        self._handle = c_void_p()
        r = self._d2xx.FT_OpenEx(c_char_p(bytes(sn,'ascii')), 1, byref(self._handle))
        if r != 0:
            raise FtdiError(f'No such FTDI device found: {device}/{interface}')


        devType = c_ulong()
        devID = c_ulong()
        serNum = create_string_buffer(16)
        devDesc = create_string_buffer(64)
        r = self._d2xx.FT_GetDeviceInfo(self._handle, byref(devType), byref(devID), serNum, devDesc, c_void_p())
        if r != 0:
            raise FtdiError('Unable to retrieve info on FTDI device %s/%d' % device % interface)

        if devType.value == 0:
            self._devVersion = 0x0400
        elif devType.value == 1:
            self._devVersion = 0x0200
        elif devType.value == 4:
            self._devVersion = 0x0500
        elif devType.value == 5:
            self._devVersion = 0x0600
        elif devType.value == 6:
            self._devVersion = 0x0700
        elif devType.value == 7:
            self._devVersion = 0x0800
        elif devType.value == 8:
            self._devVersion = 0x0900
        elif devType.value == 9:
            self._devVersion = 0x1000
        else:
            self._devVersion = 0x0000


        r = self._d2xx.FT_SetUSBParameters(self._handle, c_uint32(65536), c_uint32(65536))
        if r != 0:
            raise FtdiError('Unable to configure transfer sizes for FTDI device %s/%d' % device % interface)

        self._update_timeouts()

        #self._set_interface(config, interface)
        self._max_packet_size = self._get_max_packet_size()
        # Invalidate data in the readbuffer
        self._readoffset = 0
        self._readbuffer = bytearray()
        # Drain input buffer
        self.purge_buffers()
        # Shallow reset
        self._reset_device()
        # Reset feature mode
        self.set_bitmode(0, Ftdi.BitMode.RESET)
        # Init latency
        self._latency_threshold = None
        self.set_latency_timer(self.LATENCY_MIN)
        self._debug_log = self.log.getEffectiveLevel() == DEBUG

    def close(self) -> None:
        """Close the FTDI interface/port."""
        if self._d2xx and self._handle:
            self._d2xx.FT_Close(self._handle)
            self._handle = None

    def reset(self, usb_reset: bool = False) -> None:
        """Reset FTDI device.

           :param usb_reset: wether to perform a full USB reset of the device.

           Beware that selecting usb_reset performs a full USB device reset,
           which means all other interfaces of the same device are also
           affected.
        """
        if not self.is_connected:
            raise FtdiError('Not connected')
        self._reset_device()
        if usb_reset:
            self._reset_usb_device()

    def open_mpsse_from_url(self, url: str, direction: int = 0x0,
                            initial: int = 0x0, frequency: float = 6.0E6,
                            latency: int = 16, debug: bool = False) -> float:
        """Open a new interface to the specified FTDI device in MPSSE mode.

           MPSSE enables I2C, SPI, JTAG or other synchronous serial interface
           modes (vs. UART mode).

           :param url: a FTDI URL selector
           :param direction: a bitfield specifying the FTDI GPIO direction,
                where high level defines an output, and low level defines an
                input
           :param initial: a bitfield specifying the initial output value
           :param float frequency: serial interface clock in Hz
           :param latency: low-level latency in milliseconds. The shorter
                the delay, the higher the host CPU load. Do not use shorter
                values than the default, as it triggers data loss in FTDI.
           :param debug: use a tracer to decode MPSSE protocol
           :return: actual bus frequency in Hz
        """
        devdesc, interface = self.get_identifiers(url)
        return self.open_mpsse_from_device(devdesc, interface,
                                           direction=direction,
                                           initial=initial,
                                           frequency=frequency,
                                           latency=latency,
                                           debug=debug)

    def open_mpsse(self, vendor: int, product: int, bus: Optional[int] = None,
                   address: Optional[int] = None, index: int = 0,
                   serial: Optional[str] = None, interface: int = 1,
                   direction: int = 0x0, initial: int = 0x0,
                   frequency: float = 6.0E6, latency: int = 16,
                   debug: bool = False) -> float:
        """Open a new interface to the specified FTDI device in MPSSE mode.

           MPSSE enables I2C, SPI, JTAG or other synchronous serial interface
           modes (vs. UART mode).

           If several FTDI devices of the same kind (vid, pid) are connected
           to the host, either index or serial argument should be used to
           discriminate the FTDI device.

           index argument is not a reliable solution as the host may enumerate
           the USB device in random order. serial argument is more reliable
           selector and should always be prefered.

           Some FTDI devices support several interfaces/ports (such as FT2232H
           and FT4232H). The interface argument selects the FTDI port to use,
           starting from 1 (not 0). Note that not all FTDI ports are MPSSE
           capable.

           :param vendor: USB vendor id
           :param product: USB product id
           :param bus: optional selector, USB bus
           :param address: optional selector, USB address on bus
           :param index: optional selector, specified the n-th matching
                             FTDI enumerated USB device on the host
           :param serial: optional selector, specified the FTDI device
                              by its serial number
           :param interface: FTDI interface/port
           :param direction: a bitfield specifying the FTDI GPIO direction,
                where high level defines an output, and low level defines an
                input
           :param initial: a bitfield specifying the initial output value
           :param frequency: serial interface clock in Hz
           :param latency: low-level latency in milliseconds. The shorter
                the delay, the higher the host CPU load. Do not use shorter
                values than the default, as it triggers data loss in FTDI.
           :param bool debug: use a tracer to decode MPSSE protocol
           :return: actual bus frequency in Hz
        """
        return self.open_mpsse_from_device(serial, interface,
                                           direction=direction,
                                           initial=initial,
                                           frequency=frequency,
                                           latency=latency,
                                           debug=debug)

    def open_mpsse_from_device(self, device: str,
                               interface: int = 1, direction: int = 0x0,
                               initial: int = 0x0, frequency: float = 6.0E6,
                               latency: int = 16, tracer: bool = False,
                               debug: bool = False) -> float:
        """Open a new interface to the specified FTDI device in MPSSE mode.

           MPSSE enables I2C, SPI, JTAG or other synchronous serial interface
           modes (vs. UART mode).

           If several FTDI devices of the same kind (vid, pid) are connected
           to the host, either index or serial argument should be used to
           discriminate the FTDI device.

           index argument is not a reliable solution as the host may enumerate
           the USB device in random order. serial argument is more reliable
           selector and should always be prefered.

           Some FTDI devices support several interfaces/ports (such as FT2232H
           and FT4232H). The interface argument selects the FTDI port to use,
           starting from 1 (not 0). Note that not all FTDI ports are MPSSE
           capable.

           :param device: FTDI USB device
           :param interface: FTDI interface/port
           :param direction: a bitfield specifying the FTDI GPIO direction,
                where high level defines an output, and low level defines an
                input
           :param initial: a bitfield specifying the initial output value
           :param frequency: serial interface clock in Hz
           :param latency: low-level latency in milliseconds. The shorter
                the delay, the higher the host CPU load. Do not use shorter
                values than the default, as it triggers data loss in FTDI.
           :param bool tracer: use a tracer to decode MPSSE protocol
           :param bool debug: add more debug traces
           :return: actual bus frequency in Hz
        """
        self.open_from_device(device, interface)
        if not self.is_mpsse_interface(interface):
            self.close()
            raise FtdiMpsseError('This interface does not support MPSSE')
        if to_bool(tracer):  # accept strings as boolean
            from .tracer import FtdiMpsseTracer
            self._tracer = FtdiMpsseTracer(self.device_version)
            self.log.debug('Using MPSSE tracer')
        # Set latency timer
        self.set_latency_timer(latency)
        # Set chunk size
        self.write_data_set_chunksize()
        self.read_data_set_chunksize()
        self.set_flowctrl('hw')
        # Reset feature mode
        self.set_bitmode(0, Ftdi.BitMode.RESET)
        # Drain buffers
        self.purge_buffers()
        # Disable event and error characters
        self.set_event_char(0, False)
        self.set_error_char(0, False)
        # Enable MPSSE mode
        self.set_bitmode(direction, Ftdi.BitMode.MPSSE)
        # Configure clock
        frequency = self._set_frequency(frequency)

        self.enable_loopback_mode(True)
        # Write a bogus MPSSE op-code
        self.write_data(bytearray((0xAB,)))
        resp = self.read_data(2)
        if len(resp) < 2 or resp[0] != 0xFA or resp[1] != 0xAB:
            raise FtdiMpsseError('Unable to synchronize MPSSE')
        self.enable_loopback_mode(False)

        # Configure I/O
        cmd = bytearray((Ftdi.SET_BITS_LOW, initial & 0xFF, direction & 0xFF))
        if self.has_wide_port:
            initial >>= 8
            direction >>= 8
            cmd.extend((Ftdi.SET_BITS_HIGH, initial & 0xFF, direction & 0xFF))
        self.write_data(cmd)
        self.validate_mpsse()
        # Return the actual frequency
        return frequency

    def open_bitbang_from_url(self, url: str, direction: int = 0x0,
                              latency: int = 16, baudrate: int = 1000000,
                              sync: bool = False) -> float:
        """Open a new interface to the specified FTDI device in bitbang mode.

           Bitbang enables direct read or write to FTDI GPIOs.

           :param url: a FTDI URL selector
           :param direction: a bitfield specifying the FTDI GPIO direction,
                where high level defines an output, and low level defines an
                input
           :param latency: low-level latency to select the USB FTDI poll
                delay. The shorter the delay, the higher the host CPU load.
           :param baudrate: pace to sequence GPIO exchanges
           :param sync: whether to use synchronous or asynchronous bitbang
           :return: actual bitbang baudrate in bps
        """
        devdesc, interface = self.get_identifiers(url)
        return self.open_bitbang_from_device(devdesc, interface,
                                             direction=direction,
                                             latency=latency,
                                             baudrate=baudrate,
                                             sync=sync)

    def open_bitbang(self, vendor: int, product: int,
                     bus: Optional[int] = None, address: Optional[int] = None,
                     index: int = 0, serial: Optional[str] = None,
                     interface: int = 1, direction: int = 0x0,
                     latency: int = 16, baudrate: int = 1000000,
                     sync: bool = False) -> float:
        """Open a new interface to the specified FTDI device in bitbang mode.

           Bitbang enables direct read or write to FTDI GPIOs.

           :param vendor: USB vendor id
           :param product: USB product id
           :param index: optional selector, specified the n-th matching
                             FTDI enumerated USB device on the host
           :param serial: optional selector, specified the FTDI device
                              by its serial number
           :param interface: FTDI interface/port
           :param direction: a bitfield specifying the FTDI GPIO direction,
                where high level defines an output, and low level defines an
                input
           :param latency: low-level latency to select the USB FTDI poll
                delay. The shorter the delay, the higher the host CPU load.
           :param baudrate: pace to sequence GPIO exchanges
           :param sync: whether to use synchronous or asynchronous bitbang
           :return: actual bitbang baudrate in bps
        """
        return self.open_bitbang_from_device(serial, interface,
                                             direction=direction,
                                             latency=latency,
                                             baudrate=baudrate,
                                             sync=sync)

    def open_bitbang_from_device(self, device: str,
                                 interface: int = 1, direction: int = 0x0,
                                 latency: int = 16, baudrate: int = 1000000,
                                 sync: bool = False) -> int:
        """Open a new interface to the specified FTDI device in bitbang mode.

           Bitbang enables direct read or write to FTDI GPIOs.

           :param device: FTDI USB device
           :param interface: FTDI interface/port
           :param direction: a bitfield specifying the FTDI GPIO direction,
                where high level defines an output, and low level defines an
                input
           :param latency: low-level latency to select the USB FTDI poll
                delay. The shorter the delay, the higher the host CPU load.
           :param baudrate: pace to sequence GPIO exchanges
           :param sync: whether to use synchronous or asynchronous bitbang
           :return: actual bitbang baudrate in bps
        """
        self.open_from_device(device, interface)
        # Set latency timer
        self.set_latency_timer(latency)
        # Set chunk size
        # Beware that RX buffer, over 512 bytes, contains 2-byte modem marker
        # on every 512 byte chunk, so data and out-of-band marker get
        # interleaved. This is not yet supported with read_data_bytes for now
        self.write_data_set_chunksize()
        self.read_data_set_chunksize()
        # disable flow control
        self.set_flowctrl('')
        # Enable BITBANG mode
        self.set_bitmode(direction, Ftdi.BitMode.BITBANG if not sync else
                         Ftdi.BitMode.SYNCBB)
        # Configure clock
        if baudrate:
            self._baudrate = self._set_baudrate(baudrate, False)
        # Drain input buffer
        self.purge_buffers()
        return self._baudrate

    @property
    def usb_path(self) -> Tuple[int, int, int]:
        """Provide the physical location on the USB topology.

           :return: a tuple of bus, address, interface; if connected
        """
        if not self.is_connected:
            raise FtdiError('Not connected')
        return None

    @property
    def device_version(self) -> int:
        """Report the device version, i.e. the kind of device.

           :see: :py:meth:`ic_name` for a product version of this information.

           :return: the device version (16-bit integer)
        """
        if not self.is_connected:
            raise FtdiError('Device characteristics not yet known')
        return self._devVersion

    @property
    def ic_name(self) -> str:
        """Return the current type of the FTDI device as a string

           see also http://www.ftdichip.com/Support/
           Documents/TechnicalNotes/TN_100_USB_VID-PID_Guidelines.pdf

           :return: the identified FTDI device as a string
        """
        if not self.is_connected:
            return 'unknown'
        return self.DEVICE_NAMES.get(self.device_version, 'undefined')

    @property
    def device_port_count(self) -> int:
        """Report the count of port/interface of the Ftdi device.

           :return: the count of ports
        """
        if not self.is_connected:
            raise FtdiError('Device characteristics not yet known')
        return self.PORT_COUNTS.get(self.device_version, 0)

    @property
    def port_index(self) -> int:
        """Report the port/interface index, starting from 1

           :return: the port position/index
        """
        if not self.is_connected:
            raise FtdiError('Device characteristics not yet known')
        return self._index

    @property
    def port_width(self) -> int:
        """Report the width of a single port / interface

           :return: the width of the port, in bits
           :raise FtdiError: if no FTDI port is open
        """
        if not self.is_connected:
            raise FtdiError('Device characteristics not yet known')
        if self.device_version in (0x0700, 0x0900):
            return 16
        if self.device_version in (0x0500, ):
            return 12
        return 8

    @property
    def has_mpsse(self) -> bool:
        """Tell whether the device supports MPSSE (I2C, SPI, JTAG, ...)

           :return: True if the FTDI device supports MPSSE
           :raise FtdiError: if no FTDI port is open
        """
        if not self.is_connected:
            raise FtdiError('Device characteristics not yet known')
        return self.device_version in (0x0500, 0x0700, 0x0800, 0x0900)

    @property
    def has_wide_port(self) -> bool:
        """Tell whether the device supports 16-bit GPIO ports (vs. 8 bits)

           :return: True if the FTDI device supports wide GPIO port
           :raise FtdiError: if no FTDI port is open
        """
        return self.port_width > 8

    @property
    def has_cbus(self) -> bool:
        """Tell whether the device supports CBUS bitbang.

           CBUS bitbanging feature requires a special configuration in EEPROM.
           This function only reports if the current device supports this mode,
           not if this mode has been enabled in EEPROM.

           EEPROM configuration must be queried to check which CBUS pins have
           been configured for GPIO/bitbang mode.

           :return: True if the FTDI device supports CBUS bitbang
           :raise FtdiError: if no FTDI port is open
        """
        if not self.is_connected:
            raise FtdiError('Device characteristics not yet known')
        return self.device_version in (0x0600, 0x0900, 0x1000)

    @property
    def has_drivezero(self) -> bool:
        """Tell whether the device supports drive-zero mode, i.e. if the
           device supports the open-collector drive mode, useful for I2C
           communication for example.

           :return: True if the FTDI device features drive-zero mode
           :raise FtdiError: if no FTDI port is open
        """
        if not self.is_connected:
            raise FtdiError('Device characteristics not yet known')
        return self.device_version in (0x0900, )

    @property
    def is_legacy(self) -> bool:
        """Tell whether the device is a low-end FTDI

           :return: True if the FTDI device can only be used as a slow USB-UART
                    bridge
           :raise FtdiError: if no FTDI port is open
        """
        if not self.is_connected:
            raise FtdiError('Device characteristics not yet known')
        return self.device_version <= 0x0200

    @property
    def is_H_series(self) -> bool:
        """Tell whether the device is a high-end FTDI

           :return: True if the FTDI device is a high-end USB-UART bridge
           :raise FtdiError: if no FTDI port is open
        """
        if not self.is_connected:
            raise FtdiError('Device characteristics not yet known')
        return self.device_version in (0x0700, 0x0800, 0x0900)


    @property
    def is_mpsse(self) -> bool:
        """Tell whether the device is configured in MPSSE mode

           :return: True if the FTDI interface is configured in MPSSE mode
        """
        return self._bitmode == Ftdi.BitMode.MPSSE

    def is_mpsse_interface(self, interface: int) -> bool:
        """Tell whether the interface supports MPSSE (I2C, SPI, JTAG, ...)

           :return: True if the FTDI interface supports MPSSE
           :raise FtdiError: if no FTDI port is open
        """
        if not self.has_mpsse:
            return False
        if self.device_version == 0x0800 and interface > 2:
            return False
        return True

    @property
    def is_bitbang_enabled(self) -> bool:
        """Tell whether some bitbang mode is activated

           :return: True if the FTDI interface is configured to support
                    bitbanging
        """
        return self._bitmode not in (
            Ftdi.BitMode.RESET,
            Ftdi.BitMode.MPSSE,
            Ftdi.BitMode.CBUS  # CBUS mode does not change base frequency
        )

    # legacy API
    bitbang_enabled = is_bitbang_enabled

    @property
    def frequency_max(self) -> float:
        """Tells the maximum frequency for MPSSE clock.

           :return: the maximum supported frequency in Hz
        """
        return Ftdi.BUS_CLOCK_HIGH if self.is_H_series else Ftdi.BUS_CLOCK_BASE

    @property
    def fifo_sizes(self) -> Tuple[int, int]:
        """Return the (TX, RX) tupple of hardware FIFO sizes

           :return: 2-tuple of TX, RX FIFO size in bytes
        """
        try:
            return Ftdi.FIFO_SIZES[self.device_version]
        except KeyError:
            raise FtdiFeatureError('Unsupported device: 0x%04x' %
                                   self.device_version)

    @property
    def mpsse_bit_delay(self) -> float:
        """Delay between execution of two MPSSE SET_BITS commands.

           :return: minimum delay (actual value might be larger) in seconds
        """
        # measured on FTDI2232H, not documented in datasheet, hence may vary
        # from on FTDI model to another...
        # left as a variable so it could be tweaked base on the FTDI bcd type,
        # the frequency, or ... whatever else
        return 0.5E-6  # seems to vary between 5 and 6.5 us

    @property
    def baudrate(self) -> int:
        """Return current baudrate.
        """
        return self._baudrate

    @property
    def usb_dev(self) -> str:
        """Return the underlying USB Device.
        """
        return self._base_serial_num

    def set_baudrate(self, baudrate: int, constrain: bool = True) -> int:
        """Change the current UART or BitBang baudrate.

           The FTDI device is not able to use an arbitrary baudrate. Its
           internal dividors are only able to achieve some baudrates.

           PyFtdi attemps to find the closest configurable baudrate and if
           the deviation from the requested baudrate is too high, it rejects
           the configuration if constrain is set.

           :py:attr:`baudrate` attribute can be used to retrieve the exact
           selected baudrate.

           :py:const:`BAUDRATE_TOLERANCE` defines the maximum deviation between
           the requested baudrate and the closest FTDI achieveable baudrate,
           which matches standard UART clock drift (3%). If the achievable
           baudrate is not within limits, baudrate setting is rejected.

           :param baudrate: the new baudrate for the UART.
           :param constrain: whether to validate baudrate is in RS232 tolerance
                             limits or allow larger drift
           :raise ValueError: if deviation from selected baudrate is too large
           :raise FtdiError: on IO Error
           :return: the effective baudrate
        """
        self._baudrate = self._set_baudrate(baudrate, constrain)
        return self._baudrate

    def set_frequency(self, frequency: float) -> float:
        """Change the current MPSSE bus frequency

           The FTDI device is not able to use an arbitrary frequency. Its
           internal dividors are only able to achieve some frequencies.

           PyFtdi finds and selects the closest configurable frequency.

           :param frequency: the new frequency for the serial interface,
                in Hz.
           :return: the selected frequency, which may differ from the requested
                one, in Hz
        """
        return self._set_frequency(frequency)

    def purge_rx_buffer(self) -> None:
        """Clear the USB receive buffer on the chip (host-to-ftdi) and the
           internal read buffer."""
        if self._d2xx.FT_Purge(self._handle, c_ulong(1)) != 0:
            raise FtdiError('Unable to flush RX buffer')
        # Invalidate data in the readbuffer
        self._readoffset = 0
        self._readbuffer = bytearray()
        self.log.debug('rx buf purged')

    def purge_tx_buffer(self) -> None:
        """Clear the USB transmit buffer on the chip (ftdi-to-host)."""
        if self._d2xx.FT_Purge(self._handle, c_ulong(2)) != 0:
            raise FtdiError('Unable to flush TX buffer')

    def purge_buffers(self) -> None:
        """Clear the buffers on the chip and the internal read buffer."""
        self.purge_rx_buffer()
        self.purge_tx_buffer()

    def write_data_set_chunksize(self, chunksize: int = 0) -> None:
        """Configure write buffer chunk size.

           This is a low-level configuration option, which is not intended to
           be use for a regular usage.

           :param chunksize: the optional size of the write buffer in bytes,
                             it is recommended to use 0 to force automatic
                             evaluation of the best value.
        """
        if chunksize == 0:
            chunksize = self.fifo_sizes[0]
        self._writebuffer_chunksize = chunksize
        self.log.debug('TX chunksize: %d', self._writebuffer_chunksize)

    def write_data_get_chunksize(self) -> int:
        """Get write buffer chunk size.

           :return: the size of the write buffer in bytes
        """
        return self._writebuffer_chunksize

    def read_data_set_chunksize(self, chunksize: int = 0) -> None:
        """Configure read buffer chunk size.

           This is a low-level configuration option, which is not intended to
           be use for a regular usage.

           :param chunksize: the optional size of the read buffer in bytes,
                             it is recommended to use 0 to force automatic
                             evaluation of the best value.
        """
        # Invalidate all remaining data
        self._readoffset = 0
        self._readbuffer = bytearray()
        if chunksize == 0:
            # status byte prolog is emitted every maxpacketsize, but for "some"
            # reasons, FT232R emits it every RX FIFO size bytes... Other
            # devices use a maxpacketsize which is smaller or equal to their
            # FIFO size, so this weird behavior is for now only experienced
            # with FT232R. Any, the following compution should address all
            # devices.
            chunksize = min(self.fifo_sizes[0], self.fifo_sizes[1],
                            self._max_packet_size)
        if platform == 'linux':
            if chunksize > 16384:
                chunksize = 16384
        self._readbuffer_chunksize = chunksize
        self._raw_readbuffer = (c_uint8 * self._readbuffer_chunksize)()
        self.log.debug('RX chunksize: %d', self._readbuffer_chunksize)

    def read_data_get_chunksize(self) -> int:
        """Get read buffer chunk size.

           :return: the size of the write buffer in bytes
        """
        return self._readbuffer_chunksize

    def set_bitmode(self, bitmask: int, mode: 'Ftdi.BitMode') -> None:
        """Enable/disable bitbang modes.

           Switch the FTDI interface to bitbang mode.
        """
        self.log.debug('bitmode: %s', mode.name)
        if self._d2xx.FT_SetBitMode(self._handle, c_uint8(bitmask), c_uint8(mode.value)) != 0:
            raise FtdiError('Unable to set bitmode')
        self._bitmode = mode

    def read_pins(self) -> int:
        """Directly read pin state, circumventing the read buffer.
           Useful for bitbang mode.

           :return: bitfield of FTDI interface input GPIO
        """
        pins = c_uint8()
        if self._d2xx.FT_GetBitMode(self._handle, byref(pins)) != 0:
            raise FtdiError('Unable to read pins')
        return pins.value

    def set_cbus_direction(self, mask: int, direction: int) -> None:
        """Configure the CBUS pins used as GPIOs

           :param mask: which pins to configure as GPIOs
           :param direction: which pins are output (vs. input)
        """
        # sanity check: there cannot be more than 4 CBUS pins in bitbang mode
        if not 0 <= mask <= 0x0F:
            raise ValueError('Invalid CBUS gpio mask: 0x%02x' % mask)
        if not 0 <= direction <= 0x0F:
            raise ValueError('Invalid CBUS gpio direction: 0x%02x' % direction)
        self._cbus_pins = (mask, direction)

    def get_cbus_gpio(self) -> int:
        """Get the CBUS pins configured as GPIO inputs

           :return: bitfield of CBUS read pins
        """
        if self._bitmode not in (Ftdi.BitMode.RESET, Ftdi.BitMode.CBUS):
            raise FtdiError('CBUS gpio not available from current mode')
        if not self._cbus_pins[0] & ~self._cbus_pins[1]:
            raise FtdiError('No CBUS IO configured as input')
        outv = (self._cbus_pins[1] << 4) | self._cbus_out
        oldmode = self._bitmode
        try:
            self.set_bitmode(outv, Ftdi.BitMode.CBUS)
            inv = self.read_pins()
            #print(f'BM {outv:04b} {inv:04b}')
        finally:
            if oldmode != self._bitmode:
                self.set_bitmode(0, oldmode)
        return inv & ~self._cbus_pins[1] & self._cbus_pins[0]

    def set_cbus_gpio(self, pins: int) -> None:
        """Set the CBUS pins configured as GPIO outputs

           :param pins: bitfield to apply to CBUS output pins
        """
        if self._bitmode not in (Ftdi.BitMode.RESET, Ftdi.BitMode.CBUS):
            raise FtdiError('CBUS gpio not available from current mode')
        # sanity check: there cannot be more than 4 CBUS pins in bitbang mode
        if not 0 <= pins <= 0x0F:
            raise ValueError('Invalid CBUS gpio pins: 0x%02x' % pins)
        if not self._cbus_pins[0] & self._cbus_pins[1]:
            raise FtdiError('No CBUS IO configured as output')
        pins &= self._cbus_pins[0] & self._cbus_pins[1]
        value = (self._cbus_pins[1] << 4) | pins
        oldmode = self._bitmode
        try:
            self.set_bitmode(value, Ftdi.BitMode.CBUS)
            self._cbus_out = pins
        finally:
            if oldmode != self._bitmode:
                self.set_bitmode(0, oldmode)

    def set_latency_timer(self, latency: int):
        """Set latency timer.

           The FTDI chip keeps data in the internal buffer for a specific
           amount of time if the buffer is not full yet to decrease
           load on the usb bus.

           The shorted the latency, the shorted the delay to obtain data and
           the higher the host CPU load. Be careful with this option.

           :param latency: latency (unspecified unit)
        """
        if not Ftdi.LATENCY_MIN <= latency <= Ftdi.LATENCY_MAX:
            raise ValueError("Latency out of range")
        if self._d2xx.FT_SetLatencyTimer(self._handle, c_uint8(latency)) != 0:
            raise FtdiError('Unable to latency timer')

    def get_latency_timer(self) -> int:
        """Get latency timer.

           :return: the current latency (unspecified unit)
        """
        latency = c_uint8()
        if self._d2xx.FT_GetLatencyTimer(self._handle, byref(latency)) != 0:
            raise FtdiError('Unable to get latency')
        return latency.value

    def poll_modem_status(self) -> int:
        """Poll modem status information.

           This function allows the retrieve the two status bytes of the
           device, useful in UART mode.

           FTDI device does not have a so-called USB "interrupt" end-point,
           event polling on the UART interface is done through the regular
           control endpoint.

           see :py:func:`modem_status` to obtain decoded status strings

           :return: modem status, as a proprietary bitfield
        """
        value = c_uint32()
        if self._d2xx.FT_GetModemStatus(self._handle, byref(value)) != 0:
            raise FtdiError('Unable to get modem status')
        status, = sunpack('<H', value.value)
        return status

    def modem_status(self) -> Tuple[str, ...]:
        """Provide the current modem status as a tuple of set signals

           :return: decodede modem status as short strings
        """
        value = c_uint32()
        if self._d2xx.FT_GetModemStatus(self._handle, byref(value)) != 0:
            raise FtdiError('Unable to get modem status')
        status = [value.value & 0xFF, (value.value >> 8) & 0xFF]
        return self.decode_modem_status(status)

    def set_flowctrl(self, flowctrl: str) -> None:
        """Select flowcontrol in UART mode.

           Either hardware flow control through RTS/CTS UART lines,
           software or no flow control.

           :param str flowctrl: either 'hw' for HW flow control or '' (empty
                                string) for no flow control.
           :raise ValueError: if the flow control argument is invalid

           .. note:: How does RTS/CTS flow control work (from FTDI FAQ):

                FTxxx RTS# pin is an output. It should be connected to the CTS#
                input pin of the device at the other end of the UART link.

                    * If RTS# is logic 0 it is indicating the FTxxx device can
                      accept more data on the RXD pin.
                    * If RTS# is logic 1 it is indicating the FTxxx device
                      cannot accept more data.

                RTS# changes state when the chip buffer reaches its last 32
                bytes of space to allow time for the external device to stop
                sending data to the FTxxx device.

                FTxxx CTS# pin is an input. It should be connected to the RTS#
                output pin of the device at the other end of the UART link.

                  * If CTS# is logic 0 it is indicating the external device can
                    accept more data, and the FTxxx will transmit on the TXD
                    pin.
                  * If CTS# is logic 1 it is indicating the external device
                    cannot accept more data. the FTxxx will stop transmitting
                    within 0~3 characters, depending on what is in the buffer.

                    **This potential 3 character overrun does occasionally
                    present problems.** Customers shoud be made aware the FTxxx
                    is a USB device and not a "normal" RS232 device as seen on
                    a PC. As such the device operates on a packet basis as
                    opposed to a byte basis.

                Word to the wise. Not only do RS232 level shifting devices
                level shift, but they also invert the signal.
        """
        ctrl = {'hw': Ftdi.SIO_RTS_CTS_HS,
                '': Ftdi.SIO_DISABLE_FLOW_CTRL}
        try:
            value = ctrl[flowctrl]
        except KeyError:
            raise ValueError('Unknown flow control: %s' % flowctrl)
        if self._d2xx.FT_SetFlowControl(self._handle, c_uint16(value), c_uint8(0), c_uint8(0)) != 0:
            raise FtdiError('Unable to set flow control')

    def set_dtr(self, state: bool) -> None:
        """Set dtr line

           :param state: new DTR logical level
        """
        if state:
            if self._d2xx.FT_SetDtr(self._handle) != 0:
                raise FtdiError('Unable to set DTR line')
        else:
            if self._d2xx.FT_ClrDtr(self._handle) != 0:
                raise FtdiError('Unable to set DTR line')

    def set_rts(self, state: bool) -> None:
        """Set rts line

           :param state: new RTS logical level
        """
        if state:
            if self._d2xx.FT_SetRts(self._handle) != 0:
                raise FtdiError('Unable to set RTS line')
        else:
            if self._d2xx.FT_ClrRts(self._handle) != 0:
                raise FtdiError('Unable to set RTS line')

    def set_dtr_rts(self, dtr: bool, rts: bool) -> None:
        """Set dtr and rts lines at once

           :param dtr: new DTR logical level
           :param rts: new RTS logical level
        """
        self.set_dtr(dtr)
        self.set_rts(rts)

    def set_break(self, break_: bool) -> None:
        """Start or stop a break exception event on the serial line

           :param break_: either start or stop break event
        """
        if break_:
            if self._d2xx.FT_SetBreakOn(self._handle) != 0:
                raise FtdiError('Unable to start break sequence')
        else:
            if self._d2xx.FT_SetBreakOff(self._handle) != 0:
                raise FtdiError('Unable to stop break sequence')

    def set_event_char(self, eventch: int, enable: bool) -> None:
        """Set the special event character"""
        self._event_char = eventch
        self._event_char_enabled = enable
        if self._d2xx.FT_SetChars(self._handle,
                                  c_uint8(self._event_char), c_uint8(self._event_char_enabled),
                                  c_uint8(self._error_char), c_uint8(self._error_char_enabled)) != 0:
            raise FtdiError('Unable to set event char')

    def set_error_char(self, errorch: int, enable: bool) -> None:
        """Set error character"""
        self._error_char = errorch
        self._error_char_enabled = enable
        if self._d2xx.FT_SetChars(self._handle,
                                  c_uint8(self._event_char), c_uint8(self._event_char_enabled),
                                  c_uint8(self._error_char), c_uint8(self._error_char_enabled)) != 0:
            raise FtdiError('Unable to set error char')

    def set_line_property(self, bits: int, stopbit: Union[int, float],
                          parity: str, break_: bool = False) -> None:
        """Configure the (RS232) UART characteristics.

           Arguments match the valid subset for FTDI HW of pyserial
           definitions.

           Bits accepts one of the following values:

           * ``7`` for 7-bit characters
           * ``8`` for 8-bit characters

           Stopbit accepts one of the following values:

           * ``1`` for a single bit
           * ``1.5`` for a bit and a half
           * ``2`` for two bits

           Parity accepts one of the following strings:

           * ``N`` for no parity bit
           * ``O`` for odd parity bit
           * ``E`` for even parity bit
           * ``M`` for parity bit always set
           * ``S`` for parity bit always reset

           :param bits: data bit count
           :param stopbit: stop bit count
           :param parity: parity mode as a single uppercase character
           :param break_: force break event
        """
        bytelength = {7: Ftdi.BITS_7,
                      8: Ftdi.BITS_8}
        parities = {'N': Ftdi.PARITY_NONE,
                    'O': Ftdi.PARITY_ODD,
                    'E': Ftdi.PARITY_EVEN,
                    'M': Ftdi.PARITY_MARK,
                    'S': Ftdi.PARITY_SPACE}
        stopbits = {1: Ftdi.STOP_BIT_1,
                    1.5: Ftdi.STOP_BIT_15,
                    2: Ftdi.STOP_BIT_2}
        if parity not in parities:
            raise FtdiFeatureError("Unsupported parity")
        if bits not in bytelength:
            raise FtdiFeatureError("Unsupported byte length")
        if stopbit not in stopbits:
            raise FtdiFeatureError("Unsupported stop bits")

        if self._d2xx.FT_SetDataCharacteristics(self._handle,
                                  c_uint8(bytelength[bits]), c_uint8(stopbits[stopbit]),
                                  c_uint8(parities[parity])) != 0:
            raise FtdiError('Unable to set line property')
        self.set_break(break_)

    def enable_adaptive_clock(self, enable: bool = True) -> None:
        """Enable adaptative clock mode, useful in MPSEE mode.

           Adaptive clock is a unique feature designed for a feedback clock
           for JTAG with ARM core.

           :param enable: whether to enable or disable this mode.
           :raise FtdiMpsseError: if MPSSE mode is not enabled
        """
        if not self.is_mpsse:
            raise FtdiMpsseError('Setting adaptive clock mode is only '
                                 'available from MPSSE mode')
        self.write_data(bytearray([enable and Ftdi.ENABLE_CLK_ADAPTIVE or
                                   Ftdi.DISABLE_CLK_ADAPTIVE]))

    def enable_3phase_clock(self, enable: bool = True) -> None:
        """Enable 3-phase clocking mode, useful in MPSSE mode.

           3-phase clock is mostly useful with I2C mode. It is also be used
           as a workaround to support SPI mode 3.

           :param enable: whether to enable or disable this mode.
           :raise FtdiMpsseError: if MPSSE mode is not enabled or device is
                not capable of 3-phase clocking
        """
        if not self.is_mpsse:
            raise FtdiMpsseError('Setting 3-phase clock mode is only '
                                 'available from MPSSE mode')
        if not self.is_H_series:
            raise FtdiFeatureError('This device does not support 3-phase '
                                   'clock')
        self.write_data(bytearray([enable and Ftdi.ENABLE_CLK_3PHASE or
                                   Ftdi.DISABLE_CLK_3PHASE]))

    def enable_drivezero_mode(self, lines: int) -> None:
        """Enable drive-zero mode, useful in MPSSE mode.

           drive-zero mode is mostly useful with I2C mode, to support the open
           collector driving mode.

           :param lines: bitfield of GPIO to drive in collector driven mode
           :raise FtdiMpsseError: if MPSSE mode is not enabled or device is
                not capable of drive-zero mode
        """
        if not self.is_mpsse:
            raise FtdiMpsseError('Setting drive-zero mode is only '
                                 'available from MPSSE mode')
        if not self.has_drivezero:
            raise FtdiFeatureError('This device does not support drive-zero '
                                   'mode')
        self.write_data(bytearray([Ftdi.DRIVE_ZERO, lines & 0xff,
                                   (lines >> 8) & 0xff]))

    def enable_loopback_mode(self, loopback: bool = False) -> None:
        """Enable loopback, i.e. connect DO to DI in FTDI MPSSE port for test
           purposes only. It does not support UART (TX to RX) mode.

           :param loopback: whether to enable or disable this mode
        """
        self.write_data(bytearray((Ftdi.LOOPBACK_START if loopback else
                                   Ftdi.LOOPBACK_END,)))

    def calc_eeprom_checksum(self, data: Union[bytes, bytearray]) -> int:
        """Calculate EEPROM checksum over the data

           :param data: data to compute checksum over. Must be an even number
                        of bytes
           :return: checksum
        """
        length = len(data)
        if not length:
            raise ValueError('No data to checksum')
        if length & 0x1:
            raise ValueError('Length not even')
        # NOTE: checksum is computed using 16-bit values in little endian
        # ordering
        checksum = 0XAAAA
        mtp = self.device_version == 0x1000  # FT230X
        for idx in range(0, length, 2):
            if mtp and 0x24 <= idx < 0x80:
                # special MTP user section which is not considered for the CRC
                continue
            val = ((data[idx+1] << 8) + data[idx]) & 0xffff
            checksum = val ^ checksum
            checksum = ((checksum << 1) & 0xffff) | ((checksum >> 15) & 0xffff)
        return checksum

    def read_eeprom(self, addr: int = 0, length: Optional[int] = None,
                    eeprom_size: Optional[int] = None) -> bytes:
        """Read the EEPROM starting at byte address, addr, and returning
           length bytes. Here, addr and length are in bytes but we
           access a 16-bit word at a time, so automatically update
           addr and length to work with word accesses.

           :param addr: byte address that desire to read.
           :param length: byte length to read or None
           :param eeprom_size: total size in bytes of the eeprom or None
           :return: eeprom bytes, as an array of bytes
        """
        eeprom_size = self._check_eeprom_size(eeprom_size)
        if length is None:
            length = eeprom_size
        if addr < 0 or (addr+length) > eeprom_size:
            raise ValueError('Invalid address/length')
        word_addr = addr >> 1
        word_count = length >> 1
        if (addr & 0x1) | (length & 0x1):
            word_count += 1
        data = bytearray()
        while word_count:
            buf = c_uint16()
            if self._d2xx.FT_ReadEE(self._handle, c_uint32(word_addr), byref(buf)) != 0:
                raise FtdiEepromError('EEPROM read error @ %d' %
                                      (word_addr << 1))
            data.extend(buf.value)
            word_count -= 1
            word_addr += 1
        start = addr & 0x1
        return bytes(data[start:start+length])

    def write_eeprom(self, addr: int, data: Union[bytes, bytearray],
                     eeprom_size: Optional[int] = None,
                     dry_run: bool = True) -> None:
        """Write multiple bytes to the EEPROM starting at byte address,
           addr. This function also updates the checksum
           automatically.

           .. warning:: You can brick your device with invalid size or content.
                        Use this function at your own risk, and RTFM.

           :param addr: starting byte address to start writing
           :param data: data to be written
           :param eeprom_size: total size in bytes of the eeprom or None
           :param dry_run: log what should be written, do not actually
                           change the EEPROM content
        """
        eeprom_size = self._check_eeprom_size(eeprom_size)
        if not data:
            return
        length = len(data)
        if addr < 0 or (addr+length) > eeprom_size:
            # accept up to eeprom_size, even if the last two bytes are
            # overwritten with a locally computed checksum
            raise ValueError('Invalid address/length')
        # First, read out the entire EEPROM, based on eeprom_size.
        eeprom = bytearray(self.read_eeprom(0, eeprom_size))
        # patch in the new data
        eeprom[addr:addr+len(data)] = data
        # compute new checksum
        chksum = self.calc_eeprom_checksum(eeprom[:-2])
        self.log.info('New EEPROM checksum: 0x%04x', chksum)
        # insert updated checksum - it is last 16-bits in EEPROM
        if self.device_version == 0x1000:
            # FT230x EEPROM structure is different
            eeprom[0x7e] = chksum & 0x0ff
            eeprom[0x7f] = chksum >> 8
        else:
            eeprom[-2] = chksum & 0x0ff
            eeprom[-1] = chksum >> 8
        # Write back the new data and checksum back to
        # EEPROM. Only write data that is changing instead of writing
        # everything in EEPROM, even if the data does not change.
        #
        # Compute start and end sections of eeprom baring in mind that
        # they must be even since it is a 16-bit EEPROM.
        # If start addr is odd, back it up one.
        start = addr
        size = length
        if start & 0x1:
            start -= 1
            size += 1
        if size & 0x1:
            size += 1
        if size > eeprom_size-2:
            size = eeprom_size-2
        # finally, write new section of data and ...
        self._write_eeprom_raw(start, eeprom[start:start+size],
                               dry_run=dry_run)
        # ... updated checksum
        self._write_eeprom_raw((eeprom_size-2), eeprom[-2:], dry_run=dry_run)

    def overwrite_eeprom(self, data: Union[bytes, bytearray],
                         dry_run: bool = True) -> None:
        """Write the whole EEPROM content, from first to last byte.

           .. warning:: You can brick your device with invalid size or content.
                        Use this function at your own risk, and RTFM.

           :param data: data to be written (should include the checksum)
           :param dry_run: log what should be written, do not actually
                           change the EEPROM content
        """
        if self.device_version in self.INT_EEPROMS:
            eeprom_size = self.INT_EEPROMS[self.device_version]
            if len(data) != eeprom_size:
                raise ValueError('Invalid EEPROM size')
        elif len(data) not in self.EXT_EEPROM_SIZES:
            raise ValueError('Invalid EEPROM size')
        self._write_eeprom_raw(0, data, dry_run=dry_run)

    def write_data(self, data: Union[bytes, bytearray]) -> int:
        """Write data to the FTDI port.

           In UART mode, data contains the serial stream to write to the UART
           interface.

           In MPSSE mode, data contains the sequence of MPSSE commands and
           data.

           Data buffer is split into chunk-sized blocks before being sent over
           the USB bus.

           :param data: the byte stream to send to the FTDI interface
           :return: count of written bytes
        """
        offset = 0
        size = len(data)
        while offset < size:
            write_size = self._writebuffer_chunksize
            if offset + write_size > size:
                write_size = size - offset
            length = self._write(data[offset:offset+write_size])
            # print('WRITE', offset, size, length)
            if length <= 0:
                raise FtdiError("Usb bulk write error")
            offset += length
        return offset

    def read_data_bytes(self, size: int, attempt: int = 1,
                        request_gen: Optional[Callable[[int], bytes]] = None) \
            -> bytes:
        """Read data from the FTDI interface

           In UART mode, data contains the serial stream read from the UART
           interface.

           In MPSSE mode, data contains the sequence of data received and
           processed with the MPSEE engine.

           Data buffer is rebuilt from chunk-sized blocks received over the USB
           bus.

           FTDI device always sends internal status bytes, which are stripped
           out as not part of the data payload.

           Because of the multiple buses, buffers, FIFOs, and MPSSE command
           processing, data might not be immediately available on the host
           side. The attempt argument can be used to increase the attempt count
           to retrieve the expected amount of data, before giving up and
           returning all the received data, which may be shorted than the
           requested amount.

           :param size: the number of bytes to received from the device
           :param attempt: attempt cycle count
           :param request_gen: a callable that takes the number of bytes read
                               and expect a bytes byffer to send back to the
                               remote device. This is only useful to perform
                               optimized/continuous transfer from a slave
                               device.
           :return: payload bytes, as bytes
        """
        # Packet size sanity check
        if not self._max_packet_size:
            raise FtdiError("max_packet_size is bogus")
        packet_size = self._max_packet_size
        length = 1  # initial condition to enter the usb_read loop
        data = bytearray()
        # everything we want is still in the cache?
        if size <= len(self._readbuffer)-self._readoffset:
            data = self._readbuffer[self._readoffset:self._readoffset+size]
            self._readoffset += size
            return data
        # something still in the cache, but not enough to satisfy 'size'?
        if len(self._readbuffer)-self._readoffset != 0:
            data = self._readbuffer[self._readoffset:]
            # end of readbuffer reached
            self._readoffset = len(self._readbuffer)
        # read from USB, filling in the local cache as it is empty
        retry = attempt
        req_size = size
        while (len(data) < size) and (length > 0):
            while True:
                tempbuf = self._read(size - len(data))
                retry -= 1
                length = len(tempbuf)
                # the received buffer contains at least one useful databyte
                # (first 2 bytes in each packet represent the current modem
                # status)
                if length >= 2:
                    if tempbuf[1] & self.TX_EMPTY_BITS:
                        if request_gen:
                            req_size -= length-2
                            if req_size > 0:
                                cmd = request_gen(req_size)
                                if cmd:
                                    self.write_data(cmd)
                if length > 2:
                    retry = attempt
                    if self._latency_threshold:
                        self._adapt_latency(True)
                    # skip the status bytes
                    chunks = (length+packet_size-1) // packet_size
                    count = packet_size - 2
                    # if you want to show status, use the following code:
                    status = tempbuf[:2]
                    if status[1] & self.ERROR_BITS[1]:
                        self.log.error(
                            'FTDI error: %02x:%02x %s',
                            status[0], status[1], (' '.join(
                                self.decode_modem_status(status,
                                                         True)).title()))
                    self._readbuffer = bytearray()
                    self._readoffset = 0
                    srcoff = 2
                    for _ in range(chunks):
                        self._readbuffer += tempbuf[srcoff:srcoff+count]
                        srcoff += packet_size
                    length = len(self._readbuffer)
                    break
                else:
                    # received buffer only contains the modem status bytes
                    # no data received, may be late, try again
                    if retry > 0:
                        continue
                    # no actual data
                    self._readbuffer = bytearray()
                    self._readoffset = 0
                    if self._latency_threshold:
                        self._adapt_latency(False)
                    # no more data to read?
                    return data
            if length > 0:
                # data still fits in buf?
                if (len(data) + length) <= size:
                    data += self._readbuffer[self._readoffset:
                                             self._readoffset+length]
                    self._readoffset += length
                    # did we read exactly the right amount of bytes?
                    if len(data) == size:
                        return data
                else:
                    # partial copy, not enough bytes in the local cache to
                    # fulfill the request
                    part_size = min(size-len(data),
                                    len(self._readbuffer)-self._readoffset)
                    if part_size < 0:
                        raise FtdiError("Internal Error")
                    data += self._readbuffer[self._readoffset:
                                             self._readoffset+part_size]
                    self._readoffset += part_size
                    return data
        # never reached
        raise FtdiError("Internal error")

    def read_data(self, size: int) -> bytes:
        """Shortcut to received a bytes buffer instead of the array of bytes.

           Note that output byte buffer may be shorted than the requested
           size.

           :param size: the number of bytes to received from the device
           :return: payload bytes
        """
        return bytes(self.read_data_bytes(size))

    def get_cts(self) -> bool:
        """Read terminal status line: Clear To Send

           :return: CTS line logical level
        """
        status = self.poll_modem_status()
        return bool(status & self.MODEM_CTS)

    def get_dsr(self) -> bool:
        """Read terminal status line: Data Set Ready

           :return: DSR line logical level
        """
        status = self.poll_modem_status()
        return bool(status & self.MODEM_DSR)

    def get_ri(self) -> bool:
        """Read terminal status line: Ring Indicator

           :return: RI line logical level
        """
        status = self.poll_modem_status()
        return bool(status & self.MODEM_RI)

    def get_cd(self) -> bool:
        """Read terminal status line: Carrier Detect

           :return: CD line logical level
        """
        status = self.poll_modem_status()
        return bool(status & self.MODEM_RLSD)

    def set_dynamic_latency(self, lmin: int, lmax: int,
                            threshold: int) -> None:
        """Set up or disable latency values.

           Dynamic latency management is a load balancer to adapt the
           responsiveness of FTDI read request vs. the host CPU load.

           It is mostly useful in UART mode, so that read bandwidth can be
           increased to the maximum achievable throughput, while maintaining
           very low host CPU load when no data is received from the UART.

           There should be no need to tweak the default values. Use with care.

           Minimum latency is limited to 12 or above, at FTDI device starts
           losing bytes when latency is too short...

           Maximum latency value is 255 ms.

           Polling latency is reset to `lmin` each time at least one payload
           byte is received from the FTDI device.

           It doubles, up to `lmax`, every `threshold` times no payload has
           been received from the FTDI device.

           :param lmin: minimum latency level (ms)
           :param lmax: maximum latenty level (ms)
           :param threshold: count to reset latency to maximum level
        """
        if not threshold:
            self._latency_count = 0
            self._latency_threshold = None
        else:
            for lat in (lmin, lmax):
                if not self.LATENCY_MIN <= lat <= self.LATENCY_MAX:
                    raise ValueError("Latency out of range: %d" % lat)
            self._latency_min = lmin
            self._latency_max = lmax
            self._latency_threshold = threshold
            self._latency = lmin
            self.set_latency_timer(self._latency)

    def validate_mpsse(self) -> None:
        """Check that the previous MPSSE request has been accepted by the FTDI
           device.

           :raise FtdiError: if the FTDI device rejected the command.
        """
        # only useful in MPSSE mode
        num_avail = c_uint32(0)
        if self._d2xx.FT_GetQueueStatus(self._handle, byref(num_avail)) != 0:
            raise FtdiError('Unable to check MPSSE response') from None
        if num_avail.value > 0:
            bytes_ = self.read_data(2)
            if (len(bytes_) >= 2) and (bytes_[0] == '\xfa'):
                raise FtdiError("Invalid command @ %d" % bytes_[1])

    @classmethod
    def get_error_string(cls) -> str:
        """Wrapper for legacy compatibility.

           :return: a constant, meaningless string
        """
        return "Unknown error"

    # --- Private implementation -------------------------------------------

    def _set_interface(self, config: str, ifnum: int):
        """Select the interface to use on the FTDI device"""
        raise NotImplementedError()

    def _reset_usb_device(self) -> None:
        """Reset USB device (USB command, not FTDI specific)."""
        if self._d2xx.FT_CyclePort(self._handle) != 0:
            raise FtdiError('Unable to cycle port on FTDI device')

    def _reset_device(self):
        """Reset the FTDI device (FTDI vendor command)"""
        if self._d2xx.FT_ResetDevice(self._handle) != 0:
            raise FtdiError('Unable to reset FTDI device')

    def _update_timeouts(self) -> None:
        """Update the read and write timeouts."""
        if self._d2xx.FT_SetTimeouts(self._handle, c_uint32(self._usb_read_timeout), c_uint32(self._usb_write_timeout)) != 0:
            raise FtdiError('Unable to set read/write timeouts')

    def _write(self, data: Union[bytes, bytearray]) -> int:
        if self._debug_log:
            try:
                self.log.debug('> %s', hexlify(data).decode())
            except TypeError as exc:
                self.log.warning('> (invalid output byte sequence: %s)', exc)
        if self._tracer:
            self._tracer.send(self._index, data)
        num_written = c_uint32()
        if self._d2xx.FT_Write(self._handle, create_string_buffer(bytes(data)), c_uint32(len(data)), byref(num_written)) != 0:
            raise FtdiError('Write Failed') from None
        return num_written.value

    def _read(self, size: int) -> bytearray:
        #num_avail = c_uint32(0)
        #if self._d2xx.FT_GetQueueStatus(self._handle, byref(num_avail)) != 0:
        #    raise FtdiError('Unable to read') from None

        #num_read = c_uint32(0);
        #if num_avail.value > 0:
        #    if num_avail.value > len(self._raw_readbuffer):
        #        num_avail = c_uint32(len(self._raw_readbuffer))
        #    if self._d2xx.FT_Read(self._handle, byref(self._raw_readbuffer), num_avail, byref(num_read)) != 0:
        #        raise FtdiError('Unable to read') from None

        num_read = c_uint32(0);
        if self._d2xx.FT_Read(self._handle, byref(self._raw_readbuffer), c_uint32(size), byref(num_read)) != 0:
            raise FtdiError('Unable to read') from None

        value = c_uint32()
        if self._d2xx.FT_GetModemStatus(self._handle, byref(value)) != 0:
            raise FtdiError('Unable to read')

        data = bytearray()
        data.extend( [value.value & 0xFF, (value.value >> 8) & 0xFF] )
        if num_read.value > 0:
            data.extend( self._raw_readbuffer[:num_read.value] )

        if len(data) > 0:
            if self._debug_log:
                self.log.debug('< %s', hexlify(data).decode())
            if self._tracer and len(data) > 2:
                self._tracer.receive(self._index, data[2:])
        return data

    def _adapt_latency(self, payload_detected: bool) -> None:
        """Dynamic latency adaptation depending on the presence of a
           payload in a RX buffer.

           :param payload_detected: whether a payload has been received
                                    within last RX buffer
        """
        if payload_detected:
            self._latency_count = 0
            if self._latency != self._latency_min:
                self.set_latency_timer(self._latency_min)
                self._latency = self._latency_min
            return
        # no payload received
        self._latency_count += 1
        if self._latency != self._latency_max:
            if self._latency_count > \
                    self._latency_threshold:
                self._latency *= 2
                if self._latency > self._latency_max:
                    self._latency = self._latency_max
                else:
                    self._latency_count = 0
                self.set_latency_timer(self._latency)

    def _check_eeprom_size(self, eeprom_size: Optional[int]) -> int:
        if self.device_version in self.INT_EEPROMS:
            if (eeprom_size and
                    eeprom_size != self.INT_EEPROMS[self.device_version]):
                raise ValueError('Invalid EEPROM size: %d' % eeprom_size)
            eeprom_size = self.INT_EEPROMS[self.device_version]
        else:
            if eeprom_size is None:
                eeprom_size = self.EXT_EEPROM_SIZES[-1]
            if eeprom_size not in self.EXT_EEPROM_SIZES:
                raise ValueError('Invalid EEPROM size: %d' % eeprom_size)
        return eeprom_size

    def _write_eeprom_raw(self, addr: int, data: Union[bytes, bytearray],
                          dry_run: bool = True) -> None:
        """Write multiple bytes to the EEPROM starting at byte address,
           addr. Length of data must be a multiple of 2 since the
           EEPROM is 16-bits. So automatically extend data by 1 byte
           if this is not the case.

           :param int addr: starting byte address to start writing
           :param bytes data: data to be written
           :param dry_run: log what should be written, do not actually
                           change the EEPROM content
        """
        if self.device_version == 0x0600:
            # FT232R internal EEPROM is unstable and latency timer seems
            # to have a direct impact on EEPROM programming...
            latency = self.get_latency_timer()
        else:
            latency = 0
        try:
            if latency:
                self.set_latency_timer(self.LATENCY_EEPROM_FT232R)
            length = len(data)
            if addr & 0x1 or length & 0x1:
                raise ValueError('Address/length not even')
            for word in sunpack('<%dH' % (length//2), data):
                if not dry_run:
                    if self._d2xx.FT_WriteEE(self._handle, c_uint32(addr >> 1), c_uint16(word)) != 0:
                        raise FtdiEepromError('EEPROM Write Error @ %d' % addr)
                    self.log.debug('Write EEPROM [0x%02x]: 0x%04x', addr, word)
                else:
                    self.log.info('Fake write EEPROM [0x%02x]: 0x%04x',
                                  addr, word)
                addr += 2
        finally:
            if latency:
                self.set_latency_timer(latency)

    def _get_max_packet_size(self) -> int:
        """Retrieve the maximum length of a data packet"""
        if not self.is_connected:
            raise IOError("Device is not yet known", ENODEV)
        # Need to assess a real way to find this size
        return 1024

    def _convert_baudrate_legacy(self, baudrate: int) -> Tuple[int, int, int]:
        if baudrate > self.BAUDRATE_REF_BASE:
            raise ValueError('Invalid baudrate (too high)')
        div8 = int(round((8 * self.BAUDRATE_REF_BASE) / baudrate))
        if (div8 & 0x7) == 7:
            div8 += 1
        div = div8 >> 3
        div8 &= 0x7
        if div8 == 1:
            div |= 0xc000
        elif div8 >= 4:
            div |= 0x4000
        elif div8 != 0:
            div |= 0x8000
        elif div == 1:
            div = 0
        value = div & 0xFFFF
        index = (div >> 16) & 0xFFFF
        estimate = int(((8 * self.BAUDRATE_REF_BASE) + (div8//2))//div8)
        return estimate, value, index

    def _convert_baudrate(self, baudrate: int) -> Tuple[int, int, int]:
        """Convert a requested baudrate into the closest possible baudrate
           that can be assigned to the FTDI device

           :param baudrate: the baudrate in bps
           :return: a 3-uple of the apprimated baudrate, the value and index
                    to use as the USB configuration parameter
        """
        if self.device_version == 0x200:
            return self._convert_baudrate_legacy(baudrate)
        if self.is_H_series and baudrate >= 1200:
            hispeed = True
            clock = self.BAUDRATE_REF_HIGH
            bb_ratio = self.BITBANG_BAUDRATE_RATIO_HIGH
        else:
            hispeed = False
            clock = self.BAUDRATE_REF_BASE
            bb_ratio = self.BITBANG_BAUDRATE_RATIO_BASE
        if baudrate > clock:
            raise ValueError('Invalid baudrate (too high)')
        if baudrate < ((clock >> 14) + 1):
            raise ValueError('Invalid baudrate (too low)')
        if self.is_bitbang_enabled:
            baudrate //= bb_ratio
        div8 = int(round((8 * clock) / baudrate))
        div = div8 >> 3
        div |= self.FRAC_DIV_CODE[div8 & 0x7] << 14
        if div == 1:
            div = 0
        elif div == 0x4001:
            div = 1
        if hispeed:
            div |= 0x00020000
        value = div & 0xFFFF
        index = (div >> 16) & 0xFFFF
        if self.has_mpsse:
            index <<= 8
            index |= self._index
        estimate = int(((8 * clock) + (div8//2))//div8)
        if self.is_bitbang_enabled:
            estimate *= bb_ratio
        return estimate, value, index

    def _set_baudrate(self, baudrate: int, constrain: bool) -> int:
        if self.is_mpsse:
            raise FtdiFeatureError('Cannot change frequency w/ current mode')
        actual, value, index = self._convert_baudrate(baudrate)
        delta = 100*abs(float(actual-baudrate))/baudrate
        self.log.debug('Actual baudrate: %d %.1f%% div [%04x:%04x]',
                       actual, delta, index, value)
        # return actual
        if constrain and delta > Ftdi.BAUDRATE_TOLERANCE:
            raise ValueError('Baudrate tolerance exceeded: %.02f%% '
                             '(wanted %d, achievable %d)' %
                             (delta, baudrate, actual))
        if self._d2xx.FT_SetBaudRate(self._handle, c_uint32(actual)) != 0:
            raise FtdiError('Unable to set baudrate')
        return actual

    def _set_frequency(self, frequency: float) -> float:
        """Convert a frequency value into a TCK divisor setting"""
        if not self.is_mpsse:
            raise FtdiFeatureError('Cannot change frequency w/ current mode')
        if frequency > self.frequency_max:
            raise FtdiFeatureError('Unsupported frequency: %f' % frequency)
        # Calculate base speed clock divider
        divcode = Ftdi.ENABLE_CLK_DIV5
        divisor = int((Ftdi.BUS_CLOCK_BASE+frequency/2)/frequency)-1
        divisor = max(0, min(0xFFFF, divisor))
        actual_freq = Ftdi.BUS_CLOCK_BASE/(divisor+1)
        error = (actual_freq/frequency)-1
        # Should we use high speed clock available in H series?
        if self.is_H_series:
            # Calculate high speed clock divider
            divisor_hs = int((Ftdi.BUS_CLOCK_HIGH+frequency/2)/frequency)-1
            divisor_hs = max(0, min(0xFFFF, divisor_hs))
            actual_freq_hs = Ftdi.BUS_CLOCK_HIGH/(divisor_hs+1)
            error_hs = (actual_freq_hs/frequency)-1
            # Enable if closer to desired frequency (percentually)
            if abs(error_hs) < abs(error):
                divcode = Ftdi.DISABLE_CLK_DIV5
                divisor = divisor_hs
                actual_freq = actual_freq_hs
                error = error_hs
        # FTDI expects little endian
        if self.is_H_series:
            cmd = bytearray((divcode,))
        else:
            cmd = bytearray()
        cmd.extend((Ftdi.SET_TCK_DIVISOR, divisor & 0xff,
                    (divisor >> 8) & 0xff))
        self.write_data(cmd)
        self.validate_mpsse()
        # Drain input buffer
        self.purge_rx_buffer()
        # Note that bus frequency may differ from clock frequency, when
        # 3-phase clock is enable, in which case bus frequency = 2/3 clock
        # frequency
        if actual_freq > 1E6:
            self.log.debug('Clock frequency: %.6f MHz (error: %+.1f %%)',
                           (actual_freq/1E6), error*100)
        else:
            self.log.debug('Clock frequency: %.3f KHz (error: %+.1f %%)',
                           (actual_freq/1E3), error*100)
        return actual_freq

    def __get_timeouts(self) -> Tuple[int, int]:
        return self._usb_read_timeout, self._usb_write_timeout

    def __set_timeouts(self, timeouts: Tuple[int, int]):
        (read_timeout, write_timeout) = timeouts
        self._usb_read_timeout = read_timeout
        self._usb_write_timeout = write_timeout
        if self.is_connected:
            self._update_timeouts()

    timeouts = property(__get_timeouts, __set_timeouts)

    @classmethod
    def _load_backend(cls):
        libname = ctypes.util.find_library("FTD2XX.dll")
        if not libname:
            _LOGGER.error('FTD2XX.DLL could not be found')
            raise FtdiLibraryNotFoundException("FTD2XX.DLL")
        try:
            return ctypes.WinDLL(libname)
        except Exception:
            _LOGGER.error(libname + ' could not be loaded', exc_info=True)
            return None
