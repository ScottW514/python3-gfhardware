"""
(C) Copyright 2020
Scott Wiederhold, s.e.wiederhold@gmail.com
https://community.openglow.org
SPDX-License-Identifier:    MIT
"""
from collections import namedtuple
from enum import Enum, IntEnum
from typing import Union

# System Wide Values
LOGGER_NAME = 'openglow'
PULS_DEVICE = '/dev/glowforge'
SYSFS_GF_BASE = '/sys/glowforge/'
SWITCH_DEVICE = '/dev/input/event0'
TEMP_SENSORS = ['chassis', 'water_1', 'water_2', 'power', 'tec']
XY_STEP_PER_MM = 0.15
Z_STEP_PER_MM = 0.70612

# Named Tuples
AxisPosition = namedtuple('AxisPosition', ['steps', 'mm', 'inch'])
Position = namedtuple('Position', ['x', 'y', 'z', 'bytes'])
PulsPosition = namedtuple('PulsPosition', ['total', 'processed'])
SDMA = namedtuple('SDMA', ['pc', 'rpc', 'spc', 'epc', 't', 'sf', 'df', 'lm', 'r0', 'r1', 'r2', 'r3', 'r4', 'r5', 'r6',
                           'r7', 'mda', 'msa', 'ms', 'md', 'pda', 'psa', 'ps', 'pd', 'ca', 'cs', 'dda', 'dsa', 'ds',
                           'dd', 'sc0', 'sc1', 'sc2', 'sc3', 'sc4', 'sc5', 'sc6', 'sc7'])
SwitchEvent = namedtuple('SwitchEvent', ['sec', 'usec', 'type', 'code', 'val'])
Temperature = namedtuple('Temperature', ['raw', 'C', 'F'])


class ButtonColor(Enum):
    OFF = (0, 0, 0)
    WHITE = (255, 255, 255)
    RED = (255, 0, 0)
    PINK = (255, 0, 128)
    MAGENTA = (255, 0, 255)
    ORANGE = (255, 128, 0)
    YELLOW = (255, 255, 0)
    LIME = (128, 255, 0)
    GREEN = (0, 255, 0)
    TEAL = (0, 255, 128)
    SKYBLUE = (0, 128, 255)
    CYAN = (0, 255, 255)
    BLUE = (0, 0, 255)
    PURPLE = (128, 0, 255)


class Dir(Enum):
    Neg = 0
    Pos = 1


class EventCode(IntEnum):
    EV_SYN = 0x00
    EV_SW = 0x05


class InputSwitch(IntEnum):
    SW_DOOR1 = 0x00
    SW_DOOR2 = 0x01
    SW_BUTTON = 0x02
    SW_DOORS = 0x03
    SW_ESTOP = 0x04
    SW_INTERLOCK = 0x05
    SW_INTERLOCK_LATCH = 0x06
    SW_HEAD = 0x07


class MachineState(Enum):
    IDLE = "Idle",
    RUNNING = "Running",
    DISABLED = "Disabled",
    FAULT = "Fault"


class Microstep(Enum):
    FULL = 1
    HALF = 2
    M_1 = 1
    M_2 = 2
    M_4 = 4
    M_8 = 8
    M_16 = 16
    M_32 = 32


class SynCode(IntEnum):
    SYN_REPORT = 0x00
    SYN_CONFIG = 0x01
    SYN_MT_REPORT =	0x02
    SYN_DROPPED = 0x03


class ZCur(Enum):
    HIGH = 0
    LOW = 1


def read_file(attr: str, binary: bool = False) -> Union[str, bytes]:
    with open(attr, 'br' if binary else 'r') as f:
        return f.read() if binary else f.read().strip()


def write_file(attr: str, val: Union[str, bytes], binary: bool = False) -> None:
    with open(attr, 'bw' if binary else 'w') as file:
        file.write(val)


def write_attr(attr: str, val: Union[str, int]) -> None:
    write_file(attr, str(val))


__all__ = [
    # Constants
    'LOGGER_NAME', 'PULS_DEVICE', 'SDMA', 'SYSFS_GF_BASE', 'SWITCH_DEVICE', 'TEMP_SENSORS', 'XY_STEP_PER_MM',
    'Z_STEP_PER_MM',
    # Enums
    'ButtonColor', 'Dir', 'EventCode', 'InputSwitch', 'MachineState', 'Microstep', 'SynCode', 'ZCur',
    # Functions
    'read_file', 'write_attr', 'write_file',
    # Named Tuples
    'AxisPosition', 'Position', 'PulsPosition', 'SwitchEvent', 'Temperature'
]
