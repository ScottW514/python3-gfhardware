"""
(C) Copyright 2026
Scott Wiederhold, s.e.wiederhold@gmail.com
https://community.openglow.org

SPDX-License-Identifier:    MIT

Host tests for the cloud-mode job supervision in gfhardware.machine: how
a running or waiting job reacts to the lid, the interlock loop, the
button, and a service cancel. The real Machine code runs against a fake
kernel wrapper (records every cnc write, plays a simple state machine)
and a fake switch monitor (settable switch word, edge delivery on the
switch thread), with the wire events captured.

Run:  PYTHONPATH=.:../Glowforge-Utilities python3 -m unittest tests.test_machine_lid_button
"""
import os
import sys
import tempfile
import threading
import time
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(os.path.dirname(ROOT), 'Glowforge-Utilities'))
os.environ['GFHOME_CONF'] = os.path.join(tempfile.gettempdir(), 'no-such-forgefirm.conf')

# The package __init__ imports the machine, which imports the hardware
# modules (one of them a compiled evdev extension): register a bare
# package first so gfhardware._common loads without it, then the fakes,
# then the machine.
_pkg = types.ModuleType('gfhardware')
_pkg.__path__ = [os.path.join(ROOT, 'gfhardware')]
sys.modules['gfhardware'] = _pkg

from gfhardware._common import (InputSwitch, MachineState, ButtonColor,   # noqa: E402
                                SwitchEvent, Position, AxisPosition, PulsPosition,
                                HeadInfo)


# ---------------------------------------------------------------- fakes

class FakeCNC:
    """Kernel wrapper stand-in. state plays: run() -> RUNNING for
    `run_reads` state reads, then IDLE (the program's end); stop() ->
    IDLE at once; resume(-n) -> RUNNING for `backtrack_reads` reads then
    IDLE; resume(+n) -> RUNNING again for the remainder."""

    def __init__(self):
        self.writes = []
        self._state = MachineState.IDLE
        self._reads_left = 0
        self.run_reads = 10 ** 9
        self.backtrack_reads = 3
        self.processed = 100
        self.total = 1000
        self.latch = 1
        self.resume_error = None

    # -- attributes the Machine constructor maps pulse-header keys onto
    def set_step_freq(self, v): self.writes.append(('step_freq', v))
    def set_x_decay(self, v): pass
    def set_x_current(self, v): pass
    def set_x_mode(self, v): pass
    def set_y_decay(self, v): pass
    def set_y_current(self, v): pass
    def set_y_mode(self, v): pass

    # -- job path
    def run(self):
        self.writes.append(('run', 1))
        self._state = MachineState.RUNNING
        self._reads_left = self.run_reads

    def stop(self):
        self.writes.append(('stop', 1))
        self._state = MachineState.IDLE

    def resume(self, steps):
        if self.resume_error is not None:
            raise self.resume_error
        self.writes.append(('resume', int(steps)))
        self._state = MachineState.RUNNING
        self._reads_left = self.backtrack_reads if steps < 0 else self.run_reads

    def laser_latch(self, v):
        self.writes.append(('laser_latch', int(v)))
        self.latch = int(v)

    def clear_all(self): self.writes.append(('clear_all', 1))
    def set_pulse_dev(self, dev): pass
    def enable(self): pass
    def disable(self): pass
    def reset(self): pass

    @property
    def state(self):
        if self._state is MachineState.RUNNING:
            self._reads_left -= 1
            if self._reads_left <= 0:
                self._state = MachineState.IDLE
                self.processed = self.total
        return self._state

    @property
    def position(self):
        ax = AxisPosition(10, 1.0, 0.04)
        return Position(ax, ax, ax, PulsPosition(self.total, self.processed))


