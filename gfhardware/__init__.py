"""
(C) Copyright 2020
Scott Wiederhold, s.e.wiederhold@gmail.com
https://community.openglow.org
SPDX-License-Identifier:    MIT
"""
from enum import Enum, IntEnum, auto


class InputSwitch(IntEnum):
    SW_DOOR1 = 0x00
    SW_DOOR2 = 0x01
    SW_BUTTON = 0x02
    SW_DOORS = 0x03
    SW_ESTOP = 0x04
    SW_INTERLOCK = 0x05
    SW_INTERLOCK_LATCH = 0x06
    SW_HEAD = 0x07


class EventCode(IntEnum):
    EV_SYN = 0x00
    EV_SW = 0x05


class SynCode(IntEnum):
    SYN_REPORT = 0x00
    SYN_CONFIG = 0x01
    SYN_MT_REPORT =	0x02
    SYN_DROPPED = 0x03


class MachineState(Enum):
    IDLE = auto(),
    RUNNING = auto(),
    DISABLED = auto(),
    FAULT = auto()


class ButtonColor(IntEnum):
    OFF = 0
    WHITE = 1
    RED = 2
    PINK = 3
    MAGENTA = 4
    ORANGE = 5
    YELLOW = 7
    LIME = 8
    GREEN = 9
    TEAL = 10
    SKYBLUE = 11
    CYAN = 12
    BLUE = 13
    PURPLE = 14


_BUTTON_COLOR = [
    (  0,   0,   0),  # OFF
    (255, 255, 255),  # WHITE
    (255,   0,   0),  # RED
    (255,   0, 128),  # PINK
    (255,   0, 255),  # MAGENTA
    (255, 128,   0),  # ORANGE
    (255, 255,   0),  # YELLOW
    (128, 255,   0),  # LIME
    (  0, 255,   0),  # GREEN
    (  0, 255, 128),  # TEAL
    (  0, 128, 255),  # SKYBLUE
    (  0, 255, 255),  # CYAN
    (  0,   0, 255),  # BLUE
    (128,   0, 255),  # PURPLE
]
