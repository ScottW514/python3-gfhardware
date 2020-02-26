"""
(C) Copyright 2020
Scott Wiederhold, s.e.wiederhold@gmail.com
https://community.openglow.org
SPDX-License-Identifier:    MIT
"""
import os
from typing import Union

from . import MachineState


class Machine(object):
    def __init__(self):
        self._base_path = '/sys/glowforge/cnc/'

    def _read(self, path: str, binary: bool = False) -> Union[str, bytes]:
        with open(self._base_path + path, 'br' if binary else 'r') as file:
            data = file.read()
            return data if binary else data.strip()

    def _write(self, path: str, val: Union[str, bytes], binary: bool = False):
        with open(self._base_path + path, 'bw' if binary else 'w') as file:
            file.write(val)

    def _command(self, cmd: str):
        self._write(cmd, '1')

    def clear_all(self):
        with open('/dev/glowforge', 'w') as f:
            os.lseek(f.fileno(), 0, os.SEEK_SET)

    def clear_pulse_and_byte(self):
        with open('/dev/glowforge', 'w') as f:
            os.lseek(f.fileno(), 1, os.SEEK_SET)

    def clear_position(self):
        with open('/dev/glowforge', 'w') as f:
            os.lseek(f.fileno(), 2, os.SEEK_SET)

    def disable(self):
        self._command('disable')

    def enable(self):
        self._command('enable')

    @property
    def faults(self) -> str:
        return self._read('faults')

    @property
    def ignored_faults(self) -> str:
        return self._read('ignored_faults')

    @ignored_faults.setter
    def ignored_faults(self, val):
        self._write('ignored_faults', val)

    def laser_latch(self, val):
        self._write('laser_latch', val)

    def load_pulse(self, data: bytes):
        with open('/dev/glowforge', 'bw') as f:
            f.write(data)

    @property
    def motor_lock(self) -> str:
        return self._read('motor_lock')

    @motor_lock.setter
    def motor_lock(self, val):
        self._write('motor_lock', val)

    @property
    def position(self) -> dict:
        raw = self._read('position', binary=True)
        return {
            'x': int.from_bytes(raw[0:3], byteorder='little', signed=True),
            'y': int.from_bytes(raw[4:7], byteorder='little', signed=True),
            'z': int.from_bytes(raw[8:11], byteorder='little', signed=True),
            'bytes_processed': int.from_bytes(raw[12:15], byteorder='little', signed=False),
            'bytes_total': int.from_bytes(raw[16:19], byteorder='little', signed=False),
        }

    def resume(self):
        self._command('resume')

    def run(self):
        self._command('run')

    @property
    def sdma_context(self) -> str:
        return self._read('sdma_context')

    @property
    def state(self) -> MachineState:
        state = self._read('state')
        if state == 'disabled':
            return MachineState.DISABLED
        elif state == 'idle':
            return MachineState.IDLE
        elif state == 'running':
            return MachineState.RUNNING
        elif state == 'fault':
            return MachineState.FAULT
        else:
            raise ValueError('Received invalid state: {}'.format(state))

    @property
    def step_freq(self) -> str:
        return self._read('step_freq')

    @step_freq.setter
    def step_freq(self, val):
        self._write('step_freq', val)

    def stop(self):
        self._command('stop')

    @property
    def x_decay(self) -> str:
        return self._read('x_decay')

    @x_decay.setter
    def x_decay(self, val):
        self._write('x_decay', val)

    @property
    def x_mode(self) -> str:
        return self._read('x_mode')

    @x_mode.setter
    def x_mode(self, val):
        self._write('x_mode', val)

    @property
    def y_decay(self) -> str:
        return self._read('y_decay')

    @y_decay.setter
    def y_decay(self, val):
        self._write('y_decay', val)

    @property
    def y_mode(self) -> str:
        return self._read('y_mode')

    @y_mode.setter
    def y_mode(self, val):
        self._write('y_mode', val)

    def z_step(self, val):
        self._write('z_step', val)