class FakeSwitches:
    """Switch monitor stand-in: a settable switch word plus edge delivery
    into the machine's handler on a helper thread (as the real monitor
    thread would)."""

    def __init__(self):
        # Pending edge timers from the previous test must never land in
        # this one's run loop.
        for t in getattr(self, 'timers', []):
            t.cancel()
        for t in getattr(self, 'timers', []):
            t.join(1.0)
        self.timers = []
        self.word = {s: False for s in InputSwitch}
        self.word[InputSwitch.SW_DOOR1] = True
        self.word[InputSwitch.SW_DOOR2] = True
        self.word[InputSwitch.SW_DOORS] = True
        self.handler = None
        self.started = False

    def start(self): self.started = True
    def join(self): pass
    stop = False

    def all_switches(self):
        return dict(self.word)

    def _edge(self, code, val):
        self.word[code] = val
        if self.handler:
            self.handler(SwitchEvent(0, 0, 5, code, val))

    def _later(self, delay, fn):
        t = threading.Timer(delay, fn)
        self.timers.append(t)
        t.start()

    def press(self, delay=0.0):
        self._later(delay, lambda: (self._edge(InputSwitch.SW_BUTTON, True),
                                    self._edge(InputSwitch.SW_BUTTON, False)))

    def open_lid(self, delay=0.0):
        self._later(delay, lambda: (self._edge(InputSwitch.SW_DOORS, False),
                                    self._edge(InputSwitch.SW_DOOR1, False)))

    def close_lid(self, delay=0.0):
        self._later(delay, lambda: (self._edge(InputSwitch.SW_DOOR1, True),
                                    self._edge(InputSwitch.SW_DOORS, True)))

    def open_interlock(self, delay=0.0):
        self._later(delay, lambda: self._edge(InputSwitch.SW_INTERLOCK, True))


class FakeCooling:
    def __init__(self):
        self.armed = False
        self.mode = 'idle'
        self._fire_ok = True

    def set_armed(self, a): self.armed = bool(a)
    def set_mode(self, m): self.mode = m
    def fire_ok(self): return self._fire_ok
    def verdict(self): return {'fire_ok': self._fire_ok}
    def clear_profile(self): pass
    def start(self): pass
    def profile_air_assist(self, v): pass
    def profile_exhaust(self, v): pass
    def profile_intake(self, v): pass


def _install_fakes():
    """Register stand-ins for the hardware modules before gfhardware.machine
    is imported (it star-imports them at module load)."""
    cnc_mod = types.ModuleType('gfhardware.cnc')
    cnc_mod.cnc = FakeCNC()
    cnc_mod.__all__ = ['cnc']

    sw_mod = types.ModuleType('gfhardware.switches')
    fake_sw = FakeSwitches()

    class SwitchMonitor:
        def __new__(cls, dev, handler):
            fake_sw.handler = handler
            return fake_sw
    sw_mod.SwitchMonitor = SwitchMonitor
    sw_mod.__all__ = ['SwitchMonitor']

    leds_mod = types.ModuleType('gfhardware.leds')
    leds_mod.button_colors = []
    leds_mod.set_button_color = lambda c: leds_mod.button_colors.append(c)
    leds_mod.head_all_led_off = lambda: None
    leds_mod.set_lid_led = lambda v: None
    leds_mod.set_head_led_from_pulse = lambda v: None
    leds_mod.__all__ = ['set_button_color', 'head_all_led_off', 'set_lid_led',
                        'set_head_led_from_pulse']

    cooling_mod = types.ModuleType('gfhardware.cooling')

    class _Temp:
        C = 20.0

    class _Sensors:
        water_2 = _Temp()
        all = {}
    cooling_mod.temp_sensor = _Sensors()

    class WaterPump:
        @staticmethod
        def heater_off(): pass
    cooling_mod.WaterPump = WaterPump
    cooling_mod.__all__ = ['temp_sensor', 'WaterPump']

    coolsvc_mod = types.ModuleType('gfhardware.coolsvc')
    coolsvc_mod.cooling_svc = FakeCooling()

    z_mod = types.ModuleType('gfhardware.z_axis')

    class ZAxis:
        homed = 0

        @staticmethod
        def home(): ZAxis.homed += 1

        @staticmethod
        def step(d): pass

        @staticmethod
        def set_mode_from_puls(v): pass

        @staticmethod
        def reset(): pass
    z_mod.ZAxis = ZAxis

    cam_mod = types.ModuleType('gfhardware.cam')
    cam_mod.GFCAM_HEAD = 0
    cam_mod.GFCAM_LID = 1
    cam_mod.capture = lambda *a, **k: b''

    id_mod = types.ModuleType('gfhardware.id')
    id_mod.serial = lambda: 'XXX-000'
    id_mod.hostname = lambda: 'host'
    id_mod.password = lambda: 'pw'

    for name, mod in (('gfhardware.cnc', cnc_mod), ('gfhardware.switches', sw_mod),
                      ('gfhardware.leds', leds_mod), ('gfhardware.cooling', cooling_mod),
                      ('gfhardware.coolsvc', coolsvc_mod), ('gfhardware.z_axis', z_mod),
                      ('gfhardware.cam', cam_mod), ('gfhardware.id', id_mod)):
        sys.modules[name] = mod
    return cnc_mod.cnc, fake_sw, coolsvc_mod.cooling_svc, leds_mod


