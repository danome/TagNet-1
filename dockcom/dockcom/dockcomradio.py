from __future__ import print_function   # python3 print function
from builtins import *

from time import sleep, localtime
import binascii

from construct import *

import spidev

try:
    import RPi.GPIO as GPIO
    gpio = True
except RuntimeError as e:
    gpio = False
    print(e)

from si446xdef import *
from si446xcfg import get_config_wds, get_config_local
import si446xtrace

__all__ = ['SpiInterface', 'Si446xRadio', 'si446xtrace_test']

##########################################################################
#
# _get_cts
#
def _get_cts():
    if (gpio):
        return (GPIO.input(GPIO_CTS))
    else:
        return False

# _get_cts_wait
#
def _get_cts_wait(t):
    if (gpio):
        for i in range(t+1):
            r = _get_cts()
            if (r or (t == 0)):  return r
            sleep(.001)
    return False

##########################################################################
#
# common spi access routines
#
# note: spi device driver controls chip select

class SpiInterface:
    """class to access the Si446x over SPI interface"""
    def __init__(self, device, trace=None):
        try:
            self.trace = trace if (trace) else si446xtrace.Trace(100)
            self.spi = spidev.SpiDev()
            self.spi.open(0, device)  # port=0, device(CS)=device_num
            #self.spi.max_speed_hz=600000
            self.trace.add('RADIO_CHIP',
                           'spi max speed: {}'.format(self.spi.max_speed_hz),
                           level=2)
        except IOError as e:
            self.trace.add('RADIO_INIT_ERROR', e, level=2)

    def command(self, pkt, form):
        _get_cts_wait(100)
        if (not _get_cts()):
            self.trace.add('RADIO_CTS_ERROR', 'no cts [1]', level=2)
        try:
            self.form = form
            self.trace.add('RADIO_CMD', bytearray(pkt), form, level=2)
            self.spi.xfer2(list(bytearray(pkt)))
        except IOError as e:
            self.trace.add('RADIO_CMD_ERROR', e, level=2)

    def response(self, rlen, form):
        _get_cts_wait(100)
        rsp = ''
        if (not _get_cts()):
            self.trace.add('RADIO_RSP_ERROR', 'no cts [2]', level=2)
        try:
            r = self.spi.xfer2([0x44] + rlen * [0])
            rsp = bytearray(r[1:rlen+1])
            form = form if (form) else self.form
            self.trace.add('RADIO_RSP', rsp, form, level=2)
        except IOError as e:
            self.trace.add('RADIO_RSP_ERROR', e, level=2)
        return rsp

    def read_fifo(self, rlen):
        if (rlen > RX_FIFO_MAX):
            self.trace.add('RADIO_RX_TOO_LONG', 'len: {}'.format(rlen), level=2)
        r = self.spi.xfer2([0x77] + rlen * [0])
        self.trace.add('RADIO_RX_FIFO', str(rlen), None, level=2)
        return r[1:]

    def write_fifo(self, buf):
        if (len(buf) > TX_FIFO_MAX):
            self.trace.add('RADIO_TX_TOO_LONG', 'len: {}'.format(len(buf)), level=2)
        self.trace.add('RADIO_TX_FIFO', str(len(buf)), None, level=2)
        r = self.spi.xfer2([0x66] + buf)

    def read_frr(self, off, len):
        index = [0,1,3,7]
        if (len > 4):
            self.trace.add('RADIO_FRR_TOO_LONG', 'len: {}, off: {}'.format(len,off), level=2)
        r = self.spi.xfer2([0x50 + index[off]] + len * [0])
        rsp = bytearray(r[1:len+1])
        self.trace.add('RADIO_FRR', rsp, fast_frr_rsp_s.name, level=2)
        return rsp
#end class

##########################################################################

