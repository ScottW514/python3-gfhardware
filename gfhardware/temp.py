"""
(C) Copyright 2020
Scott Wiederhold, s.e.wiederhold@gmail.com
https://community.openglow.org
SPDX-License-Identifier:    MIT
"""
from .shared import read_file


class TempSensor(object):
    def __init__(self, sensor_def: dict):
        self._name = sensor_def.get('name') or None
        self._sensor_path = sensor_def.get('sensor_path') or None
        self._temp_calc = sensor_def.get('temp_calc') or self._calc

    @property
    def name(self) -> str:
        return self._name

    @property
    def temp(self) -> tuple:
        c = self._temp_calc(int(read_file(self._sensor_path)))
        return c, ((c * (9 / 5)) + 32)

    def _calc(self, in_value: int) -> float:
        return float(in_value)


def _lm75_calc(in_value: int) -> float:
    return in_value/1000


chassis_temp = TempSensor({
    'name': 'chassis_temp',
    'sensor_path': '/sys/class/hwmon/hwmon0/temp1_input',
    'temp_calc': _lm75_calc
})

water_temp_1 = TempSensor({
    'name': 'water_temp_1',
    'sensor_path': '/sys/glowforge/pic/water_temp_1',
    'temp_calc': None # Don't yet know
})

water_temp_2 = TempSensor({
    'name': 'water_temp_2',
    'sensor_path': '/sys/glowforge/pic/water_temp_2',
    'temp_calc': None # Don't yet know
})

pwr_temp = TempSensor({
    'name': 'pwr_temp',
    'sensor_path': '/sys/glowforge/pic/pwr_temp',
    'temp_calc': None # Don't yet know
})

tec_temp = TempSensor({
    'name': 'tec_temp',
    'sensor_path': '/sys/glowforge/pic/tec_temp',
    'temp_calc': None # Don't yet know
})

_all_temps = ['chassis_temp', 'water_temp_1', 'water_temp_2', 'pwr_temp', 'tec_temp']


def get_all_temps() -> dict:
    temps = {}
    for sensor in _all_temps:
        temps[sensor] = globals()[sensor].temp
    return temps


__all__ = _all_temps + ['get_all_temps']