CNC, SW, COOL, LEDS = _install_fakes()

import gfhardware.machine as machine_mod          # noqa: E402
from gfhardware.machine import Machine             # noqa: E402
from gfutilities.configuration import set_cfg     # noqa: E402

set_cfg('THERMAL.MAX_START_TEMP', 50)
# Point the reads the constructor makes at fakes.
machine_mod.read_file = lambda attr, binary=False: 'hw_id=0x4c\nserial=1\nversion=0x2\nr5=0\nr6=0'
EVENTS = []
machine_mod.send_wss_event = lambda q, action_id, event: EVENTS.append(event)
machine_mod.generate_linear_puls = lambda x, y, dev: None


def job_events():
    """The action's own wire events (the unsolicited button:*/lid:* edge
    reports are expected alongside and not part of these checks)."""
    return [e for e in EVENTS if e.startswith('print:')]


def make_machine():
    m = Machine()
    m.running_action_id = 42
    m._running_action_cancelled = False
    return m


class RunLoopTests(unittest.TestCase):

    def setUp(self):
        CNC.__init__()
        SW.__init__()
        COOL.__init__()
        del EVENTS[:]
        del LEDS.button_colors[:]
        self.m = make_machine()
        SW.handler = self.m._switch_event

    # -- lid / interlock mid-run --------------------------------------

    def test_lid_open_mid_run_stops_and_cancels(self):
        SW.open_lid(delay=0.05)
        t0 = time.monotonic()
        aborted = self.m._run_loop()
        dt = time.monotonic() - t0
        self.assertTrue(aborted)
        self.assertTrue(self.m._running_action_cancelled)
        self.assertIn(('stop', 1), CNC.writes)
        # Edge-driven: the stop lands well inside one 100 ms poll tick.
        self.assertLess(dt, 0.15, 'stop was not edge-driven (%.3f s)' % dt)

    def test_interlock_open_mid_run_stops_and_cancels(self):
        SW.open_interlock(delay=0.05)
        aborted = self.m._run_loop()
        self.assertTrue(aborted)
        self.assertTrue(self.m._running_action_cancelled)
        self.assertIn(('stop', 1), CNC.writes)

    def test_lid_open_before_run_stops_on_first_pass(self):
        SW.word[InputSwitch.SW_DOORS] = False
        aborted = self.m._run_loop()
        self.assertTrue(aborted)
        self.assertIn(('stop', 1), CNC.writes)

    def test_transient_lid_open_still_cancels(self):
        # Open and closed again before the loop's level read: the edge
        # is what counts (the hardware button latch set on it).
        SW.open_lid(delay=0.02)
        SW.close_lid(delay=0.03)
        aborted = self.m._run_loop()
        self.assertTrue(aborted)
        self.assertTrue(self.m._running_action_cancelled)

    # -- the park and the hunt ignore the lid ---------------------------

    def test_park_ignores_lid(self):
        CNC.run_reads = 5
        SW.open_lid(delay=0.02)
        aborted = self.m._run_loop(park=True)
        self.assertFalse(aborted)
        self.assertNotIn(('stop', 1), CNC.writes)

    def test_park_ignores_service_cancel(self):
        CNC.run_reads = 5
        self.m._running_action_cancelled = True
        aborted = self.m._run_loop(park=True)
        self.assertFalse(aborted)
        self.assertNotIn(('stop', 1), CNC.writes)

    def test_hunt_ignores_lid(self):
        CNC.run_reads = 5
        SW.word[InputSwitch.SW_DOORS] = False
        aborted = self.m._run_loop(lid_gated=False)
        self.assertFalse(aborted)
        self.assertNotIn(('stop', 1), CNC.writes)

    def test_service_cancel_mid_run_stops(self):
        threading.Timer(0.05, lambda: setattr(self.m, '_running_action_cancelled', True)).start()
        aborted = self.m._run_loop()
        self.assertTrue(aborted)
        self.assertIn(('stop', 1), CNC.writes)

    def test_cooling_verdict_pulled_relocks_and_stops(self):
        threading.Timer(0.05, lambda: setattr(COOL, '_fire_ok', False)).start()
        COOL.armed = True
        aborted = self.m._run_loop()
        self.assertTrue(aborted)
        self.assertIn(('laser_latch', 1), CNC.writes)
        self.assertIn(('stop', 1), CNC.writes)

    # -- button pause / resume (prints) --------------------------------

    def test_button_pauses_and_resumes_a_print(self):
        SW.press(delay=0.05)                       # pause
        SW.press(delay=0.40)                       # resume
        threading.Timer(0.6, lambda: setattr(CNC, '_reads_left', 1)).start()   # end the program
        aborted = self.m._run_loop(pausable=True)
        self.assertFalse(aborted)
        w = CNC.writes
        self.assertIn(('stop', 1), w)
        self.assertIn(('resume', -2000), w)
        self.assertIn(('resume', 1950), w)
        self.assertLess(w.index(('stop', 1)), w.index(('resume', -2000)))
        self.assertLess(w.index(('resume', -2000)), w.index(('resume', 1950)))
        self.assertEqual(job_events(), ['print:paused', 'print:resumed'])
        self.assertFalse(self.m._running_action_cancelled)
        # The latch was never touched by the pause.
        self.assertNotIn(('laser_latch', 1), w)

    def test_button_does_not_pause_a_motion(self):
        CNC.run_reads = 5
        SW.press(delay=0.02)
        aborted = self.m._run_loop(pausable=False)
        self.assertFalse(aborted)
        self.assertNotIn(('stop', 1), CNC.writes)
        self.assertEqual(job_events(), [])

    def test_lid_open_while_paused_cancels_without_second_stop(self):
        SW.press(delay=0.05)                       # pause
        SW.open_lid(delay=0.40)
        aborted = self.m._run_loop(pausable=True)
        self.assertTrue(aborted)
        self.assertTrue(self.m._running_action_cancelled)
        self.assertEqual(CNC.writes.count(('stop', 1)), 1)   # only the pause's stop
        self.assertEqual(job_events(), ['print:paused'])

    def test_service_cancel_while_paused(self):
        SW.press(delay=0.05)
        threading.Timer(0.4, lambda: setattr(self.m, '_running_action_cancelled', True)).start()
        aborted = self.m._run_loop(pausable=True)
        self.assertTrue(aborted)
        self.assertEqual(job_events(), ['print:paused'])

    def test_pause_at_program_end_finishes(self):
        # The decel lands on the program's last byte: no backtrack, done.
        SW.press(delay=0.05)

        def end_it():
            CNC.processed = CNC.total
        threading.Timer(0.049, end_it).start()
        aborted = self.m._run_loop(pausable=True)
        self.assertFalse(aborted)
        self.assertNotIn(('resume', -2000), CNC.writes)
        self.assertEqual(job_events(), [])

    def test_backtrack_refused_pauses_in_place(self):
        # The kernel refuses the backtrack (e.g. a streamed ring): the job
        # holds where the decel stopped, still paused, still resumable.
        SW.press(delay=0.05)
        SW.press(delay=0.40)
        threading.Timer(0.6, lambda: setattr(CNC, '_reads_left', 1)).start()
        orig = CNC.resume

        def resume(steps):
            if steps < 0:
                raise OSError(1, 'EPERM')
            orig(steps)
        CNC.resume = resume
        try:
            aborted = self.m._run_loop(pausable=True)
        finally:
            del CNC.resume
        self.assertFalse(aborted)
        self.assertEqual(job_events(), ['print:paused', 'print:resumed'])
        self.assertIn(('resume', 1950), CNC.writes)