class Si446xRadio(object):
    """ class for handling low level Radio device control"""
    def __init__(self, device=0, callback=None, trace=None):
        self.trace = trace if (trace) else si446xtrace.Trace(100)
        self.channel = 0
        self.callback = callback if (callback) else self._gpio_callback
        self.dump_strings = {}
        self.spi = SpiInterface(device, trace=self.trace)
    #end def

    def _gpio_callback(self, channel):
        self.trace.add('si446xradio: Edge detected on channel %s'%channel)

    def change_state(self, state,  wait=0):
        """
        change_state - force radio chip to change to specific state.
        waits (ms) for acknowledgement that radio has processed the change
        """
        request = change_state_cmd_s.parse('\x00' * change_state_cmd_s.sizeof())
        request.cmd = 'CHANGE_STATE'
        request.state = state
        cmd = change_state_cmd_s.build(request)
        self.spi.command(cmd, change_state_cmd_s.name)
        _get_cts_wait(wait)
    #end def

    def check_CCA(self):
        """
        Perform Clear Channel Assessment.
        """
        rssi = self.fast_latched_rssi()
        return True if (rssi < si446x_cca_threshold) else False
    #end def

    def clear_interrupts(self, clr_flags=None):
        """
        Clear radio chip pending interrupts  default (nothing in clr_flags)
        then clear all interrupts
        """
        request = read_cmd_s.parse('\x00' * read_cmd_s.sizeof())
        request.cmd='GET_INT_STATUS'
        cf = clr_pend_int_s.build(clr_flags) if (clr_flags) else ''
        cmd = read_cmd_s.build(request) + cf
        self.spi.command(cmd, read_cmd_s.name)
    #end def

    def config_frr(self):
        """
        Configure the Fast Response Registers to the expected values by Driver
        const uint8_t si446x_frr_config[] = { 0x11, 0x02, 0x04, 0x00,

        frr is set manually right after POWER_UP
        A: device state
        B: PH_PEND
        C: MODEM_PEND
        D: Latched_RSSI

        We use LR (Latched_RSSI) when receiving a packet.  The RSSI value is
        attached to the last RX packet.  The Latched_RSSI value may, depending
        on configuration, be associated with some number of bit times once RX 
        is enabled or when SYNC is detected.
        """
        request = config_frr_cmd_s.parse('\x00' * config_frr_cmd_s.sizeof())
        request.cmd='SET_PROPERTY'
        request.group='FRR_CTL'
        request.num_props=4
        request.start_prop=0
        request.a_mode='CURRENT_STATE'
        request.b_mode='INT_PH_PEND'
        request.c_mode='INT_MODEM_PEND'
        request.d_mode='LATCHED_RSSI'
        cmd = config_frr_cmd_s.build(request)
        self.spi.command(cmd, config_frr_cmd_s.name)
    #end def

    def disable_interrupt(self):
        """
        Disable radio chip hardware interrupt
        """
        if (gpio):
            GPIO.remove_event_detect(GPIO_NIRQ)
    #end def

    def dump_radio(self):
        """
        Dump all of the current radio chip configuration to memory. This is a side effect of
        getting all the properties since all spi_ I/O operations are traced.
        """
        for gp_n, gp_s in radio_config_groups.iteritems():
            i = 0
            s = ''
            while (True):
                """
                accumulate entire property, repeating get_ for MAX_RADIO_RSP size (16 bytes)
                until all pieces have been retrieved.
                """
                r = gp_s.sizeof() - i
                x = r if (r < MAX_RADIO_RSP) else MAX_RADIO_RSP
                s += self.get_property(radio_config_group_ids.parse(gp_n), i, x)
                i += x
                if (i >= gp_s.sizeof()):
                    break
            self.dump_strings[gp_n] = s
            self.dump_time = localtime()
    #end def

    def enable_interrupts(self):
        """
        Enable radio chip interrupts
        """
        if (gpio):
            GPIO.add_event_detect(GPIO_NIRQ,
                                  GPIO.FALLING,
                                  callback=self.callback,
                                  bouncetime=100)
    #end def

    def  fast_all(self):
        """
        Read all four fast response registers
        """
        return self.spi.read_frr(0, 4)
    #end def
     
    def fast_device_state(self):
        """
        Get current state of the radio chip from fast read register
        """
        return ord(self.spi.read_frr(0, 1))
    #end def

    def fast_latched_rssi(self):
        """
        get RSSI from fast read register

        The radio chip measures the receive signal strength (RSSI) during the
        beginning of receiving a packet, and latches this value.
        """
        return ord( self.spi.read_frr(3, 1))
    #end def

    def fast_modem_pend(self):
        """
        get modem pending interrupt flags from fast read register
        """
        return ord(self.spi.read_frr(2, 1))
    #end def

    def fast_ph_pend(self):
        """
        get modem pending interrupt flags from fast read register
        """
        return ord(self.spi.read_frr(1, 1))
    #end def

    def fifo_info(self, rx_flush=False, tx_flush=False):
        """
        Get the current tx/rx fifo depths and optionally flush

        returns a list of [rx_fifo_count, tx_fifo_space]
        """
        request = fifo_info_cmd_s.parse('\x00' * fifo_info_cmd_s.sizeof())
        request.cmd='FIFO_INFO'
        request.state.rx_reset=rx_flush
        request.state.tx_reset=tx_flush
        cmd = fifo_info_cmd_s.build(request)
        self.spi.command(cmd, fifo_info_cmd_s.name)
        rsp = self.spi.response(fifo_info_rsp_s.sizeof(), fifo_info_rsp_s.name)
        if (rsp):
            response = fifo_info_rsp_s.parse(rsp)
            return [response.rx_fifo_count, response.tx_fifo_space]
        return None
    #end def

    def get_channel(self):
        """
        get_channel get current radio channel
        """
        return self.channel
    #end def

    def get_clear_interrupts(self, clr_flags=None):
        """
        Clear radio chip pending interrupts

        Return pending interrupt conditions (existing prior to clear).
        """
        self.clear_interrupts(clr_flags)
        rsp = self.spi.response(int_status_rsp_s.sizeof(), int_status_rsp_s.name)
        if (rsp):
            return (int_status_rsp_s.parse(rsp))
        return None
    #end def

    def get_config_lists(self):
        """
        Get a list of configuration lists

        each list is consists of concatenated pascal strings, each presenting
        a command string for configuring radio chip properties
        """
        return [get_config_wds, get_config_local]
    #end def
    
    def get_cts(self):
        """
        Get current readiness radio chip command processor
        """
        # read CTS, return true if high
        rsp = _get_cts()
        return rsp
    #end def

    def get_gpio(self):
        """
        Get current state and configuration of radio chip GPIO pins
        """        
        request = read_cmd_s.parse('\x00' * read_cmd_s.sizeof())
        request.cmd='GPIO_PIN_CFG'
        cmd = read_cmd_s.build(request)
        self.spi.command(cmd, read_cmd_s.name)
        rsp = self.spi.response(get_gpio_pin_cfg_rsp_s.sizeof(), get_gpio_pin_cfg_rsp_s.name)
        if (rsp):
            response = get_gpio_pin_cfg_rsp_s.parse(rsp)
            return response
        return None
    #end def

    def get_interrupts(self):
        """
        get current interrupt conditions

        doesn't clear any interrupts
        """
        request = read_cmd_s.parse('\x00' * read_cmd_s.sizeof())
        request.cmd='GET_INT_STATUS'
        cmd = read_cmd_s.build(request)
        self.spi.command(cmd, read_cmd_s.name)
        rsp = self.spi.response(int_status_rsp_s.sizeof(), int_status_rsp_s.name)
        if (rsp):
            return (int_status_rsp_s.parse(rsp))
        return None
    #end def

    def get_packet_info(self):
        """
        Get the length of the packet as received by the radio
        """
        request = packet_info_cmd_s.parse('\x00' * packet_info_cmd_s.sizeof())
        request.cmd='PACKET_INFO'
        request.field_num='NO_OVERRIDE'
        cmd = packet_info_cmd_s.build(request)
        self.spi.command(cmd, packet_info_cmd_s.name)
        rsp = self.spi.response(packet_info_rsp_s.sizeof(), packet_info_rsp_s.name)
        if (rsp):
            response = packet_info_rsp_s.parse(rsp)
            return response.length
        return None
    #end def

    def get_property(self, group, prop, len):
        """
        Read one or more contiguous radio chip properties
        """
        request = get_property_cmd_s.parse('\x00' * get_property_cmd_s.sizeof())
        request.cmd='GET_PROPERTY'
        request.group=group
        request.num_props=len
        request.start_prop=prop
        cmd = get_property_cmd_s.build(request)
        self.spi.command(cmd, get_property_cmd_s.name)
        rsp = self.spi.response(17, get_property_rsp_s.name)
        if (rsp):
            response = get_property_rsp_s.parse(rsp)
            return bytearray(response.data)[:len]
        return None
    #end def

    def power_up(self):
        """
        Turn radio chip power on.
        """
        if (not _get_cts()):
            self.trace.add('RADIO_CHIP', 'cts not ready', level=2)
        request = power_up_cmd_s.parse('\x00' * power_up_cmd_s.sizeof())
        request.cmd='POWER_UP'
        request.boot_options.patch=False
        request.boot_options.func=1
        request.xtal_options.txcO=3
        request.xo_freq=4000000
        cmd = power_up_cmd_s.build(request)
        self.spi.command(cmd,  power_up_cmd_s.name)
    #end def

    def read_cmd_buff(self):
        """
        Read Clear-to-send (CTS) status via polling command over SPI

        Pull nsel low. send command. read cts and cmd_buff. If cts=0xff, then 
        pull nsel high and repeat.
        """
        rsp = self.spi.response(read_cmd_buff_rsp_s.sizeof(), read_cmd_buff_rsp_s.name)
        if (rsp):
            response = read_cmd_buff_rsp_s.parse(rsp)
    #end def

    def read_silicon_info(self):
        """
        Read silicon manufacturing information
        """
        request = read_cmd_s.parse('\x00' * read_cmd_s.sizeof())
        request.cmd='PART_INFO'
        cmd = read_cmd_s.build(request)
        self.spi.command(cmd, read_cmd_s.name)
        rsp = self.spi.response(read_part_info_rsp_s.sizeof(),
                                read_part_info_rsp_s.name)
        if (rsp):
            response = read_part_info_rsp_s.parse(rsp)
        request.cmd='FUNC_INFO'
        cmd = read_cmd_s.build(request)
        self.spi.command(cmd, read_cmd_s.name)
        rsp = self.spi.response(read_func_info_rsp_s.sizeof(),
                                read_func_info_rsp_s.name)
        if (rsp):
            response = read_func_info_rsp_s.parse(rsp)
    #end def


    def read_rx_fifo(self, len):
        """
        Read data from the radio chip receive fifo
        returns bytestring
        """
        return self.spi.read_fifo(len)
    #end def

    def set_channel(self, num):
        """
        Set radio channel
        """
        self.channel = num
    #end def
    
    def send_config(self, props):
        """
        Send a config string to the radio chip

        Already formatted into proper byte string with command and parameters
        """
        self.spi.command(props, set_property_cmd_s.name)
    #end def

    def set_property(self, pg, ps, pd):
        """
        Set one or more contiguous radio chip properties
        """
        request = set_property_cmd_s.parse('\x00' * set_property_cmd_s.sizeof())
        request.cmd='SET_PROPERTY'
        request.group=pg
        request.num_props=len(pd)
        request.start_prop=ps
        cmd = set_property_cmd_s.build(request) + pd
        self.spi.command(cmd, set_property_cmd_s.name)
    #end def

    def set_power(self, level):
        """
        Set radio transmission power level (0x7f = 20dBm)
        """
