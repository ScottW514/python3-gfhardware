"""
(C) Copyright 2020
Scott Wiederhold, s.e.wiederhold@gmail.com
https://community.openglow.org
SPDX-License-Identifier:    MIT

Based on python-evdev
Copyright (c) 2012-2016 Georgi Valkov. All rights reserved.
"""
import os
import warnings
import contextlib

from gfhardware.sw import _input, InputSwitch, EventCode

from gfhardware.sw.eventio import EventIO


class InputDevice(EventIO):
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

    def __str__(self):
        msg = 'device {}'
        return msg.format(self.path)

    def __repr__(self):
        msg = (self.__class__.__name__, self.path)
        return '{}({!r})'.format(*msg)

    def __fspath__(self):
        return self.path

    def close(self):
        if self.fd > -1:
            try:
                super().close()
                os.close(self.fd)
            finally:
                self.fd = -1

    def grab(self):
        """
        Grab input device using ``EVIOCGRAB`` - other applications will
        be unable to receive events until the device is released. Only
        one process can hold a ``EVIOCGRAB`` on a device.

        Warning
        -------
        Grabbing an already grabbed device will raise an ``IOError``.
        """
        _input.ioctl_EVIOCGRAB(self.fd, 1)

    def ungrab(self):
        """
        Release device if it has been already grabbed (uses `EVIOCGRAB`).

        Warning
        -------
        Releasing an already released device will raise an
        ``IOError('Invalid argument')``.
        """
        _input.ioctl_EVIOCGRAB(self.fd, 0)

    @contextlib.contextmanager
    def grab_context(self):
        """
        A context manager for the duration of which only the current
        process will be able to receive events from the device.
        """
        self.grab()
        yield
        self.ungrab()

    def switch_states(self):
        """
        Return current switch states.
        i.e. {<InputSwitch.DOOR1: 0>: True, <InputSwitch.DOOR1: 0>: False, ...}
        """
        active_switches = _input.ioctl_EVIOCG_bits(self.fd, EventCode.EV_SW.value)
        switch_states = {}
        for switch in InputSwitch:
            switch_states[switch] = True if switch.value in active_switches else False
        return switch_states

    @property
    def fn(self):
        msg = 'Please use {0}.path instead of {0}.fn'.format(self.__class__.__name__)
        warnings.warn(msg, DeprecationWarning, stacklevel=2)
        return self.path
