"""
(C) Copyright 2020
Scott Wiederhold, s.e.wiederhold@gmail.com
https://community.openglow.org
SPDX-License-Identifier:    MIT

Based on python-evdev
Copyright (c) 2012-2016 Georgi Valkov. All rights reserved.
"""
from enum import IntEnum


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