#        pkt = ''.join([chr(item) for item in [0x18, level, 0]])
        self.set_property('PA', 1, chr(level & 0x7f))
    #end def

    def shutdown(self):
        """
        Power off the radio chip.
        """
        self.trace.add('RADIO_CHIP',
                       'set GPIO pin {} (SI446x sdn disable)'.format(GPIO_SDN),
                       level=2)
        if (gpio):
            GPIO.output(GPIO_SDN,1)
            GPIO.cleanup()
    #end def

    def start_rx(self, len, channel=255):
        """
        Transition the radio chip to the receive enabled state
        """
        request = start_rx_cmd_s.parse('\x00' * start_rx_cmd_s.sizeof())
        request.cmd = 'START_RX'
        request.channel = self.channel if (channel == 255) else channel
        request.condition.start = 'IMMEDIATE'
        request.next_state1 = 'NOCHANGE'  # rx timeout
        request.next_state2 = 'READY'     # rx complete
        request.next_state3 = 'READY'     # rx invalid (bad CRC)
        request.rx_len= len
        cmd = start_rx_cmd_s.build(request)
        self.spi.command(cmd, start_rx_cmd_s.name)
    #end def

    def start_rx_short(self):
        """
        Transition the radio chip to the receive enabled state
        """
        request = read_cmd_s.parse('\x00' * read_cmd_s.sizeof())
        request.cmd='START_RX'
        cmd = read_cmd_s.build(request)
        self.spi.command(cmd, read_cmd_s.name)
    #end def

    def start_tx(self, len, channel=255):
        """
        Transition the radio chip to the transmit state.
        """
        request = start_tx_cmd_s.parse('\x00' * start_tx_cmd_s.sizeof())
        request.cmd='START_TX'
        request.channel = self.channel if (channel == 255) else channel
        request.condition.txcomplete_state='READY'
        request.condition.retransmit='NO'
        request.condition.start='IMMEDIATE'
        request.tx_len=len
        cmd = start_tx_cmd_s.build(request)
        self.spi.command(cmd,  start_tx_cmd_s.name)
    #end def

    def trace_radio(self):
        """
        Dump the saved radio chip configuration to the trace buffer
        """
        for k, v in self.dump_strings.iteritems():
            self.trace.add('RADIO_DUMP', v, radio_config_groups[k].name, level=2)
    #end def

    def unshutdown(self):
        """
        Power on the radio chip.

        Set GPIO pin 18 (GPIO23) connected to si446x.sdn
        """
        self.trace.add('RADIO_GPIO',
                       'clear GPIO pin {} (SI446x sdn enable)'.format(GPIO_SDN),
                       level=2)
        if (gpio):
            GPIO.setmode(GPIO.BOARD)
            GPIO.setup(GPIO_CTS,GPIO.IN)    #  [CTSn]
            GPIO.setup(GPIO_NIRQ,GPIO.IN)   #  [IRQ]
            GPIO.setup(GPIO_SDN,GPIO.OUT)   #  [sdn]
            GPIO.output(GPIO_SDN,1)         # make sure it is already shut down
            sleep(.1)
            GPIO.output(GPIO_SDN,0)
            sleep(.1)
    #end def
    
    def write_tx_fifo(self, dat):
        """
        Write data into the radio chip transmit fifo
        """
        self.spi.write_fifo(dat)
    #end def

#end class

def si446xtrace_test_callback():
    print('tested radio callback')

def test_trace(radio, trace):
    for t in trace.rb.data:
        print(type(t[4]),t)

def test_radio(radio, trace):
    radio.unshutdown()
    radio.power_up()
    radio.config_frr()
    list_of_lists = radio.get_config_lists()
    for l in list_of_lists:
        x = 0
        while (True):
            s = l(x)
            if (not s): break
            radio.send_config(s)
            x += len(s) + 1
    radio.set_property('INT_CTL', 0, '\x03\x3b\x23\x00')
    radio.set_property('PKT', 0x0c, '\x10')
    radio.fast_all()
    radio.dump_radio()
    
def si446xtrace_test():
    import si446xtrace
    trace =  si446xtrace.Trace(100)
    radio = Si446xRadio(device=0, callback=si446xtrace_test_callback, trace=trace)
    test_radio(radio, trace)
    test_trace(radio, trace)
    return trace, radio

if __name__ == '__main__':

    t,r = si446xtrace_test()
    
