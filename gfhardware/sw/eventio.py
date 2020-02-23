"""
(C) Copyright 2020
Scott Wiederhold, s.e.wiederhold@gmail.com
https://community.openglow.org
SPDX-License-Identifier:    MIT

Based on python-evdev
Copyright (c) 2012-2016 Georgi Valkov. All rights reserved.
"""
import select

from gfhardware.sw import _input
from gfhardware.sw.events import InputEvent


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
