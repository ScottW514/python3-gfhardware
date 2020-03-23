"""
(C) Copyright 2020
Scott Wiederhold, s.e.wiederhold@gmail.com
https://community.openglow.org
SPDX-License-Identifier:    MIT

Based on python-evdev
Copyright (c) 2012-2016 Georgi Valkov. All rights reserved.

"""
import logging
import os
import select
from threading import Thread
from typing import Callable

from gfhardware._common import *
if os.getenv('REMOTE_DEBUG'):
    import importlib.util
    evdev_spec = importlib.util.spec_from_file_location(
        "input.evdev", "/usr/lib/python3.7/site-packages/gfhardware/input/evdev.cpython-37m-arm-linux-gnueabi.so")
    evdev = importlib.util.module_from_spec(evdev_spec)
    evdev_spec.loader.exec_module(evdev)
else:
    from gfhardware.input import evdev

logger = logging.getLogger(LOGGER_NAME)


class InputDevice(object):
    """
    A linux input device from which input events can be read.
    """
    __slots__ = ('path', 'fd')

    def __init__(self, dev):
        """
        Arguments
        ---------
        dev : str|bytes|PathLike
          Path to input device
        """
        self.path = dev if not hasattr(dev, '__fspath__') else dev.__fspath__()
        fd = os.open(dev, os.O_RDONLY | os.O_NONBLOCK)
        self.fd = fd

    def __del__(self):
        if hasattr(self, 'fd') and self.fd is not None:
            try:
                self.close()
            except OSError:
                pass

    def close(self):
        if self.fd > -1:
            try:
                os.close(self.fd)
            finally:
                self.fd = -1

    def read_loop(self) -> SwitchEvent:
        """
        Enter an endless :func:`select.select()` loop that yields input events.
        """
        evdev.ioctl_EVIOCGRAB(self.fd, 1)
        try:
            while True:
                r, w, x = select.select([self.fd], [], [], .1)
                if len(r) == 0:
                    yield None
                else:
                    for event in self.read():
                        yield event
        finally:
            evdev.ioctl_EVIOCGRAB(self.fd, 0)

    def read(self) -> SwitchEvent:
        """
        Read multiple input events from device. Return a generator object that
        yields :class:`InputEvent <evdev.events.InputEvent>` instances. Raises
        `BlockingIOError` if there are no available events at the moment.
        """
        # events -> [(sec, usec, type, code, val), ...]
        events = evdev.device_read_many(self.fd)

        for event in events:
            sec, usec, e_type, code, val = tuple(event)
            if e_type == 5:
                code = InputSwitch(code)
                val = True if int(val) == 1 else False
            elif e_type == 0:
                code = SynCode(code)
            yield SwitchEvent(sec, usec, e_type, code, val)

    def switch_states(self) -> dict:
        """
        Return current switch states.
        i.e. {<InputSwitch.DOOR1: 0>: True, <InputSwitch.DOOR2: 1>: False, ...}
        """
        active_switches = evdev.ioctl_EVIOCG_bits(self.fd, EventCode.EV_SW.value)
        switch_states = {}
        for switch in InputSwitch:
            switch_states[switch] = True if switch.value in active_switches else False
        return switch_states


class SwitchMonitor(Thread):
    """
    Switch Event Process Queue
    Responds to incoming WSS events for motion actions..
    """
    def __init__(self, input_dev: str, event_handler: Callable[[SwitchEvent], None]):
        """
        Initialize Switch Event Thread
        :param input_dev: Input Device object
        :type input_dev: str
        :param event_handler: Function to pass event object to
        :type event_handler: object
        """
        self._input_dev = InputDevice(input_dev)
        self.stop = False
        self._event_handler = event_handler
        Thread.__init__(self)

    def run(self) -> None:
        logger.debug('THREAD START')
        for event in self._input_dev.read_loop():
            if event is not None:
                if event.type == 5:
                    self._event_handler(event)
                elif event.type == 0 and event.code == SynCode.SYN_DROPPED:
                    logger.error('switch monitor dropped events: %s' % str(event))
            if self.stop:
                break
        logger.debug('THREAD EXIT')

    def switch_state(self, switch: InputSwitch) -> bool:
        return self.all_switches().get(switch, None)

    def all_switches(self) -> dict:
        return self._input_dev.switch_states()


__all__ = ['SwitchMonitor']
