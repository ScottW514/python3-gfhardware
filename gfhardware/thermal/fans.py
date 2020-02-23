"""
(C) Copyright 2020
Scott Wiederhold, s.e.wiederhold@gmail.com
https://community.openglow.org
SPDX-License-Identifier:    MIT
"""


class Fan(object):
    def __init__(self, fan_desc: dict):
        self._max_speed = fan_desc.get('max_speed') or 1
        self._min_speed = fan_desc.get('min_speed') or 0
        self._pwm_path = fan_desc.get('pwm_path') or None
        self._tach_path = fan_desc.get('tach_path') or None
        self._tach_calc = fan_desc.get('tach_calc') or self._tach_calc
        self._name = fan_desc.get('name') or None

        self._state = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def set_speed(self) -> int:
        with open(self._pwm_path, 'r') as file:
            return int(file.readline())

    @set_speed.setter
    def set_speed(self, speed: int = None):
        if speed > self._max_speed or speed < self._min_speed:
            raise ValueError("Speed must be between {} and {}.".format(self._min_speed, self._max_speed))
        with open(self._pwm_path, 'w') as file:
            file.write(str(speed))

    @property
    def get_speed(self) -> int:
        with open(self._tach_path, 'r') as file:
            return self._tach_calc(int(file.readline()))

    def off(self):
        self.set_speed = self._min_speed

    def _tach_calc(self, in_speed: int) -> int:
        return in_speed


# These are rough guesses
def _2pole_fan_tach(ns_period: int) -> int:
    if ns_period == 0:
        return 0
    return int(((1/(ns_period/1000000000))*60)/2)


def _8pole_fan_tach(ns_period: int) -> int:
    if ns_period == 0:
        return 0
    return int(((1/(ns_period/1000000))*60)/8)


exhaust_fan = Fan({
    'name': 'exhaust_fan',
    'max_speed': 65535,
    'min_speed': 0,
    'pwm_path': '/sys/glowforge/thermal/exhaust_pwm',
    'tach_path': '/sys/glowforge/thermal/tach_exhaust',
    'tach_calc': _2pole_fan_tach,
})

intake_fan1 = Fan({
    'name': 'intake_fan1',
    'max_speed': 65535,
    'min_speed': 0,
    'pwm_path': '/sys/glowforge/thermal/intake_pwm',
    'tach_path': '/sys/glowforge/thermal/tach_intake_1',
    'tach_calc': _2pole_fan_tach,
})

intake_fan2 = Fan({
    'name': 'intake_fan2',
    'max_speed': 65535,
    'min_speed': 0,
    'pwm_path': '/sys/glowforge/thermal/intake_pwm',
    'tach_path': '/sys/glowforge/thermal/tach_intake_2',
    'tach_calc': _2pole_fan_tach,
})

air_assist_fan = Fan({
    'name': 'air_assist_fan',
    'max_speed': 1023,
    'min_speed': 96,
    'pwm_path': '/sys/glowforge/head/air_assist_pwm',
    'tach_path': '/sys/glowforge/head/air_assist_tach',
    'tach_calc': _8pole_fan_tach,
})

purge_fan = Fan({
    'name': 'air_assist_fan',
    'max_speed': 1,
    'min_speed': 0,
    'pwm_path': '/sys/glowforge/head/purge_air',
    'tach_path': '/sys/glowforge/head/purge_air_current',
    'tach_calc': None,
})

__all__ = ['exhaust_fan', 'intake_fan1', 'intake_fan2', 'air_assist_fan', 'purge_fan']
