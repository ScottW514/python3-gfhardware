"""
(C) Copyright 2020
Scott Wiederhold, s.e.wiederhold@gmail.com
https://community.openglow.org
SPDX-License-Identifier:    MIT

Based on python-evdev
Copyright (c) 2012-2016 Georgi Valkov. All rights reserved.

"""
import os
import select
import warnings
import contextlib

from gfhardware.input import evdev
from gfhardware import InputSwitch, EventCode


class InputEvent(object):
    """
    A generic input event.
    """
    __slots__ = 'sec', 'usec', 'type', 'code', 'value'

    def __init__(self, sec, usec, type, code, value):
        self.sec = sec
        self.usec = usec
        self.type = type
        self.code = code
        self.value = value

    def timestamp(self):
        return self.sec + (self.usec / 1000000.0)

    def __str__(s):
        msg = 'event at {:f}, code {:02d}, type {:02d}, val {:02d}'
        return msg.format(s.timestamp(), s.code, s.type, s.value)

    def __repr__(s):
        msg = '{}({!r}, {!r}, {!r}, {!r}, {!r})'
        return msg.format(s.__class__.__name__,
                          s.sec, s.usec, s.type, s.code, s.value)


class EventIO(object):
    """
    Base class for reading input events.

    This class is used by :class:`InputDevice`.

    - On, :class:`InputDevice` it used for reading user-generated events (e.g.
      key presses, mouse movements) and writing feedback events (e.g. leds,
      beeps).
    """
    __slots__ = 'fd'

    def fileno(self):
        """
        Return the file descriptor to the open event device. This makes
        it possible to pass instances directly to :func:`select.select()` and
        :class:`asyncore.file_dispatcher`.
        """
        return self.fd

    def read_loop(self):
        """
        Enter an endless :func:`select.select()` loop that yields input events.
        """
        while True:
            r, w, x = select.select([self.fd], [], [])
            for event in self.read():
                yield event

    def read_one(self):
        """
        Read and return a single input event as an instance of
        :class:`InputEvent <evdev.events.InputEvent>`.

        Return ``None`` if there are no pending input events.
        """
        # event -> (sec, usec, type, code, val)
        event = _input.device_read(self.fd)

        if event:
            return InputEvent(*event)

    def read(self):
        """
        Read multiple input events from device. Return a generator object that
        yields :class:`InputEvent <evdev.events.InputEvent>` instances. Raises
        `BlockingIOError` if there are no available events at the moment.
        """
        # events -> [(sec, usec, type, code, val), ...]
        events = _input.device_read_many(self.fd)

        for event in events:
            yield InputEvent(*event)

    def close(self):
        pass


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