class ButtonWaitTests(unittest.TestCase):

    def setUp(self):
        CNC.__init__()
        SW.__init__()
        COOL.__init__()
        del EVENTS[:]
        self.m = make_machine()
        SW.handler = self.m._switch_event
        COOL.armed = True

    def test_press_ends_the_wait(self):
        SW.press(delay=0.05)
        self.m._button_wait({'id': 42})
        self.assertFalse(self.m._running_action_cancelled)
        self.assertNotIn(('laser_latch', 1), CNC.writes)

    def test_lid_open_cancels_the_wait(self):
        SW.open_lid(delay=0.05)
        self.m._button_wait({'id': 42})
        self.assertTrue(self.m._running_action_cancelled)
        self.assertIn(('laser_latch', 1), CNC.writes)
        self.assertFalse(COOL.armed)

    def test_interlock_open_cancels_the_wait(self):
        SW.open_interlock(delay=0.05)
        self.m._button_wait({'id': 42})
        self.assertTrue(self.m._running_action_cancelled)
        self.assertIn(('laser_latch', 1), CNC.writes)

    def test_press_with_lid_open_does_not_arm(self):
        SW.word[InputSwitch.SW_DOORS] = False
        SW.press(delay=0.02)
        self.m._button_wait({'id': 42})
        self.assertTrue(self.m._running_action_cancelled)
        self.assertIn(('laser_latch', 1), CNC.writes)


class StartGateTests(unittest.TestCase):

    def setUp(self):
        CNC.__init__()
        SW.__init__()
        COOL.__init__()
        del EVENTS[:]
        self.m = make_machine()
        SW.handler = self.m._switch_event

    def test_lid_open_at_start_cancels_a_gated_job(self):
        SW.word[InputSwitch.SW_DOORS] = False
        self.m._motion({'id': 42, 'action_type': 'motion', 'motion_url': 'x'})
        self.assertTrue(self.m._running_action_cancelled)
        self.assertNotIn(('run', 1), CNC.writes)

    def test_interlock_open_at_start_cancels_a_gated_job(self):
        SW.word[InputSwitch.SW_INTERLOCK] = True
        self.m._motion({'id': 42, 'action_type': 'print', 'motion_url': 'x'})
        self.assertTrue(self.m._running_action_cancelled)

    def test_hunt_start_gate_ignores_the_lid(self):
        SW.word[InputSwitch.SW_DOORS] = False
        self.assertTrue(self.m._safe_to_move(lid_gated=False))
        self.assertFalse(self.m._safe_to_move(lid_gated=True))


if __name__ == '__main__':
    unittest.main()
