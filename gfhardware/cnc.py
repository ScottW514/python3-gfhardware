"""
(C) Copyright 2020
Scott Wiederhold, s.e.wiederhold@gmail.com
https://community.openglow.org
SPDX-License-Identifier:    MIT
"""
import os

from . import MachineState
from .shared import read_file, write_file


class Machine(object):
    def __init__(self):
        self._base_path = '/sys/glowforge/cnc/'

    def _command(self, cmd: str):
        write_file(self._base_path + cmd, '1')

    def _dev_seek(self, count):
        with open('/dev/glowforge', 'w') as f:
            os.lseek(f.fileno(), count, os.SEEK_SET)

    def clear_all(self):
        self._dev_seek(0)

    def clear_pulse_and_byte(self):
        self._dev_seek(1)

    def clear_position(self):
        self._dev_seek(2)

    def disable(self):
        self._command('disable')

    def enable(self):
        self._command('enable')

    @property
    def faults(self) -> str:
        return read_file(self._base_path + 'faults')

    @property
    def ignored_faults(self) -> str:
        return read_file(self._base_path + 'ignored_faults')

    @ignored_faults.setter
    def ignored_faults(self, val):
        write_file(self._base_path + 'ignored_faults', val)

    def laser_latch(self, val):
        write_file(self._base_path + 'laser_latch', val)

    def load_pulse(self, data: bytes):
        write_file('/dev/glowforge', data, True)

    @property
    def motor_lock(self) -> str:
        return read_file(self._base_path + 'motor_lock')

    @motor_lock.setter
    def motor_lock(self, val):
        write_file(self._base_path + 'motor_lock', val)

    @property
    def position(self) -> dict:
        raw = read_file(self._base_path + 'position', True)
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
        return read_file(self._base_path + 'sdma_context')

    @property
    def state(self) -> MachineState:
        state = read_file(self._base_path + 'state')
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
        return read_file(self._base_path + 'step_freq')

    @step_freq.setter
    def step_freq(self, val):
        write_file(self._base_path + 'step_freq', val)

    def stop(self):
        self._command('stop')

    @property
    def x_decay(self) -> str:
        return read_file(self._base_path + 'x_decay')

    @x_decay.setter
    def x_decay(self, val):
        write_file(self._base_path + 'x_decay', val)

    @property
    def x_mode(self) -> str:
        return read_file(self._base_path + 'x_mode')

    @x_mode.setter
    def x_mode(self, val):
        write_file(self._base_path + 'x_mode', val)

    @property
    def y_decay(self) -> str:
        return read_file(self._base_path + 'y_decay')

    @y_decay.setter
    def y_decay(self, val):
        write_file(self._base_path + 'y_decay', val)

    @property
    def y_mode(self) -> str:
        return read_file(self._base_path + 'y_mode')

    @y_mode.setter
    def y_mode(self, val):
        write_file(self._base_path + 'y_mode', val)

    def z_step(self, val):
        write_file(self._base_path + 'z_step', val)
