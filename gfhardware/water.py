"""
(C) Copyright 2020
Scott Wiederhold, s.e.wiederhold@gmail.com
https://community.openglow.org
SPDX-License-Identifier:    MIT
"""


class _WaterBase(object):
    def __init__(self):
        self._name = None
        self._path = None

    def _read(self, path: str):
        with open(path, 'r') as file:
            return file.readline()

    def _write(self, path: str, val: str):
        with open(path, 'w') as file:
            file.write(val)

    @property
    def name(self) -> str:
        return self._name

    def on(self):
        with open(self._path, 'w') as file:
            file.write('1')

    def off(self):
        with open(self._path, 'w') as file:
            file.write('0')


class WaterPump(_WaterBase):
    def __init__(self):
        super(_WaterBase, self).__init__()
        self._name = "water_pump"
        self._path = '/sys/glowforge/thermal/water_pump_on'
        self._pwm_path = '/sys/glowforge/thermal/heater_pwm'

    @property
    def heater_pwm(self) -> int:
        with open(self._pwm_path, 'r') as file:
            return int(file.readline())

    @heater_pwm.setter
    def heater_pwm(self, pwm: int = None):
        if pwm > 65535 or pwm < 0:
            raise ValueError("PWM must be between 0 and 65535.")
        with open(self._pwm_path, 'w') as file:
            file.write(str(pwm))


class TEC(_WaterBase):
    def __init__(self):
        super(_WaterBase, self).__init__()
        self._name = "water_pump"
        self._path = '/sys/glowforge/thermal/tec_on'


water_pump = WaterPump()
tec = TEC()

__all__ = ['water_pump', 'tec']
