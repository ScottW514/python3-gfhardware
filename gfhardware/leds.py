"""
(C) Copyright 2020
Scott Wiederhold, s.e.wiederhold@gmail.com
https://community.openglow.org
SPDX-License-Identifier:    MIT
"""
from . import ButtonColor, _BUTTON_COLOR
from .shared import write_file


def set_button_color(color: ButtonColor):
    write_file('/sys/class/leds/button_led_1/target', str(_BUTTON_COLOR[color][0]))
    write_file('/sys/class/leds/button_led_2/target', str(_BUTTON_COLOR[color][1]))
    write_file('/sys/class/leds/button_led_3/target', str(_BUTTON_COLOR[color][2]))
