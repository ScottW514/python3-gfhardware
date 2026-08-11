"""
(C) Copyright 2020
Scott Wiederhold, s.e.wiederhold@gmail.com
https://community.openglow.org
SPDX-License-Identifier:    MIT
"""
import json
import logging
import os
import time
from threading import Thread, Lock
from urllib import parse, request

from gfhardware._common import LOGGER_NAME

logger = logging.getLogger(LOGGER_NAME)

VERDICT_FILE = '/run/forgefirm/cooling.state'
# Reader staleness window per the contract: a verdict older than this
# is no verdict (fire blocked). The engine publishes at 1 Hz.
VERDICT_MAX_AGE_S = 2.0
REPORT_PERIOD_S = 1.0
# localhost: anything slower means forgectrl is wedged - drop the
# report, the level-triggered refresh retries in a second.
REPORT_TIMEOUT_S = 0.25


class CoolingService(Thread):
    """Client of the forgectrl cooling engine (contract: forgectrl
    docs/SERVICES.md).

    The engine owns the thermal hardware - fan/pump/TEC/heater profiles,
    coolant flow verification, over-temp policy - for every controller
    mode. This client reports job state (mode, armed, and the per-job
    run fan duties from the pulse header) level-triggered at ~1 Hz, and
    reads the engine's published verdict on the fire path, treating a
    missing or stale verdict file as fire-blocked.
    """

    def __init__(self):
        self.stop = False
        self.armed = False
        self._lock = Lock()
        self._mode = 'idle'
        self._profile = {}
        port = os.getenv('FORGECTRL_PORT', '8080')
        self._url = 'http://127.0.0.1:%s/cool/state' % port
        Thread.__init__(self, daemon=True)

    # ------------------------------------------------------- job state

    def set_mode(self, mode: str) -> None:
        with self._lock:
            self._mode = mode
        self.report()

    def set_armed(self, armed: bool) -> None:
        self.armed = bool(armed)
        self.report()

    # Per-job run fan profile, fed from the pulse-header keys by the
    # MACHINE_SETTINGS map (AArd/EFrd/IFrd). The engine falls back to
    # its configured/factory duties for anything unset or out of range.
    def profile_air_assist(self, val) -> None:
        self._set_duty('air_assist', val)

    def profile_exhaust(self, val) -> None:
        self._set_duty('exhaust', val)

    def profile_intake(self, val) -> None:
        self._set_duty('intake', val)

    def _set_duty(self, key: str, val) -> None:
        try:
            with self._lock:
                self._profile[key] = int(val)
        except (TypeError, ValueError):
            logger.warning('bad %s duty in pulse header: %r', key, val)

    def clear_profile(self) -> None:
        with self._lock:
            self._profile = {}

    def report(self) -> None:
        with self._lock:
            params = {'mode': self._mode, 'armed': int(self.armed)}
            params.update(self._profile)
        try:
            request.urlopen(
                request.Request('%s?%s' % (self._url, parse.urlencode(params)),
                                method='POST'),
                timeout=REPORT_TIMEOUT_S).close()
        except OSError:
            pass    # level-triggered: the next report self-heals

    def run(self):
        while not self.stop:
            self.report()
            time.sleep(REPORT_PERIOD_S)

    # --------------------------------------------------------- verdict

    def verdict(self) -> dict:
        """The engine's current verdict, or None when missing/stale
        (which readers must treat as fire_ok=False, hold=True)."""
        try:
            with open(VERDICT_FILE) as f:
                v = json.load(f)
            age = time.clock_gettime(time.CLOCK_MONOTONIC) - v['ts_mono']
            if age > VERDICT_MAX_AGE_S:
                return None
            return v
        except (OSError, ValueError, KeyError):
            return None

    def fire_ok(self) -> bool:
        v = self.verdict()
        return bool(v and v.get('fire_ok'))


cooling_svc = CoolingService()

__all__ = ['cooling_svc', 'CoolingService']
